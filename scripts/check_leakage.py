"""Check that no test tile's ground footprint is covered by any training image (SN6, SN8).
SN4 is checked by location id (0 of 40 test locations appear in train)."""
import sys, re, io, json
sys.path.insert(0, "scripts")
import prepare_xds as P
import rasterio, pandas as pd
from rasterio.warp import transform_bounds
from shapely.geometry import box
from shapely.strtree import STRtree
from concurrent.futures import ThreadPoolExecutor
import os
os.environ["GDAL_DISABLE_READDIR_ON_OPEN"]="EMPTY_DIR"
def bounds(key):
    for t in range(4):
        try:
            with rasterio.open(f"/vsicurl/{P.S3}/{key}") as r:
                return box(*transform_bounds(r.crs, "EPSG:4326", *r.bounds))
        except Exception as e: err=e
    print("fail", key, err); return None
def check(name, test, train):
    with ThreadPoolExecutor(32) as ex:
        bt = list(ex.map(bounds, test)); br = [b for b in ex.map(bounds, train) if b is not None]
    tree = STRtree(br); n=0; fr=[]
    for b in bt:
        if b is None: continue
        hits=[br[i] for i in tree.query(b)]
        a=sum(b.intersection(x).area for x in hits)/b.area
        fr.append(min(a,1.0)); n+= a>0.05
    print(name, "test tiles",len(bt),"with >5% area covered by train images:",n, "mean covered frac %.3f"%(sum(fr)/len(fr)))
base = "spacenet/SN6_buildings/train/AOI_11_Rotterdam/"
keys = [k for k in P.list_keys(base + "PS-RGB/") if k.endswith(".tif")]
tid = lambda k: re.search(r"PS-RGB_(.*)\.tif", k).group(1)
# current geographic split, rebuilt from the dataset's own ids
sp = json.load(open("data/xds/sn6/splits.json"))
idk = {"sn6_" + tid(k): k for k in keys}
base_id = lambda s: s.rsplit("_s", 1)[0] if s.rsplit("_s", 1)[-1].isdigit() else s
test = sorted({idk[base_id(i)] for i in sp["test"]}); train = sorted({idk[base_id(i)] for i in sp["train"]})
check("SN6", test, train)
for reg in ["Germany_Training_Public", "Louisiana-East_Training_Public"]:
    m = pd.read_csv(io.BytesIO(P.fetch(f"spacenet/SN8_floods/{reg}/{reg}_label_image_mapping.csv")))
    te,tr=[],[]
    for lab, pre in zip(m["label"], m["pre-event image"]):
        (te if P.h(lab, reg) < 0.25*1000 else tr).append(f"spacenet/SN8_floods/{reg}/PRE-event/{pre}")
    check("SN8 "+reg, te, tr)
