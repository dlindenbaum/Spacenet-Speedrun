"""Build the streamable assets for the story map (story/index.html): an imagery pyramid of the Vegas WorldView-3
strip and spatially chunked binary vector layers. Everything is served as static files next to the page and
fetched on demand by viewport.

Imagery (WebP, same per-city stretch the model sees):
  img/ov/{r}_{c}.webp    1/16 resolution (4.8 m), whole strip, 2x2 tiles
  img/l4/{r}_{c}.webp    1/4 resolution (1.2 m), 1024 px tiles, loaded when zoomed in
  img/full/{name}.webp   full 0.3 m insets at the story locations
Vectors (binary, one file per layer x chunk on a 4x4 grid; features assigned by centroid):
  vec/{layer}_{i}_{j}.bin   [u32 n][u32 nv][u32 starts(n+1)][u16 xy(2nv), units of 1e-6 deg from chunk origin][u8 attr]*
  manifest.json             bounds, tiles, chunks, origins, attribute schemas, story locations

  python scripts/build_story_assets.py <out_dir>
"""
import csv
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyarrow.parquet as pq
import rasterio
from PIL import Image
from rasterio.windows import Window
from shapely import box, wkt

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "jobs"))
import strip_eval as SE  # noqa: E402

AOI = "AOI_2_Vegas"
UNIT = 1e-6  # deg per quantization step (~0.1 m)
GRID = 4
SIMPLIFY = 2e-6  # deg (~0.2 m)
csv.field_size_limit(10 ** 9)


def stretch_img(arr, lohi):
    z = arr.sum(0) == 0
    x = (arr.astype(np.float32) - lohi[:, :1, None]) / (lohi[:, 1:, None] - lohi[:, :1, None])
    rgb = (np.clip(x, 0, 1) ** 0.9 * 254 + 1).astype(np.uint8)
    a = np.where(z, 0, 255).astype(np.uint8)
    return np.concatenate([rgb, a[None]], 0).transpose(1, 2, 0)


def save(img, path, q=72):
    if img[..., 3].max() == 0:
        return False
    Image.fromarray(img, "RGBA").save(path, "WEBP", quality=q, method=5)
    return True


def main():
    out = Path(sys.argv[1])
    (out / "img/ov").mkdir(parents=True, exist_ok=True)
    (out / "img/l4").mkdir(parents=True, exist_ok=True)
    (out / "img/full").mkdir(parents=True, exist_ok=True)
    (out / "vec").mkdir(parents=True, exist_ok=True)
    lohi = np.array(json.loads(Path("data/sn2/stretch.json").read_text())[AOI], np.float32)
    vrt = SE.mosaic_vrt(AOI, "/tmp/story_vegas.vrt")
    man = {"aoi": AOI, "acquired": "2015-10-22", "unit_deg": UNIT}
    with rasterio.Env(**SE.ENV), rasterio.open(vrt) as src:
        W, H, T = src.width, src.height, src.transform
        lon0, lat0, dlon, dlat = T.c, T.f, T.a, T.e
        man["bounds"] = [lon0, lat0 + H * dlat, lon0 + W * dlon, lat0]
        man["px"] = [W, H]

        def bounds(c0, r0, c1, r1):
            return [lon0 + c0 * dlon, lat0 + r1 * dlat, lon0 + c1 * dlon, lat0 + r0 * dlat]

        # overview 1/16, 2x2 tiles
        ov = []
        hw, hh = (W + 1) // 2, (H + 1) // 2
        for r in range(2):
            for c in range(2):
                w = Window(c * hw, r * hh, min(hw, W - c * hw), min(hh, H - r * hh))
                a = src.read(window=w, out_shape=(3, int(w.height) // 16, int(w.width) // 16))
                if save(stretch_img(a, lohi), out / f"img/ov/{r}_{c}.webp", 70):
                    ov.append({"f": f"img/ov/{r}_{c}.webp",
                               "b": bounds(w.col_off, w.row_off, w.col_off + w.width, w.row_off + w.height)})
        man["overview"] = ov
        print("overview", len(ov), flush=True)

        # 1/4 pyramid, 1024 px output tiles (4096 source px)
        S = 4096
        jobs = [(r, c) for r in range(0, H, S) for c in range(0, W, S)]

        def l4(rc):
            r, c = rc
            with rasterio.Env(**SE.ENV), rasterio.open(vrt) as s:
                w = Window(c, r, min(S, W - c), min(S, H - r))
                a = s.read(window=w, out_shape=(3, max(1, int(w.height) // 4), max(1, int(w.width) // 4)))
            name = f"img/l4/{r // S}_{c // S}.webp"
            if save(stretch_img(a, lohi), out / name, 70):
                return {"f": name, "b": bounds(c, r, c + w.width, r + w.height)}

        with ThreadPoolExecutor(12) as ex:
            man["l4"] = [t for t in ex.map(l4, jobs) if t]
        print("l4 tiles", len(man["l4"]), flush=True)

        # full-resolution insets at story locations (lon, lat of centre), 2048 px = 614 m
        story = {
            "val_tile": (-115.23469, 36.13444),  # val chip AOI_2_Vegas_img2640 (overlay figure)
            "outside_nw": (-115.25509, 36.23706),  # densest area no SpaceNet chip covers
        }
        man["full"] = {}
        for name, (lon, lat) in story.items():
            c = int((lon - lon0) / dlon) - 1024
            r = int((lat - lat0) / dlat) - 1024
            a = src.read(window=Window(c, r, 2048, 2048), boundless=True, fill_value=0)
            if save(stretch_img(a, lohi), out / f"img/full/{name}.webp", 78):
                man["full"][name] = {"f": f"img/full/{name}.webp", "b": bounds(c, r, c + 2048, r + 2048),
                                     "center": [lon, lat]}
        print("insets", list(man["full"]), flush=True)

    # ---------------------------------------------------------------- vectors
    xmin, ymin, xmax, ymax = man["bounds"]
    gx, gy = (xmax - xmin) / GRID, (ymax - ymin) / GRID

    layers = {}
    g = gpd.read_parquet("data/footprints/aoi_2_vegas_buildings.parquet")
    split_codes = {"train": 0, "val": 1, "test_public": 2, "outside": 3}
    layers["model"] = (g.geometry.values, {
        "conf": (np.clip(g.confidence.values * 255, 0, 255)).astype(np.uint8),
        "split": g.spacenet_split.map(split_codes).values.astype(np.uint8),
        "area": np.clip(np.round(g.area_m2.values / 10), 0, 255).astype(np.uint8)})  # 10 m^2 units, capped
    rows = list(csv.DictReader(open(f"data/sn2/labels/{AOI}_solutions.csv")))
    val = set(json.loads(Path("data/sn2/splits.json").read_text())["val"])
    gg, gs = [], []
    for r in rows:
        if "EMPTY" in r["PolygonWKT_Geo"]:
            continue
        p = wkt.loads(re.sub(r" 0([,)])", r"\1", r["PolygonWKT_Geo"]))
        if p.is_empty:
            continue
        gg.append(p)
        gs.append(1 if r["ImageId"] in val else 0)
    layers["gt"] = (np.array(gg, dtype=object), {"split": np.array(gs, np.uint8)})
    t = pq.read_table("data/overture/AOI_2_Vegas_mosaic.parquet", columns=["src", "wkt"]).to_pydict()
    og = [wkt.loads(w) for w in t["wkt"]]
    layers["overture"] = (np.array(og, dtype=object),
                          {"src": np.array([0 if s == "OpenStreetMap" else 1 for s in t["src"]], np.uint8)})
    chips = json.loads(Path("data/story_chips.json").read_text())
    layers["chips"] = (np.array([box(*c["b"]) for c in chips], dtype=object),
                       {"split": np.array([split_codes[c["s"]] for c in chips], np.uint8)})

    man["layers"] = {}
    for name, (geoms, attrs) in layers.items():
        import shapely
        geoms = shapely.simplify(np.asarray(geoms, dtype=object), SIMPLIFY, preserve_topology=True)
        cen = shapely.centroid(geoms)
        cx, cy = shapely.get_x(cen), shapely.get_y(cen)
        ok = (cx >= xmin) & (cx < xmax) & (cy >= ymin) & (cy < ymax)
        ci = np.clip(((cx - xmin) / gx).astype(int), 0, GRID - 1)
        cj = np.clip(((ymax - cy) / gy).astype(int), 0, GRID - 1)
        chunks = []
        grid = 1 if name == "chips" else GRID
        for i in range(grid):
            for j in range(grid):
                sel = np.nonzero(ok & ((ci == i) & (cj == j) if grid > 1 else ok))[0]
                if not len(sel):
                    continue
                ox = xmin + i * gx - 0.01 if grid > 1 else xmin - 0.01
                oy = ymax - (j + 1) * gy - 0.01 if grid > 1 else ymin - 0.01
                starts, xy = [0], []
                for k in sel:
                    p = geoms[k]
                    if p.geom_type == "MultiPolygon":
                        p = max(p.geoms, key=lambda q: q.area)
                    ring = np.asarray(p.exterior.coords)[:, :2]
                    q = np.round((ring - [ox, oy]) / UNIT)
                    if q.min() < 0 or q.max() > 65535:
                        q = np.clip(q, 0, 65535)
                    xy.append(q.astype(np.uint16))
                    starts.append(starts[-1] + len(ring))
                xy = np.concatenate(xy)
                f = out / "vec" / f"{name}_{i}_{j}.bin"
                with open(f, "wb") as fh:
                    fh.write(np.array([len(sel), len(xy)], np.uint32).tobytes())
                    fh.write(np.array(starts, np.uint32).tobytes())
                    fh.write(xy.tobytes())
                    for k in attrs:
                        fh.write(attrs[k][sel].tobytes())
                cb = [float(shapely.bounds(geoms[sel]).min(0)[0]), float(shapely.bounds(geoms[sel]).min(0)[1]),
                      float(shapely.bounds(geoms[sel]).max(0)[2]), float(shapely.bounds(geoms[sel]).max(0)[3])]
                chunks.append({"f": f"vec/{f.name}", "origin": [ox, oy], "n": int(len(sel)), "nv": int(len(xy)),
                               "b": cb, "bytes": f.stat().st_size})
        man["layers"][name] = {"attrs": list(attrs), "chunks": chunks, "n": int(sum(c["n"] for c in chunks))}
        print(name, man["layers"][name]["n"], "features", len(chunks), "chunks",
              round(sum(c["bytes"] for c in chunks) / 1e6, 1), "MB", flush=True)
    (out / "manifest.json").write_text(json.dumps(man))


if __name__ == "__main__":
    main()
