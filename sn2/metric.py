"""SpaceNet 2 building-footprint metric.

A predicted polygon is a true positive if it has IoU >= 0.5 with a ground-truth
polygon of the same tile; matching is one-to-one (greedy, highest IoU first, as
in the official SpaceNet evaluator). F1 is accumulated per AOI over all tiles,
and the overall score is the mean of the per-AOI F1 scores (as in the 2017
challenge leaderboard).
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
from shapely import wkt
from shapely.geometry import Polygon, MultiPolygon
from shapely.strtree import STRtree

MIN_AREA = 20.0  # px; ignore tiny slivers on both sides (GT polygons clipped at tile edges)
AOIS = ["AOI_2_Vegas", "AOI_3_Paris", "AOI_4_Shanghai", "AOI_5_Khartoum"]


def aoi_of(image_id: str) -> str:
    for a in AOIS:
        if image_id.startswith(a):
            return a
    raise ValueError(image_id)


def _clean(polys, min_area=MIN_AREA):
    out = []
    for p in polys:
        if p is None or p.is_empty:
            continue
        if not p.is_valid:
            p = p.buffer(0)
        if isinstance(p, MultiPolygon):
            p = max(p.geoms, key=lambda g: g.area)
        if p.area >= min_area:
            out.append(p)
    return out


def match_tile(gt: list[Polygon], pred: list[Polygon], iou_thr=0.5):
    """Return (tp, fp, fn) for one tile."""
    gt, pred = _clean(gt), _clean(pred)
    if not gt or not pred:
        return 0, len(pred), len(gt)
    tree = STRtree(gt)
    pairs = []
    for i, p in enumerate(pred):
        for j in tree.query(p):
            g = gt[j]
            inter = p.intersection(g).area
            if inter <= 0:
                continue
            iou = inter / (p.area + g.area - inter)
            if iou >= iou_thr:
                pairs.append((iou, i, j))
    pairs.sort(reverse=True)
    used_p, used_g = set(), set()
    tp = 0
    for iou, i, j in pairs:
        if i in used_p or j in used_g:
            continue
        used_p.add(i)
        used_g.add(j)
        tp += 1
    return tp, len(pred) - tp, len(gt) - tp


def score(gt_by_image: dict[str, list], pred_by_image: dict[str, list], image_ids=None):
    """gt/pred: image_id -> list of shapely Polygons (pixel coords).

    Returns dict with per-AOI precision/recall/F1 and overall mean F1.
    Only images in `image_ids` (default: all GT images) are scored; missing
    predictions count as empty.
    """
    image_ids = list(image_ids if image_ids is not None else gt_by_image.keys())
    c = defaultdict(lambda: np.zeros(3, dtype=np.int64))
    for iid in image_ids:
        tp, fp, fn = match_tile(gt_by_image.get(iid, []), pred_by_image.get(iid, []))
        c[aoi_of(iid)] += (tp, fp, fn)
    res = {}
    for a in AOIS:
        if a not in c:
            continue
        tp, fp, fn = c[a]
        p = tp / max(tp + fp, 1)
        r = tp / max(tp + fn, 1)
        f = 2 * p * r / max(p + r, 1e-9)
        res[a] = dict(precision=round(p, 4), recall=round(r, 4), f1=round(f, 4),
                      tp=int(tp), fp=int(fp), fn=int(fn))
    res["overall_f1"] = round(float(np.mean([v["f1"] for v in res.values()])), 4)
    return res


def load_solution_csv(path) -> dict[str, list[Polygon]]:
    """Read a SpaceNet-format CSV (ImageId, BuildingId, PolygonWKT_Pix, ...)."""
    import pandas as pd

    df = pd.read_csv(path)
    out: dict[str, list] = defaultdict(list)
    for iid, w in zip(df["ImageId"], df["PolygonWKT_Pix"]):
        out[iid]  # ensure key exists for empty tiles
        if isinstance(w, str) and "EMPTY" not in w:
            g = wkt.loads(w)
            if g.has_z:
                g = Polygon([(x, y) for x, y, *_ in g.exterior.coords],
                            [[(x, y) for x, y, *_ in r.coords] for r in g.interiors])
            out[iid].append(g)
    return dict(out)


def write_solution_csv(pred_by_image: dict[str, list], path):
    import csv

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ImageId", "BuildingId", "PolygonWKT_Pix", "Confidence"])
        for iid, polys in pred_by_image.items():
            if not polys:
                w.writerow([iid, -1, "POLYGON EMPTY", 1])
            for k, p in enumerate(polys):
                w.writerow([iid, k + 1, p.wkt, 1])


def format_table(res: dict, name="") -> str:
    cols = [a for a in AOIS if a in res]
    s = f"{name:<40s} " + " ".join(f"{res[a]['f1']:.3f}" for a in cols) + f" | {res['overall_f1']:.3f}"
    return s
