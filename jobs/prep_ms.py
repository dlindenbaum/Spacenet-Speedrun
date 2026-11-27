# /// script
# dependencies = ["numpy", "rasterio"]
# ///
"""HF Job (CPU): SpaceNet-2 8-band pansharpened multispectral (PS-MS, WorldView-3, 0.3 m, 650x650) ->
uint8 shards aligned 1:1 with the RGB shards (/sn2/shards/<AOI>.json id order).

Same stretch as scripts/prepare_dataset.py: per-AOI, per-band 1st/99th percentile of non-zero pixels over a
200-tile sample, mapped to 1..255 (0 = no-data). Band order is WV-3's:
  0 coastal  1 blue  2 green  3 yellow  4 red  5 red-edge  6 NIR1  7 NIR2   (RGB = 4,2,1)

Writes /out/shards_ms/<AOI>.npy (N, 650, 650, 8) + <AOI>.json + stretch.json, and a sanity check that the
PS-MS red/green/blue bands match the PS-RGB shards (correlation per tile).
"""
import io
import json
import os
import shutil
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio

S3 = "https://spacenet-dataset.s3.amazonaws.com/spacenet/SN2_buildings/train"
AOIS = ["AOI_2_Vegas", "AOI_3_Paris", "AOI_4_Shanghai", "AOI_5_Khartoum"]
SN2, OUT, TMP = Path("/sn2"), Path("/out/shards_ms"), Path("/tmp/shards_ms")


def fetch(aoi, iid, tries=6):
    url = f"{S3}/{aoi}/PS-MS/SN2_buildings_train_{aoi}_PS-MS_{iid[len(aoi) + 1:]}.tif"
    for t in range(tries):
        try:
            b = urllib.request.urlopen(url, timeout=300).read()
            with rasterio.open(io.BytesIO(b)) as r:
                return r.read()  # (8, 650, 650) uint16
        except Exception:
            if t == tries - 1:
                raise


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    stretch, check = {}, {}
    for aoi in AOIS:
        ids = json.loads((SN2 / "shards" / f"{aoi}.json").read_text())
        rng = np.random.default_rng(0)
        sample = rng.choice(ids, size=min(200, len(ids)), replace=False)
        with ThreadPoolExecutor(64) as ex:
            arrs = list(ex.map(lambda i: fetch(aoi, i), sample))
        px = np.concatenate([a.reshape(8, -1)[:, ::7] for a in arrs], axis=1)
        valid = px.sum(0) > 0
        lo = np.percentile(px[:, valid], 1, axis=1)
        hi = np.percentile(px[:, valid], 99, axis=1)
        stretch[aoi] = [[float(a), float(b)] for a, b in zip(lo, hi)]
        print(aoi, len(ids), "tiles; stretch", stretch[aoi], flush=True)

        arr = np.lib.format.open_memmap(TMP / f"{aoi}.npy", mode="w+", dtype=np.uint8, shape=(len(ids), 650, 650, 8))

        def proc(k):
            a = fetch(aoi, ids[k]).astype(np.float32)
            z = a.sum(0) == 0
            a = (a - lo[:, None, None]) / (hi - lo)[:, None, None]
            a = (np.clip(a, 0, 1) * 254 + 1).astype(np.uint8)
            a[:, z] = 0
            arr[k] = a.transpose(1, 2, 0)

        with ThreadPoolExecutor(64) as ex:
            for n, _ in enumerate(ex.map(proc, range(len(ids)))):
                if (n + 1) % 1000 == 0:
                    print(aoi, n + 1, "/", len(ids), flush=True)
        arr.flush()
        rgb = np.load(SN2 / "shards" / f"{aoi}.npy", mmap_mode="r")
        cs = [np.corrcoef(arr[k][..., [4, 2, 1]].ravel().astype(np.float32), rgb[k].ravel().astype(np.float32))[0, 1]
              for k in range(0, len(ids), max(1, len(ids) // 20))]
        check[aoi] = [round(float(np.min(cs)), 4), round(float(np.mean(cs)), 4)]
        print(aoi, "PS-MS RGB vs PS-RGB corr (min, mean)", check[aoi], flush=True)
        del arr
        shutil.copy(TMP / f"{aoi}.npy", OUT / f"{aoi}.npy")
        shutil.copy(SN2 / "shards" / f"{aoi}.json", OUT / f"{aoi}.json")
        os.remove(TMP / f"{aoi}.npy")
    (OUT / "stretch.json").write_text(json.dumps({"stretch": stretch, "rgb_corr_min_mean": check}, indent=1))
    print("done")


if __name__ == "__main__":
    main()
