"""Score Overture Maps buildings (2026-11-18 release: OSM, Microsoft ML Buildings, Google Open Buildings, ...)
against SpaceNet-2 ground truth on our val tiles, with the same metric as every other model here.

Overture polygons are projected into each tile's pixel grid (affine from the tile's GeoTIFF header) and clipped
to the 650 x 650 tile, exactly like the SpaceNet labels. Reported per city:
  * all Overture buildings, and each source on its own (precision is meaningful per source; recall is not,
    since a source only covers part of the area);
  * the same after the best global shift (search +-3 m), to separate image/footprint misregistration from
    genuinely missing or extra buildings;
  * our ConvNeXt-B predictions scored against Overture, as a cross-check.

  python scripts/overture_eval.py   (needs data/overture/<AOI>.parquet from the DuckDB query in the README)
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import rasterio
from shapely import affinity, box, wkt
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sn2.metric import AOIS, load_solution_csv, match_tile  # noqa: E402

S3 = "https://spacenet-dataset.s3.amazonaws.com/spacenet/SN2_buildings/train"
TILE = box(0, 0, 650, 650)
SHIFTS = [(dx, dy) for dx in range(-10, 11, 2) for dy in range(-10, 11, 2)]  # pixels (0.3 m)


def tile_affine(iid):
    aoi = "_".join(iid.split("_")[:3])
    url = f"/vsicurl/{S3}/{aoi}/PS-RGB/SN2_buildings_train_{aoi}_PS-RGB_{iid[len(aoi) + 1:]}.tif"
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
        with rasterio.open(url) as r:
            return iid, ~r.transform  # geo -> pixel


def to_pixels(geoms, inv):
    m = [inv.a, inv.b, inv.d, inv.e, inv.c, inv.f]
    out = []
    for g in geoms:
        p = affinity.affine_transform(g, m).intersection(TILE)
        if p.is_empty:
            continue
        if p.geom_type == "MultiPolygon":
            out += [q for q in p.geoms]
        elif p.geom_type == "Polygon":
            out.append(p)
        elif hasattr(p, "geoms"):
            out += [q for q in p.geoms if q.geom_type == "Polygon"]
    return out


def f1(c):
    return 2 * c[0] / max(2 * c[0] + c[1] + c[2], 1)


def main():
    val = json.loads(Path("data/sn2/splits.json").read_text())["val"]
    gt, ours = {}, {}
    for a in AOIS:
        gt.update(load_solution_csv(f"data/sn2/labels/{a}_solutions.csv"))
    ours_csv = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if ours_csv and ours_csv.exists():
        ours = load_solution_csv(ours_csv)
    with ThreadPoolExecutor(32) as ex:
        inv = dict(ex.map(tile_affine, val))
    res = {}
    for a in AOIS:
        t = pq.read_table(f"data/overture/{a}.parquet", columns=["src", "wkt"]).to_pydict()
        geoms = [wkt.loads(w) for w in t["wkt"]]
        srcs = np.array(t["src"])
        tree = STRtree(geoms)
        ids = [i for i in val if i.startswith(a + "_")]
        per_tile = {}  # iid -> (pixel polys, their sources)
        for i in ids:
            fwd = ~inv[i]
            x0, y0 = fwd * (0, 0)
            x1, y1 = fwd * (650, 650)
            cand = tree.query(box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
            polys, ps = [], []
            for k in cand:
                for p in to_pixels([geoms[k]], inv[i]):
                    polys.append(p)
                    ps.append(srcs[k])
            per_tile[i] = (polys, ps)
        r = {"n_tiles": len(ids), "overture_buildings": int(sum(len(v[0]) for v in per_tile.values()))}
        # all sources, with and without the best global shift
        best = None
        for dx, dy in SHIFTS:
            c = np.zeros(3)
            for i in ids:
                c += match_tile(gt.get(i, []), [affinity.translate(p, dx, dy) for p in per_tile[i][0]])
            if best is None or f1(c) > f1(best[1]):
                best = ((dx, dy), c)
        c0 = np.zeros(3)
        for i in ids:
            c0 += match_tile(gt.get(i, []), per_tile[i][0])
        r["all"] = {"f1": round(f1(c0), 4), "precision": round(c0[0] / max(c0[0] + c0[1], 1), 4),
                    "recall": round(c0[0] / max(c0[0] + c0[2], 1), 4), "tp_fp_fn": c0.astype(int).tolist()}
        bc = best[1]
        r["all_best_shift"] = {"shift_m": [round(best[0][0] * 0.3, 1), round(best[0][1] * 0.3, 1)],
                               "f1": round(f1(bc), 4), "precision": round(bc[0] / max(bc[0] + bc[1], 1), 4),
                               "recall": round(bc[0] / max(bc[0] + bc[2], 1), 4)}
        # per source: precision of that source's buildings (TP among them / count)
        r["by_source"] = {}
        for s in sorted(set(srcs)):
            c = np.zeros(3)
            for i in ids:
                c += match_tile(gt.get(i, []), [p for p, q in zip(*per_tile[i]) if q == s])
            r["by_source"][s] = {"buildings": int(c[0] + c[1]), "precision": round(c[0] / max(c[0] + c[1], 1), 4),
                                 "gt_recall_share": round(c[0] / max(c[0] + c[2], 1), 4)}
        if ours:
            c = np.zeros(3)
            for i in ids:  # our predictions scored with Overture as the reference
                c += match_tile(per_tile[i][0], ours.get(i, []))
            r["ours_vs_overture_f1"] = round(f1(c), 4)
        res[a] = r
        print(a, json.dumps(r), flush=True)
    for k in ("all", "all_best_shift"):
        res[f"overall_{k}_f1"] = round(float(np.mean([res[a][k]["f1"] for a in AOIS])), 4)
    if ours:
        res["overall_ours_vs_overture_f1"] = round(float(np.mean([res[a]["ours_vs_overture_f1"] for a in AOIS])), 4)
    print({k: v for k, v in res.items() if k.startswith("overall")})
    Path("results/overture").mkdir(parents=True, exist_ok=True)
    Path("results/overture/overture_vs_spacenet_val.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
