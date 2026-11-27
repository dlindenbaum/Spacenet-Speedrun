# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "torch>=2.4", "timm>=1.0.9", "segmentation-models-pytorch>=0.4", "opencv-python-headless",
#   "shapely>=2", "scikit-image", "numpy", "rasterio", "geopandas>=1.0", "pyproj", "pyarrow>=15",
# ]
# ///
"""HF Job (GPU): building footprints for an entire WorldView-3 strip -> one cloud-native GeoParquet.

Model: the SN2 ConvNeXt-B U-Net (F1 0.892 on held-out Vegas val tiles), same TTA and post-processing as the
validated result. The strip is pixel-identical to the SpaceNet chips, so preprocessing is unchanged.

Seamless tiling: 1300 px windows (4 chips) on an 1100 px core grid with a 100 px (30 m) margin. Each window
keeps only buildings whose centroid falls inside its core, so every building is produced exactly once and
buildings up to ~60 m across are never cut at a window edge.

Output (GeoParquet 1.1, EPSG:4326): one row per building with area_m2, confidence (mean predicted building
probability inside the polygon), spacenet_split (train / val / test_public / outside: which SpaceNet chip, if any,
contains the centroid), acquisition and model provenance. Rows are sorted along a Hilbert curve and carry a bbox
covering column, so DuckDB / GeoPandas / GDAL read only the row groups that intersect a query box over HTTP.
"""
import argparse
import json
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
import rasterio
import torch
from rasterio.windows import Window

CORE, MARGIN = 1100, 100
WIN = CORE + 2 * MARGIN  # 1300
SCALE = 1.5
BIG = int(WIN * SCALE)  # 1950
PAD = 1952  # multiple of 32


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- post-processing (CPU worker)
def polygons(args):
    """train_seg.postprocess (marker watershed) + per-building confidence, keeping only core-centroid buildings.
    Returns [(pixel-ring (N,2) in window px, confidence)]."""
    prob, t_mask, t_seed, min_seed, x_lo, x_hi, y_lo, y_hi = args
    from skimage.segmentation import watershed

    body, border, contact = [p.astype(np.float32) for p in prob]
    seed = (body * (1 - border) * (1 - contact)) > t_seed
    n, markers = cv2.connectedComponents(seed.astype(np.uint8), connectivity=4)
    if n <= 1:
        return []
    sizes = np.bincount(markers.ravel())
    small = sizes < min_seed
    small[0] = False
    markers[small[markers]] = 0
    lab = watershed(-body, markers, mask=(body > t_mask) | (markers > 0))
    nl = lab.max() + 1
    cnt = np.bincount(lab.ravel(), minlength=nl)
    conf = np.bincount(lab.ravel(), weights=body.ravel(), minlength=nl) / np.maximum(cnt, 1)
    ys, xs = np.nonzero(lab)
    cy = np.bincount(lab[ys, xs], weights=ys, minlength=nl) / np.maximum(cnt, 1) / SCALE
    cx = np.bincount(lab[ys, xs], weights=xs, minlength=nl) / np.maximum(cnt, 1) / SCALE
    from scipy.ndimage import find_objects

    out = []
    for l, sl in enumerate(find_objects(lab), start=1):
        if sl is None or cnt[l] == 0 or not (x_lo <= cx[l] < x_hi and y_lo <= cy[l] < y_hi):
            continue
        sub = (lab[sl] == l).astype(np.uint8)
        cs, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE, offset=(sl[1].start, sl[0].start))
        if not cs:
            continue
        c = cv2.approxPolyDP(max(cs, key=cv2.contourArea), 1.0, True)
        if len(c) < 3:
            continue
        ring = (c[:, 0, :].astype(np.float64) + 0.5) / SCALE
        if cv2.contourArea(c.astype(np.float32)) / SCALE ** 2 < 20:  # same 20 px^2 floor as the metric
            continue
        out.append((ring, float(conf[l])))
    return out


# ---------------------------------------------------------------- GPU
@torch.no_grad()
def predict(model, img, mean, std):
    """img uint8 (WIN, WIN, 3) -> probs float16 (3, BIG, BIG); 4-way flip/transpose TTA, one view at a time."""
    im = cv2.resize(img, (BIG, BIG), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255.0
    x = np.zeros((PAD, PAD, 3), np.float32)
    x[:BIG, :BIG] = (im - mean) / std
    x = torch.from_numpy(x.transpose(2, 0, 1)).cuda()[None].contiguous(memory_format=torch.channels_last)
    acc = 0
    for f, inv in ((lambda t: t, lambda t: t), (lambda t: t.flip(-1), lambda t: t.flip(-1)),
                   (lambda t: t.flip(-2), lambda t: t.flip(-2)),
                   (lambda t: t.transpose(-1, -2), lambda t: t.transpose(-1, -2))):
        with torch.autocast("cuda", dtype=torch.bfloat16):
            acc = acc + inv(model(f(x).contiguous(memory_format=torch.channels_last)).float().sigmoid())
    return (acc[0] / 4)[:, :BIG, :BIG].half().cpu().numpy()


def dedupe(gdf, utm):
    """Drop near-duplicates: a building whose centroid sits near a core boundary can be claimed by two windows that
    segment it slightly differently. For every pair overlapping by > 50 % of the smaller polygon, keep the one
    with higher confidence."""
    u = gdf.geometry.to_crs(utm).values
    import shapely

    a, b = shapely.STRtree(u).query(u, predicate="intersects")
    m = a < b
    a, b = a[m], b[m]
    inter = shapely.area(shapely.intersection(u[a], u[b]))
    dup = inter > 0.5 * np.minimum(shapely.area(u[a]), shapely.area(u[b]))
    conf = gdf["confidence"].values
    drop = set()
    for i, j in zip(a[dup], b[dup]):
        if i in drop or j in drop:
            continue
        drop.add(j if conf[i] >= conf[j] else i)
    return gdf.drop(index=gdf.index[sorted(drop)]).reset_index(drop=True), len(drop)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/sn2")
    ap.add_argument("--out", default="/out")
    ap.add_argument("--aoi", default="AOI_2_Vegas")
    ap.add_argument("--model", default="/out/convnextb_unet_20k/model.pt")
    ap.add_argument("--encoder", default="tu-convnext_base.fb_in22k_ft_in1k")
    ap.add_argument("--pp", default='{"t_mask": 0.4, "t_seed": 0.7, "min_seed": 30}')
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0, help="debug: only the first N windows")
    ap.add_argument("--price_per_h", type=float, default=1.5)
    a = ap.parse_args()
    t_start = time.time()
    sys.path.insert(0, str(Path(a.data) / "code"))
    import strip_eval as SE
    import train_seg as T

    pp = json.loads(a.pp)
    stretch = np.array(json.loads((Path(a.data) / "stretch.json").read_text())[a.aoi], np.float32)
    model = T.build_model(a.encoder).cuda().to(memory_format=torch.channels_last)
    model.load_state_dict(torch.load(a.model, map_location="cuda"))
    model.eval()
    mean, std = T.MEAN.astype(np.float32), T.STD.astype(np.float32)

    # SpaceNet chips (for the spacenet_split attribute)
    splits = json.loads((Path(a.data) / "splits.json").read_text())
    val_ids = set(splits["val"])
    chip_keys = [(k, "train") for k in SE.list_keys(f"spacenet/SN2_buildings/train/{a.aoi}/PS-RGB/") if k.endswith(".tif")]
    chip_keys += [(k, "test_public") for k in SE.list_keys(f"spacenet/SN2_buildings/test_public/{a.aoi}/PS-RGB/")
                  if k.endswith(".tif")]

    def chip(kv):
        k, s = kv
        img = re.search(r"_(img\d+)\.tif$", k).group(1)
        if s == "train" and f"{a.aoi}_{img}" in val_ids:
            s = "val"
        return SE.chip_bounds(k), s

    with ThreadPoolExecutor(64) as ex:
        chips = [c for c in ex.map(chip, chip_keys) if c[0]]
    log(len(chips), "SpaceNet chips", {s: sum(c[1] == s for c in chips) for s in ("train", "val", "test_public")})

    vrt = SE.mosaic_vrt(a.aoi, f"/tmp/{a.aoi}.vrt")
    t_setup = time.time() - t_start
    feats, n_windows, n_skipped = [], 0, 0
    t_read = t_gpu = 0.0
    with rasterio.Env(**SE.ENV), rasterio.open(vrt) as src:
        H, W = src.height, src.width
        tr0 = src.transform
        cores = [(r, c) for r in range(0, H, CORE) for c in range(0, W, CORE)]
        if a.limit:
            cores = cores[len(cores) // 2: len(cores) // 2 + a.limit]
        log(f"mosaic {W} x {H} px, {len(cores)} windows of {WIN} px (core {CORE})")

        def read(rc):
            r, c = rc
            t = time.time()
            with rasterio.Env(**SE.ENV), rasterio.open(vrt) as s:
                arr = s.read(window=Window(c - MARGIN, r - MARGIN, WIN, WIN), boundless=True, fill_value=0)
            return rc, arr, time.time() - t

        pool = ProcessPoolExecutor(a.workers)
        pending = []
        with ThreadPoolExecutor(8) as rex:
            for i in range(0, len(cores), 16):
                for (r, c), arr, dt in rex.map(read, cores[i:i + 16]):
                    t_read += dt
                    z = arr.sum(0) == 0
                    core_valid = ~z[MARGIN:MARGIN + CORE, MARGIN:MARGIN + CORE]
                    if core_valid.mean() < 0.005:
                        n_skipped += 1
                        continue
                    x = (arr.astype(np.float32) - stretch[:, :1, None]) / (stretch[:, 1:, None] - stretch[:, :1, None])
                    img = (np.clip(x, 0, 1) * 254 + 1).astype(np.uint8)
                    img[:, z] = 0
                    t = time.time()
                    prob = predict(model, np.ascontiguousarray(img.transpose(1, 2, 0)), mean, std)
                    t_gpu += time.time() - t
                    x_hi = MARGIN + min(CORE, W - c)
                    y_hi = MARGIN + min(CORE, H - r)
                    pending.append(((r, c), pool.submit(polygons, (prob, pp["t_mask"], pp["t_seed"], pp["min_seed"],
                                                                   MARGIN, x_hi, MARGIN, y_hi))))
                    n_windows += 1
                while len(pending) > 3 * a.workers or (pending and pending[0][1].done()):
                    (r, c), f = pending.pop(0)
                    for ring, conf in f.result():
                        gx = (c - MARGIN + ring[:, 0]) * tr0.a + tr0.c
                        gy = (r - MARGIN + ring[:, 1]) * tr0.e + tr0.f
                        feats.append((np.stack([gx, gy], 1), conf))
                if (i // 16) % 10 == 0:
                    el = time.time() - t_start
                    log(f"{min(i + 16, len(cores))}/{len(cores)} windows ({n_windows} with imagery), {len(feats)} buildings, "
                        f"read {t_read:.0f}s gpu {t_gpu:.0f}s wall {el:.0f}s")
        for (r, c), f in pending:
            for ring, conf in f.result():
                gx = (c - MARGIN + ring[:, 0]) * tr0.a + tr0.c
                gy = (r - MARGIN + ring[:, 1]) * tr0.e + tr0.f
                feats.append((np.stack([gx, gy], 1), conf))
        pool.shutdown()
    t_infer = time.time() - t_start - t_setup
    log(f"inference done: {len(feats)} buildings from {n_windows} windows ({n_skipped} empty skipped), {t_infer:.0f}s")

    # ---------------------------------------------------------------- GeoParquet
    import geopandas as gpd
    import pyproj
    from shapely import Polygon, STRtree, box, make_valid

    t = time.time()
    geoms = []
    for ring, _ in feats:
        p = Polygon(ring)
        if not p.is_valid:
            p = make_valid(p)
            if p.geom_type != "Polygon":
                p = max((g for g in getattr(p, "geoms", []) if g.geom_type == "Polygon"), key=lambda g: g.area,
                        default=None)
        geoms.append(p)
    keep = [i for i, g in enumerate(geoms) if g is not None and not g.is_empty]
    gdf = gpd.GeoDataFrame({"confidence": np.array([feats[i][1] for i in keep], np.float32)},
                           geometry=[geoms[i] for i in keep], crs="EPSG:4326")
    utm = pyproj.CRS.from_user_input(gdf.estimate_utm_crs())
    gdf, n_dup = dedupe(gdf, utm)
    log("removed", n_dup, "near-duplicate polygons at window-core boundaries")
    gdf["area_m2"] = gdf.to_crs(utm).area.astype(np.float32).round(1)
    tree = STRtree([box(*b) for b, _ in chips])
    cent = gdf.geometry.centroid
    hit = tree.query(cent.values, predicate="within")
    split = np.full(len(gdf), "outside", dtype=object)
    split[hit[0]] = np.array([chips[j][1] for j in hit[1]], dtype=object)
    gdf["spacenet_split"] = split
    gdf = gdf.iloc[np.argsort(gdf.hilbert_distance().values, kind="stable")].reset_index(drop=True)
    gdf.insert(0, "id", [f"{a.aoi.lower()}-{i:07d}" for i in range(len(gdf))])
    gdf["acquired"] = np.datetime64("2015-10-22T18:36:56", "s")
    gdf["source_image"] = "WV3 056155973040_01 (SpaceNet AOI_2_Vegas PS-RGB)"
    gdf["model"] = "SN2 ConvNeXt-B U-Net (convnextb_unet_20k), val F1 0.892 (Vegas)"
    gdf = gdf[["id", "geometry", "area_m2", "confidence", "spacenet_split", "acquired", "source_image", "model"]]
    out = Path(a.out) / "strip_footprints"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"{a.aoi.lower()}_buildings.parquet"
    gdf.to_parquet(f, compression="zstd", write_covering_bbox=True, schema_version="1.1.0", row_group_size=20000)
    t_write = time.time() - t
    wall = time.time() - t_start
    stats = {"aoi": a.aoi, "buildings": len(gdf), "duplicates_removed": n_dup, "windows": n_windows, "empty_windows_skipped": n_skipped,
             "mosaic_px": [W, H],
             "by_split": gdf["spacenet_split"].value_counts().to_dict(),
             "time_s": {"setup": round(t_setup), "inference_and_postprocess": round(t_infer),
                        "read_io_sum": round(t_read), "gpu_sum": round(t_gpu), "geoparquet": round(t_write),
                        "wall": round(wall)},
             "flavor_price_per_h": a.price_per_h, "compute_cost_usd": round(wall / 3600 * a.price_per_h, 2),
             "file_mb": round(f.stat().st_size / 1e6, 1), "utm_crs_for_area": utm.to_string()}
    (out / f"{a.aoi.lower()}_buildings.stats.json").write_text(json.dumps(stats, indent=1))
    log(json.dumps(stats))


if __name__ == "__main__":
    main()
