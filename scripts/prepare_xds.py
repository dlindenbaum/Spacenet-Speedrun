"""Build cross-dataset building-footprint eval (and train) sets from the other
SpaceNet releases, in the same form as our SN2 data:

  * RGB uint8, fixed 1-99 % percentile stretch per (dataset, collect)
  * resampled to SN2's ground sample distance (0.3 m) so the SN2 model sees
    buildings at the scale it was trained on
  * cut into sub-tiles of at most MAX_PX pixels; label polygons clipped to the
    sub-tile and expressed in its pixel frame

Output: data/xds/<ds>/images/<id>.png, labels.json {id: [wkt]}, meta.json {id: {...}},
        splits.json {"test": [...], "train": [...]}

SN1 is supported but EXCLUDED from our runs: its labels are poorly aligned / incomplete.
Datasets: sn1 (Rio, WV2 0.5 m; excluded), sn4 (Atlanta, WV2 0.5 m, 27 look angles),
          sn6 (Rotterdam, WV2 0.5 m optical), sn7 (Planet ~4 m monthly mosaics),
          sn8 (Germany + Louisiana-East, Maxar 0.3-0.8 m pre-event)
Splits are by location (SN4: geographic tile, so a location is test at every angle).
"""
import argparse
import hashlib
import io
import json
import math
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
import rasterio
from shapely import affinity, wkt
from shapely.geometry import box, shape, Polygon, MultiPolygon
from tqdm import tqdm

S3 = "https://spacenet-dataset.s3.amazonaws.com"
TARGET_GSD = 0.3
MAX_PX = 1600


def list_keys(prefix, delimiter=None):
    keys, prefixes, token = [], [], None
    while True:
        url = f"{S3}/?list-type=2&prefix={urllib.parse.quote(prefix)}"
        if delimiter:
            url += f"&delimiter={delimiter}"
        if token:
            url += "&continuation-token=" + urllib.parse.quote(token)
        t = urllib.request.urlopen(url, timeout=60).read().decode()
        keys += re.findall(r"<Key>(.*?)</Key>", t)
        prefixes += re.findall(r"<Prefix>(.*?)</Prefix>", t)[1:] if delimiter else []
        m = re.search(r"<NextContinuationToken>(.*?)</NextContinuationToken>", t)
        if not m:
            return (keys, prefixes) if delimiter else keys
        token = m.group(1)


def fetch(key, tries=5):
    for i in range(tries):
        try:
            return urllib.request.urlopen(f"{S3}/{key}", timeout=180).read()
        except Exception:
            if i == tries - 1:
                raise


def read_raster(b):
    with rasterio.MemoryFile(b) as m, m.open() as r:
        return r.read(), r.transform, r.crs


def h(s, salt=""):
    return int(hashlib.md5((s + salt).encode()).hexdigest(), 16) % 1000


def gsd_m(transform, crs, lat=None):
    rx = abs(transform.a)
    if crs is not None and crs.is_geographic:
        return rx * 111320 * math.cos(math.radians(lat))
    if crs is not None and crs.to_epsg() == 3857:
        return rx * math.cos(math.radians(lat))
    return rx


def stretch_params(arrs):
    px = np.concatenate([a[:3].reshape(3, -1)[:, ::5] for a in arrs], axis=1).astype(np.float32)
    valid = px.sum(0) > 0
    lo = np.percentile(px[:, valid], 1, axis=1)
    hi = np.percentile(px[:, valid], 99, axis=1)
    return lo, hi


def to_u8(a, lo, hi):
    a = a[:3].astype(np.float32)
    z = a.sum(0) == 0
    a = (a - lo[:, None, None]) / np.maximum(hi - lo, 1e-6)[:, None, None]
    a = (np.clip(a, 0, 1) * 254 + 1).astype(np.uint8)
    a[:, z] = 0
    return a.transpose(1, 2, 0)


def geo_to_pix(geom, transform):
    inv = ~transform
    return affinity.affine_transform(geom, [inv.a, inv.b, inv.d, inv.e, inv.xoff, inv.yoff])


def polys_of(g):
    if g.is_empty:
        return []
    if not g.is_valid:
        g = g.buffer(0)
    if isinstance(g, Polygon):
        return [g]
    if isinstance(g, MultiPolygon):
        return list(g.geoms)
    if hasattr(g, "geoms"):
        return [x for gg in g.geoms for x in polys_of(gg)]
    return []


class Writer:
    def __init__(self, out, ds):
        self.root = Path(out) / ds
        (self.root / "images").mkdir(parents=True, exist_ok=True)
        self.labels, self.meta = {}, {}

    def add(self, tid, img_u8, polys_pix, gsd, split, **meta):
        """img_u8 HxWx3 at native GSD; polys in its pixel frame. Resample to 0.3 m, sub-tile, save."""
        f = gsd / TARGET_GSD
        H, W = img_u8.shape[:2]
        nW, nH = max(1, round(W * f)), max(1, round(H * f))
        img = cv2.resize(img_u8, (nW, nH), interpolation=cv2.INTER_CUBIC if f > 1 else cv2.INTER_AREA)
        sx, sy = nW / W, nH / H
        polys = [affinity.scale(p, sx, sy, origin=(0, 0)) for p in polys_pix]
        ny, nx = math.ceil(nH / MAX_PX), math.ceil(nW / MAX_PX)
        th, tw = math.ceil(nH / ny), math.ceil(nW / nx)
        out = []
        for iy in range(ny):
            for ix in range(nx):
                x0, y0 = ix * tw, iy * th
                sub = img[y0:y0 + th, x0:x0 + tw]
                if (sub.sum(2) == 0).mean() > 0.5:
                    continue  # mostly no-data
                win = box(x0, y0, x0 + sub.shape[1], y0 + sub.shape[0])
                ps = []
                for p in polys:
                    if p.intersects(win):
                        for q in polys_of(p.intersection(win)):
                            ps.append(affinity.translate(q, -x0, -y0).wkt)
                sid = tid if ny * nx == 1 else f"{tid}_s{iy}{ix}"
                cv2.imwrite(str(self.root / "images" / f"{sid}.png"), cv2.cvtColor(sub, cv2.COLOR_RGB2BGR))
                self.labels[sid] = ps
                self.meta[sid] = dict(split=split, native_gsd=round(gsd, 4), **meta)
                out.append(sid)
        return out

    def close(self):
        (self.root / "labels.json").write_text(json.dumps(self.labels))
        (self.root / "meta.json").write_text(json.dumps(self.meta))
        sp = defaultdict(list)
        for k, m in self.meta.items():
            sp[m["split"]].append(k)
        (self.root / "splits.json").write_text(json.dumps(sp))
        print(self.root.name, {k: len(v) for k, v in sp.items()},
              "buildings", sum(len(self.labels[k]) for k in sp.get("test", [])), "in test")


# ------------------------------------------------------------------ SN1 Rio
def do_sn1(out, n_test, n_train, workers):
    w = Writer(out, "sn1")
    df = pd.read_csv(io.BytesIO(fetch("spacenet/SN1_buildings/train/summarydata/AOI_1_RIO_polygons_solution_3band.csv")))
    lab = defaultdict(list)
    for iid, s in zip(df.ImageId, df.PolygonWKT_Pix):
        lab[iid]
        if isinstance(s, str) and "EMPTY" not in s:
            g = wkt.loads(s)
            lab[iid].append(Polygon([(x, y) for x, y, *_ in g.exterior.coords]))
    ids = sorted(lab)
    test = [i for i in ids if h(i) < 100][:n_test]
    train = [i for i in ids if h(i) >= 100][:n_train]
    sample = [read_raster(fetch(f"spacenet/SN1_buildings/train/3band/3band_{i}.tif"))[0] for i in test[:60]]
    lo, hi = stretch_params(sample)

    def proc(args):
        i, split = args
        a, tr, crs = read_raster(fetch(f"spacenet/SN1_buildings/train/3band/3band_{i}.tif"))
        w.add(i, to_u8(a, lo, hi), lab[i], 0.5, split, city="Rio")

    with ThreadPoolExecutor(workers) as ex:
        list(tqdm(ex.map(proc, [(i, "test") for i in test] + [(i, "train") for i in train]),
                  total=len(test) + len(train), desc="sn1", mininterval=10))
    w.close()


# ------------------------------------------------------------------ SN4 Atlanta off-nadir
def do_sn4(out, n_loc_test, n_loc_train, train_angles, workers):
    w = Writer(out, "sn4")
    base = "spacenet/SN4_buildings/train/AOI_6_Atlanta/"
    _, collects = list_keys(base, delimiter="/")
    collects = [c for c in collects if "nadir" in c]
    gj = list_keys(base + "geojson_buildings/")
    locs = sorted(re.search(r"_(\d+_\d+)\.geojson", k).group(1) for k in gj)
    test_locs = [l for l in locs if h(l) < 100][:n_loc_test]
    train_locs = [l for l in locs if h(l) >= 100][:n_loc_train]
    labels = {}

    def get_lab(l):
        b = fetch(f"{base}geojson_buildings/SN4_buildings_train_AOI_6_Atlanta_geojson_buildings_{l}.geojson")
        if not b.strip():
            return l, []
        g = json.loads(b)
        return l, [shape(f["geometry"]) for f in g["features"] if f["geometry"]]

    with ThreadPoolExecutor(workers) as ex:
        labels = dict(ex.map(get_lab, test_locs + train_locs))
    for c in tqdm(collects, desc="sn4 collects"):
        name = c.rstrip("/").rsplit("/", 1)[1]  # nadir7_catid_...
        ang = int(re.match(r"nadir(\d+)", name).group(1))
        band = "NADIR" if ang <= 25 else ("OFF" if ang < 40 else "VOFF")
        key = lambda l: f"{c}PS-RGBNIR/SN4_buildings_train_AOI_6_Atlanta_{name}_PS-RGBNIR_{l}.tif"
        def safe_read(l):
            try:
                return read_raster(fetch(key(l)))
            except Exception:
                return None

        with ThreadPoolExecutor(workers) as ex:
            rasters = {l: r for l, r in zip(test_locs, ex.map(safe_read, test_locs)) if r is not None}
        lo, hi = stretch_params([r[0] for r in list(rasters.values())[:40]])
        jobs = [(l, "test", rasters[l]) for l in test_locs if l in rasters]
        if name.split("_")[0] in train_angles:
            jobs += [(l, "train", None) for l in train_locs]

        def proc(args):
            l, split, r = args
            r = r if r is not None else safe_read(l)
            if r is None:
                return
            a, tr, crs = r
            polys = [p for g in labels[l] for p in polys_of(geo_to_pix(g, tr))]
            w.add(f"{name.split('_')[0]}-{name[-6:]}_{l}", to_u8(a, lo, hi), polys, abs(tr.a), split,
                  angle=ang, band=band, loc=l)

        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(proc, jobs))
    w.close()


# ------------------------------------------------------------------ SN6 Rotterdam (optical)
def do_sn6(out, n_test, n_train, workers):
    w = Writer(out, "sn6")
    base = "spacenet/SN6_buildings/train/AOI_11_Rotterdam/"
    keys = [k for k in list_keys(base + "PS-RGB/") if k.endswith(".tif")]
    tid = lambda k: re.search(r"PS-RGB_(.*)\.tif", k).group(1)
    test = [k for k in keys if h(tid(k).split("tile_")[1]) < 100][:n_test]
    train = [k for k in keys if h(tid(k).split("tile_")[1]) >= 100][:n_train]
    sample = [read_raster(fetch(k))[0] for k in test[:60]]
    lo, hi = stretch_params(sample)

    def proc(args):
        k, split = args
        a, tr, crs = read_raster(fetch(k))
        g = json.loads(fetch(f"{base}geojson_buildings/SN6_Train_AOI_11_Rotterdam_Buildings_{tid(k)}.geojson"))
        polys = [p for f in g["features"] if f["geometry"] for p in polys_of(geo_to_pix(shape(f["geometry"]), tr))]
        w.add("sn6_" + tid(k).split("tile_")[1], to_u8(a, lo, hi), polys, abs(tr.a), split, city="Rotterdam")

    with ThreadPoolExecutor(workers) as ex:
        list(tqdm(ex.map(proc, [(k, "test") for k in test] + [(k, "train") for k in train]),
                  total=len(test) + len(train), desc="sn6", mininterval=10))
    w.close()


# ------------------------------------------------------------------ SN7 Planet 4 m
def do_sn7(out, crop, workers):
    """One month (the last) per AOI; the crop x crop window with most buildings. Test only."""
    w = Writer(out, "sn7")
    _, aois = list_keys("spacenet/SN7_buildings/train/", delimiter="/")

    def proc(aoi):
        name = aoi.rstrip("/").rsplit("/", 1)[1]
        imgs = sorted(k for k in list_keys(aoi + "images/") if k.endswith(".tif"))
        k = imgs[-1]
        month = re.search(r"global_monthly_(\d{4}_\d{2})", k).group(1)
        lk = f"{aoi}labels_match_pix/global_monthly_{month}_mosaic_{name}_Buildings.geojson"
        try:
            g = json.loads(fetch(lk))
        except Exception:
            return
        a, tr, crs = read_raster(fetch(k))
        lat = (tr * (a.shape[2] / 2, a.shape[1] / 2))[1]
        lat = math.degrees(2 * math.atan(math.exp(lat / 6378137)) - math.pi / 2)
        gsd = gsd_m(tr, crs, lat)
        polys = [p for f in g["features"] if f["geometry"] for p in polys_of(shape(f["geometry"]))]
        if not polys:
            return
        # pick the densest crop window (by building centroids)
        cs = np.array([p.centroid.coords[0] for p in polys])
        best, bxy = -1, (0, 0)
        for y0 in range(0, a.shape[1] - crop + 1, crop // 2):
            for x0 in range(0, a.shape[2] - crop + 1, crop // 2):
                n = ((cs[:, 0] >= x0) & (cs[:, 0] < x0 + crop) & (cs[:, 1] >= y0) & (cs[:, 1] < y0 + crop)).sum()
                if n > best:
                    best, bxy = n, (x0, y0)
        x0, y0 = bxy
        sub = a[:, y0:y0 + crop, x0:x0 + crop]
        lo, hi = stretch_params([a])
        win = box(x0, y0, x0 + crop, y0 + crop)
        ps = [affinity.translate(q, -x0, -y0) for p in polys if p.intersects(win)
              for q in polys_of(p.intersection(win))]
        w.add("sn7_" + name, to_u8(sub, lo, hi), ps, gsd, "test", month=month)

    with ThreadPoolExecutor(workers) as ex:
        list(tqdm(ex.map(proc, aois), total=len(aois), desc="sn7"))
    w.close()


# ------------------------------------------------------------------ SN8 Germany + Louisiana-East pre-event
def do_sn8(out, test_frac, workers):
    w = Writer(out, "sn8")
    regions = ["Germany_Training_Public", "Louisiana-East_Training_Public"]
    jobs = []
    for reg in regions:
        m = pd.read_csv(io.BytesIO(fetch(f"spacenet/SN8_floods/{reg}/{reg}_label_image_mapping.csv")))
        for lab, pre in zip(m["label"], m["pre-event image"]):
            split = "test" if h(lab, reg) < test_frac * 1000 else "train"
            jobs.append((reg, lab, pre, split))
    by_cat = defaultdict(list)
    for j in jobs:
        by_cat[(j[0], j[2].split("_")[0])].append(j)
    for (reg, cat), js in by_cat.items():
        with ThreadPoolExecutor(workers) as ex:
            rs = list(ex.map(lambda j: read_raster(fetch(f"spacenet/SN8_floods/{reg}/PRE-event/{j[2]}")), js))
        lo, hi = stretch_params([r[0] for r in rs[:40]])

        def proc(args):
            (reg_, lab, pre, split), (a, tr, crs) = args
            g = json.loads(fetch(f"spacenet/SN8_floods/{reg_}/annotations/{lab}"))
            polys = [p for f in g["features"] if f["geometry"] and f["geometry"]["type"] in ("Polygon", "MultiPolygon")
                     and f["properties"].get("building") for p in polys_of(geo_to_pix(shape(f["geometry"]), tr))]
            lat = (tr * (a.shape[2] / 2, a.shape[1] / 2))[1]
            w.add(f"sn8_{reg_.split('_')[0]}_{lab[:-8]}", to_u8(a, lo, hi), polys, gsd_m(tr, crs, lat), split,
                  region=reg_.split("_")[0], catid=cat)

        with ThreadPoolExecutor(workers) as ex:
            list(tqdm(ex.map(proc, zip(js, rs)), total=len(js), desc=f"sn8 {reg} {cat}", mininterval=10))
    w.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/xds")
    ap.add_argument("--only", default="sn4,sn6,sn7,sn8")  # sn1 excluded: label quality
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--train", action="store_true", help="also build training subsets")
    args = ap.parse_args()
    tr = args.train
    for ds in args.only.split(","):
        if ds == "sn1":
            do_sn1(args.out, 400, 3000 if tr else 0, args.workers)
        elif ds == "sn4":
            # train on 9 collects spanning all bands (3 nadir, 3 off, 3 very-off)
            do_sn4(args.out, 40, 300 if tr else 0,
                   {"nadir8", "nadir16", "nadir23", "nadir29", "nadir34", "nadir39", "nadir44", "nadir50", "nadir53"},
                   args.workers)
        elif ds == "sn6":
            do_sn6(args.out, 300, 1500 if tr else 0, args.workers)
        elif ds == "sn7":
            do_sn7(args.out, 128, args.workers)
        elif ds == "sn8":
            do_sn8(args.out, 0.25, args.workers)


if __name__ == "__main__":
    main()
