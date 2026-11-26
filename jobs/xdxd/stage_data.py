"""Lay out SpaceNet-2 in the 2017 release structure XD_XD's scripts expect, using OUR split:
  train (our `train` ids) -> /data/train/AOI_N_City_Train/{RGB-PanSharpen,MUL-PanSharpen,summaryData}
  test  (our `val` ids)   -> /data/test/AOI_N_City_Test/{RGB-PanSharpen,MUL-PanSharpen}
Imagery is the original 16-bit PS-RGB (= RGB-PanSharpen), 8-band PS-MS (= MUL-PanSharpen) and PAN from S3
(v16 reads PAN only for tile bounds)."""
import argparse
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

S3 = "https://spacenet-dataset.s3.amazonaws.com/spacenet/SN2_buildings/train"
AOIS = ["AOI_2_Vegas", "AOI_3_Paris", "AOI_4_Shanghai", "AOI_5_Khartoum"]


def get(url, dst, tries=6):
    if dst.exists() and dst.stat().st_size > 0:
        return
    for i in range(tries):
        try:
            b = urllib.request.urlopen(url, timeout=300).read()
            tmp = dst.with_suffix(".part")
            tmp.write_bytes(b)
            tmp.rename(dst)
            return
        except Exception:
            if i == tries - 1:
                raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sn2", default="/sn2")
    ap.add_argument("--root", default="/data")
    ap.add_argument("--aois", default=",".join(AOIS))
    ap.add_argument("--limit_train", type=int, default=0)
    ap.add_argument("--limit_test", type=int, default=0)
    ap.add_argument("--workers", type=int, default=32)
    a = ap.parse_args()
    sp = json.loads((Path(a.sn2) / "splits.json").read_text())
    jobs = []
    for aoi in a.aois.split(","):
        city_train = [i for i in sp["train"] if i.startswith(aoi + "_")][: a.limit_train or None]
        city_test = [i for i in sp["val"] if i.startswith(aoi + "_")][: a.limit_test or None]
        for kind, ids, d in (("Train", city_train, Path(a.root) / "train" / f"{aoi}_Train"),
                             ("Test", city_test, Path(a.root) / "test" / f"{aoi}_Test")):
            for prod, name in (("PS-RGB", "RGB-PanSharpen"), ("PS-MS", "MUL-PanSharpen"), ("PAN", "PAN")):
                (d / name).mkdir(parents=True, exist_ok=True)
                for iid in ids:
                    img = iid[len(aoi) + 1:]
                    jobs.append((f"{S3}/{aoi}/{prod}/SN2_buildings_train_{aoi}_{prod}_{img}.tif",
                                 d / name / f"{name}_{iid}.tif"))
            if kind == "Train":
                (d / "summaryData").mkdir(exist_ok=True)
                df = pd.read_csv(Path(a.sn2) / "labels" / f"{aoi}_solutions.csv")
                df[df.ImageId.isin(set(ids))].to_csv(d / "summaryData" / f"{aoi}_Train_Building_Solutions.csv",
                                                     index=False)
        print(aoi, "train", len(city_train), "test", len(city_test), flush=True)
    with ThreadPoolExecutor(a.workers) as ex:
        for k, _ in enumerate(ex.map(lambda j: get(*j), jobs)):
            if (k + 1) % 2000 == 0:
                print("downloaded", k + 1, "/", len(jobs), flush=True)
    print("staged", len(jobs), "files")


if __name__ == "__main__":
    main()
