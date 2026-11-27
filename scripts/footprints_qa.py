"""QA for a strip-wide footprint GeoParquet (jobs/strip_footprints.py).

1. Seamless tiling check: clip the product to every held-out SpaceNet val chip and score it against the
   SpaceNet labels with the repo metric. This should match the per-chip model result (0.892 F1 for Vegas); a drop
   would mean the 1300 px windows / core-centroid rule changed the output.
2. Agreement with Overture Maps over the whole strip and per spacenet_split (object + pixel F1 on 650 px tiles).
3. Duplicate check: pairs of output polygons overlapping by more than 1 m^2.

  python scripts/footprints_qa.py data/footprints/aoi_2_vegas_buildings.parquet AOI_2_Vegas
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import geopandas as gpd
import numpy as np
from rasterio.transform import from_bounds
from shapely import box

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import overture_eval as O  # noqa: E402
import strip_vs_overture as SV  # noqa: E402
from sn2.metric import load_solution_csv  # noqa: E402


def main():
    f, aoi = Path(sys.argv[1]), sys.argv[2]
    g = gpd.read_parquet(f)
    tree = g.sindex
    geoms = g.geometry.values
    res = {"file": f.name, "buildings": len(g), "by_split": g.spacenet_split.value_counts().to_dict()}

    # 1. product vs SpaceNet GT on val chips
    val = [i for i in json.loads(Path("data/sn2/splits.json").read_text())["val"] if i.startswith(aoi + "_")]
    gt = load_solution_csv(f"data/sn2/labels/{aoi}_solutions.csv")
    with ThreadPoolExecutor(32) as ex:
        inv = dict(ex.map(O.tile_affine, val))

    def product_tile(iv):
        fwd = ~iv
        x0, y0 = fwd * (0, 0)
        x1, y1 = fwd * (650, 650)
        cand = tree.query(box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
        return O.to_pixels(list(geoms[cand]), iv)

    acc = SV.Acc()
    for i in val:
        acc.add(gt.get(i, []), product_tile(inv[i]))
    res["val_chips_vs_spacenet_gt"] = {"n_chips": len(val), **acc.out()}
    print("val chips vs SpaceNet GT", res["val_chips_vs_spacenet_gt"], flush=True)

    # 2. agreement with Overture over the strip, on a 650 px grid of tiles, per split
    ov = SV.Overture(aoi, f"data/overture/{aoi}_mosaic.parquet")  # whole-strip extract
    xmin, ymin, xmax, ymax = g.total_bounds
    step = 650 * 2.7e-6
    accs = {}
    rng = np.random.default_rng(0)
    tiles = [(x, y) for x in np.arange(xmin, xmax, step) for y in np.arange(ymin, ymax, step)]
    for k in rng.permutation(len(tiles))[:3000]:  # random sample of strip tiles
        x, y = tiles[k]
        iv = ~from_bounds(x, y, x + step, y + step, 650, 650)
        pred = product_tile(iv)
        if not pred:
            continue
        cand = tree.query(box(x, y, x + step, y + step))
        split = g.spacenet_split.values[cand]
        s = "outside" if (split == "outside").mean() > 0.5 else "spacenet_chips"
        accs.setdefault(s, SV.Acc()).add(ov.tile(iv), pred)
    res["vs_overture"] = {k: v.out() for k, v in accs.items()}
    print("vs Overture", res["vs_overture"], flush=True)

    # 3. duplicates
    u = g.to_crs(g.estimate_utm_crs())
    a, b = u.sindex.query(u.geometry.values, predicate="intersects")
    m = a < b
    ov_area = u.geometry.values[a[m]].intersection(u.geometry.values[b[m]]).area
    res["overlapping_pairs_gt_1m2"] = int((ov_area > 1).sum())
    res["overlapping_pairs_gt_50pct"] = int((ov_area > 0.5 * np.minimum(u.geometry.values[a[m]].area,
                                                                         u.geometry.values[b[m]].area)).sum())
    print("overlaps", res["overlapping_pairs_gt_1m2"], res["overlapping_pairs_gt_50pct"])
    Path("results/footprints").mkdir(parents=True, exist_ok=True)
    Path(f"results/footprints/{aoi.lower()}_qa.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
