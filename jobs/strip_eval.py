# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "torch>=2.4", "timm>=1.0.9", "segmentation-models-pytorch>=0.4", "opencv-python-headless",
#   "shapely>=2", "scikit-image", "numpy", "rasterio",
# ]
# ///
"""HF Job (GPU): run the SN2 ConvNeXt-B U-Net on the full WorldView-3 strips the SpaceNet chips were cut from,
in windows that no SpaceNet chip (train or official test) comes near.

The strips (AOIs/<AOI>/PS-RGB/*_COG.TIF on the public SpaceNet bucket) are pixel-identical to the chips, so the
same per-city stretch, model and post-processing apply unchanged. For each city:
  1. read the bounds of every SpaceNet chip (train + test_public) and buffer them by ~100 m;
  2. lay a 650 px grid over the city's mosaic, drop windows touching that buffer or with > 5 % no-data,
     and sample N windows at random (fixed seed);
  3. predict, watershed, and write the footprints in lon/lat.
Output: /out/strip_eval/<AOI>.json  {windows: [{bounds, polys_wkt_geo}], ...}; scored locally against Overture
by scripts/strip_vs_overture.py.
"""
import argparse
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
import torch
from rasterio.windows import Window
from shapely import box
from shapely.ops import unary_union

S3 = "https://spacenet-dataset.s3.amazonaws.com"
AOIS = ["AOI_2_Vegas", "AOI_3_Paris", "AOI_4_Shanghai", "AOI_5_Khartoum"]
ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF,.vrt",
           GDAL_HTTP_MULTIRANGE="YES", GDAL_CACHEMAX=512)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def list_keys(prefix):
    keys, token = [], None
    while True:
        u = f"{S3}/?list-type=2&prefix={prefix}" + (f"&continuation-token={urllib.parse.quote(token)}" if token else "")
        x = urllib.request.urlopen(u, timeout=120).read().decode()
        keys += re.findall(r"<Key>([^<]+)</Key>", x)
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", x)
        if not m:
            return keys
        token = m.group(1)


def chip_bounds(key):
    for t in range(4):
        try:
            with rasterio.Env(**ENV), rasterio.open(f"/vsicurl/{S3}/{key}") as r:
                return tuple(r.bounds)
        except Exception:
            time.sleep(2)
    return None


def mosaic_vrt(aoi, out):
    """A GDAL VRT mosaicking the AOI's pansharpened-RGB strip files (one acquisition per AOI; the COG copy is used
    where one exists). All strips share the SpaceNet chip grid (EPSG:4326, 2.7e-6 deg), so offsets are exact."""
    keys = [k for k in list_keys(f"AOIs/{aoi}/PS-RGB/") if k.upper().endswith(".TIF")]
    cog = {k.replace("_COG.TIF", ".TIF") for k in keys if k.endswith("_COG.TIF")}
    files = [k.replace(".TIF", "_COG.TIF") if k in cog else k for k in keys if not k.endswith("_COG.TIF")]

    def hdr(k):
        with rasterio.Env(**ENV), rasterio.open(f"/vsicurl/{S3}/{k}") as r:
            return k, r.transform, r.width, r.height, r.dtypes[0]

    with ThreadPoolExecutor(32) as ex:
        info = list(ex.map(hdr, files))
    res_x, res_y = info[0][1].a, info[0][1].e
    ox, oy = min(t.c for _, t, *_ in info), max(t.f for _, t, *_ in info)
    W = max(round((t.c - ox) / res_x) + w for _, t, w, _, _ in info)
    H = max(round((t.f - oy) / res_y) + h for _, t, _, h, _ in info)
    bands = []
    for b in (1, 2, 3):
        srcs = "".join(
            f'<SimpleSource><SourceFilename relativeToVRT="0">/vsicurl/{S3}/{k}</SourceFilename><SourceBand>{b}</SourceBand>'
            f'<SrcRect xOff="0" yOff="0" xSize="{w}" ySize="{h}"/><DstRect xOff="{round((t.c - ox) / res_x)}" '
            f'yOff="{round((t.f - oy) / res_y)}" xSize="{w}" ySize="{h}"/></SimpleSource>' for k, t, w, h, _ in info)
        bands.append(f'<VRTRasterBand dataType="UInt16" band="{b}">{srcs}</VRTRasterBand>')
    Path(out).write_text(f'<VRTDataset rasterXSize="{W}" rasterYSize="{H}"><SRS>EPSG:4326</SRS>'
                         f'<GeoTransform>{ox}, {res_x}, 0, {oy}, 0, {res_y}</GeoTransform>{"".join(bands)}</VRTDataset>')
    log(aoi, len(info), "strip files ->", W, "x", H, "px mosaic")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/sn2")
    ap.add_argument("--out", default="/out")
    ap.add_argument("--model", default="/out/convnextb_unet_20k/model.pt")
    ap.add_argument("--encoder", default="tu-convnext_base.fb_in22k_ft_in1k")
    ap.add_argument("--n", type=int, default=400, help="windows per city")
    ap.add_argument("--buffer_deg", type=float, default=0.001, help="~100 m exclusion around SpaceNet chips")
    ap.add_argument("--aois", default=",".join(AOIS))
    ap.add_argument("--pp", default='{"t_mask": 0.4, "t_seed": 0.7, "min_seed": 30}')
    a = ap.parse_args()
    sys.path.insert(0, str(Path(a.data) / "code"))
    import train_seg as T  # predict / postprocess / build_model, identical to the val runs

    stretch = json.loads((Path(a.data) / "stretch.json").read_text())
    model = T.build_model(a.encoder).cuda().to(memory_format=torch.channels_last)
    model.load_state_dict(torch.load(a.model, map_location="cuda"))
    model.eval()
    pp = json.loads(a.pp)
    out_dir = Path(a.out) / "strip_eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    for aoi in a.aois.split(","):
        t0 = time.time()
        keys = [k for k in list_keys(f"spacenet/SN2_buildings/train/{aoi}/PS-RGB/") +
                list_keys(f"spacenet/SN2_buildings/test_public/{aoi}/PS-RGB/") if k.endswith(".tif")]
        with ThreadPoolExecutor(64) as ex:
            bounds = [b for b in ex.map(chip_bounds, keys) if b]
        cover = unary_union([box(*b) for b in bounds]).buffer(a.buffer_deg)
        log(aoi, len(keys), "SpaceNet chips,", len(bounds), "bounds read", f"{time.time() - t0:.0f}s")
        lo_hi = np.array(stretch[aoi], np.float32)  # (3, 2) per-channel 1st/99th percentile
        vrt = mosaic_vrt(aoi, f"/tmp/{aoi}.vrt")
        res = {"aoi": aoi, "n_spacenet_chips": len(bounds), "buffer_deg": a.buffer_deg, "pp": pp, "windows": []}
        with rasterio.Env(**ENV), rasterio.open(vrt) as src:
            H, W = src.height, src.width
            cand = [(r, c) for r in range(0, H - 650, 650) for c in range(0, W - 650, 650)]
            random.Random(0).shuffle(cand)
            tried = 0
            for r, c in cand:
                if len(res["windows"]) >= a.n:
                    break
                win = Window(c, r, 650, 650)
                wb = src.window_bounds(win)
                if cover.intersects(box(*wb)):
                    continue
                tried += 1
                arr = src.read(window=win).astype(np.float32)  # (3, 650, 650) uint16 values
                z = arr.sum(0) == 0
                if z.mean() > 0.05:
                    continue
                arr = (arr - lo_hi[:, :1, None]) / (lo_hi[:, 1:, None] - lo_hi[:, :1, None])
                img = (np.clip(arr, 0, 1) * 254 + 1).astype(np.uint8)
                img[:, z] = 0
                prob = T.predict(model, img.transpose(1, 2, 0))
                polys = T.postprocess(prob, **pp)
                tr = src.window_transform(win)
                parts = [q for p in polys for q in (p.geoms if p.geom_type == "MultiPolygon" else [p])
                         if q.geom_type == "Polygon" and not q.is_empty]
                geo = [", ".join(f"{x:.9f} {y:.9f}" for x, y in [tr * xy for xy in p.exterior.coords]) for p in parts]
                res["windows"].append({"row": r, "col": c, "bounds": list(wb),
                                       "polys_wkt_geo": [f"POLYGON (({g}))" for g in geo]})
                if len(res["windows"]) % 50 == 0:
                    log(aoi, len(res["windows"]), "windows", f"{time.time() - t0:.0f}s")
        res["mosaic_px"] = [W, H]
        res["windows_tried"] = tried
        (out_dir / f"{aoi}.json").write_text(json.dumps(res))
        log(aoi, "done", len(res["windows"]), "windows", sum(len(w["polys_wkt_geo"]) for w in res["windows"]),
            "buildings", f"{time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
