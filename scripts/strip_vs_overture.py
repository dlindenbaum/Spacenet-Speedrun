"""Does the SN2 model hold its accuracy across the full WorldView-3 strips, beyond the SpaceNet chips?

Uses Overture Maps buildings as the reference where SpaceNet labels don't exist. Overture's agreement with
SpaceNet's own labels differs a lot by city (labelling conventions), so every strip number is read against a
calibration on our val tiles, where all three label sets exist:

  val tiles:      model vs SpaceNet GT | Overture vs SpaceNet GT | model vs Overture
  strip windows:                                                  model vs Overture

If model-vs-Overture agreement on the strips matches the val-tile value, the model is about as accurate there
as on val. Two metrics: object F1 (IoU >= 0.5, as everywhere in this repo) and pixel F1 (is the same ground
marked as building; insensitive to how buildings are split into polygons).

  python scripts/strip_vs_overture.py <strip_eval dir> <our val predictions csv>
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
import pyarrow.parquet as pq
from rasterio.transform import from_bounds
from shapely import box, wkt
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import overture_eval as O  # noqa: E402
from sn2.metric import AOIS, load_solution_csv, match_tile  # noqa: E402

TILE_KM2 = (650 * 0.3 / 1000) ** 2


def mask(polys):
    m = np.zeros((650, 650), np.uint8)
    for p in polys:
        if p.geom_type == "Polygon" and not p.is_empty:
            cv2.fillPoly(m, [np.asarray(p.exterior.coords)[:, :2].round().astype(np.int32)], 1)
    return m


class Acc:
    def __init__(self):
        self.obj, self.pix = np.zeros(3), np.zeros(3)

    def add(self, ref, pred):
        self.obj += match_tile(ref, pred)
        a, b = mask(ref).astype(bool), mask(pred).astype(bool)
        self.pix += [(a & b).sum(), (~a & b).sum(), (a & ~b).sum()]

    def out(self):
        f = lambda c: round(float(2 * c[0] / max(2 * c[0] + c[1] + c[2], 1)), 4)  # noqa: E731
        return {"object_f1": f(self.obj), "pixel_f1": f(self.pix)}


class Overture:
    def __init__(self, aoi, path=None):
        t = pq.read_table(path or f"data/overture/{aoi}_all.parquet", columns=["wkt"]).to_pydict()
        self.g = [wkt.loads(w) for w in t["wkt"]]
        self.tree = STRtree(self.g)

    def tile(self, inv):
        fwd = ~inv
        x0, y0 = fwd * (0, 0)
        x1, y1 = fwd * (650, 650)
        cand = self.tree.query(box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
        return O.to_pixels([self.g[k] for k in cand], inv)


def main():
    strip_dir, ours_csv = Path(sys.argv[1]), Path(sys.argv[2])
    val = json.loads(Path("data/sn2/splits.json").read_text())["val"]
    gt, ours = {}, load_solution_csv(ours_csv)
    for a in AOIS:
        gt.update(load_solution_csv(f"data/sn2/labels/{a}_solutions.csv"))
    with ThreadPoolExecutor(32) as ex:
        inv = dict(ex.map(O.tile_affine, val))
    res = {}
    for a in AOIS:
        ov = Overture(a)
        cal = {k: Acc() for k in ("model_vs_gt", "overture_vs_gt", "model_vs_overture")}
        ids = [i for i in val if i.startswith(a + "_")]
        n_model = n_ov = 0
        for i in ids:
            o = ov.tile(inv[i])
            cal["model_vs_gt"].add(gt.get(i, []), ours.get(i, []))
            cal["overture_vs_gt"].add(gt.get(i, []), o)
            cal["model_vs_overture"].add(o, ours.get(i, []))
            n_model += len(ours.get(i, []))
            n_ov += len(o)
        r = {"val_tiles": {"n": len(ids), **{k: v.out() for k, v in cal.items()},
                           "model_buildings_per_km2": round(n_model / (len(ids) * TILE_KM2), 1),
                           "overture_buildings_per_km2": round(n_ov / (len(ids) * TILE_KM2), 1)}}
        f = strip_dir / f"{a}.json"
        if f.exists():
            s = json.loads(f.read_text())
            acc, n_model, n_ov = Acc(), 0, 0
            for w in s["windows"]:
                l, b, rr, t = w["bounds"]
                wi = ~from_bounds(l, b, rr, t, 650, 650)
                pred = O.to_pixels([wkt.loads(p) for p in w["polys_wkt_geo"]], wi)
                o = ov.tile(wi)
                acc.add(o, pred)
                n_model += len(pred)
                n_ov += len(o)
            r["strip_windows"] = {"n": len(s["windows"]), "area_km2": round(len(s["windows"]) * TILE_KM2, 1),
                                  "spacenet_chips_excluded": s["n_spacenet_chips"],
                                  "model_vs_overture": acc.out(),
                                  "model_buildings_per_km2": round(n_model / (len(s["windows"]) * TILE_KM2), 1),
                                  "overture_buildings_per_km2": round(n_ov / (len(s["windows"]) * TILE_KM2), 1)}
        res[a] = r
        print(a, json.dumps(r), flush=True)
    Path("results/overture/strip_vs_overture.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
