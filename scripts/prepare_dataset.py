"""Download SpaceNet 2 PS-RGB training tiles from the public S3 bucket and
convert them to 8-bit PNGs with a fixed per-AOI percentile stretch.

Output layout (OUT/):
  images/<ImageId>.png        650x650 RGB uint8
  labels/<AOI>_solutions.csv  original ground-truth CSV (pixel + geo WKT)
  splits.json                 {"train": [...], "val": [...], "eval_small": [...]}
  stretch.json                per-AOI per-channel (lo, hi) uint16 values
"""
import argparse
import hashlib
import io
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from tqdm import tqdm

BUCKET = "https://spacenet-dataset.s3.amazonaws.com"
PREFIX = "spacenet/SN2_buildings/train"
AOIS = ["AOI_2_Vegas", "AOI_3_Paris", "AOI_4_Shanghai", "AOI_5_Khartoum"]


def list_keys(prefix):
    keys, token = [], None
    while True:
        url = f"{BUCKET}/?list-type=2&prefix={prefix}"
        if token:
            url += "&continuation-token=" + urllib.parse.quote(token)
        t = urllib.request.urlopen(url, timeout=60).read().decode()
        keys += re.findall(r"<Key>(.*?)</Key>", t)
        m = re.search(r"<NextContinuationToken>(.*?)</NextContinuationToken>", t)
        if not m:
            return keys
        token = m.group(1)


def fetch(key, tries=5):
    for i in range(tries):
        try:
            return urllib.request.urlopen(f"{BUCKET}/{key}", timeout=120).read()
        except Exception:
            if i == tries - 1:
                raise


def read_tif(b):
    with rasterio.MemoryFile(b) as m, m.open() as r:
        return r.read()  # (3, H, W) uint16


def image_id(key):
    # SN2_buildings_train_AOI_2_Vegas_PS-RGB_img1.tif -> AOI_2_Vegas_img1
    name = key.rsplit("/", 1)[1]
    m = re.match(r"SN2_buildings_train_(AOI_\d_\w+?)_PS-RGB_(img\d+)\.tif", name)
    return f"{m.group(1)}_{m.group(2)}"


def split_of(iid):
    h = int(hashlib.md5(iid.encode()).hexdigest(), 16) % 100
    return "val" if h < 10 else "train"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/sn2")
    ap.add_argument("--workers", type=int, default=32)
    args = ap.parse_args()
    import urllib.parse  # noqa

    out = Path(args.out)
    (out / "images").mkdir(parents=True, exist_ok=True)
    (out / "labels").mkdir(exist_ok=True)
    stretch, splits = {}, {"train": [], "val": [], "eval_small": []}

    for aoi in AOIS:
        csv_key = f"{PREFIX}/{aoi}/SN2_buildings_train_{aoi}_solutions.csv"
        (out / "labels" / f"{aoi}_solutions.csv").write_bytes(fetch(csv_key))
        keys = sorted(k for k in list_keys(f"{PREFIX}/{aoi}/PS-RGB/") if k.endswith(".tif"))
        print(aoi, len(keys), "tiles")

        # per-AOI stretch from a 200-tile sample: 1st/99th percentile per channel (nonzero px)
        rng = np.random.default_rng(0)
        sample = rng.choice(keys, size=min(200, len(keys)), replace=False)
        with ThreadPoolExecutor(args.workers) as ex:
            arrs = list(ex.map(lambda k: read_tif(fetch(k)), sample))
        px = np.concatenate([a.reshape(3, -1)[:, ::7] for a in arrs], axis=1)
        valid = px.sum(0) > 0
        lo = np.percentile(px[:, valid], 1, axis=1)
        hi = np.percentile(px[:, valid], 99, axis=1)
        stretch[aoi] = [[float(l), float(h)] for l, h in zip(lo, hi)]
        print(aoi, "stretch", stretch[aoi])

        def proc(k):
            iid = image_id(k)
            dst = out / "images" / aoi / f"{iid}.png"
            dst.parent.mkdir(exist_ok=True)
            if not dst.exists():
                a = read_tif(fetch(k)).astype(np.float32)
                z = a.sum(0) == 0
                a = (a - lo[:, None, None]) / (hi - lo)[:, None, None]
                a = (np.clip(a, 0, 1) * 254 + 1).astype(np.uint8)  # keep 0 for no-data
                a[:, z] = 0
                Image.fromarray(a.transpose(1, 2, 0)).save(dst, optimize=False, compress_level=6)
            return iid

        with ThreadPoolExecutor(args.workers) as ex:
            ids = list(tqdm(ex.map(proc, keys), total=len(keys), desc=aoi, mininterval=10))
        for iid in ids:
            splits[split_of(iid)].append(iid)
        # small fixed eval subset for expensive zero-shot models: 25 val tiles per AOI
        val_aoi = sorted(i for i in ids if split_of(i) == "val")
        r = np.random.default_rng(42)
        splits["eval_small"] += sorted(r.choice(val_aoi, 25, replace=False).tolist())

    (out / "splits.json").write_text(json.dumps(splits))
    (out / "stretch.json").write_text(json.dumps(stretch, indent=1))
    print({k: len(v) for k, v in splits.items()})


if __name__ == "__main__":
    main()
