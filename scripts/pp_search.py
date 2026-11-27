"""Post-processing search for a trained U-Net, on its saved val probabilities (no GPU).

Honest protocol: val is split into two folds by tile hash; each setting is chosen on one fold
and scored on the other, then the two held-out halves are pooled (2-fold cross-validation).

  python scripts/pp_search.py --probs val_probs_u8.npz --out results/supervised/pp_search_convnextb.json
"""
import argparse
import hashlib
import itertools
import json
import sys
from multiprocessing import Pool
from pathlib import Path

import cv2
import numpy as np
from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sn2.metric import load_solution_csv, score  # noqa: E402

AOIS = ["AOI_2_Vegas", "AOI_3_Paris", "AOI_4_Shanghai", "AOI_5_Khartoum"]
SCALE = 1.5  # train_seg.py runs post-processing on the 1.5x-upsampled grid
P = None


def watershed_polys(prob, t_mask, t_seed, min_seed, eps=1.0):
    """Same as train_seg.postprocess; prob is HWC uint8 at native 650 px, upsampled 1.5x first."""
    from skimage.segmentation import watershed

    h, w = prob.shape[:2]
    pr = cv2.resize(prob, (int(w * SCALE), int(h * SCALE)), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255
    body, border, contact = pr[..., 0], pr[..., 1], pr[..., 2]
    seed = (body * (1 - border) * (1 - contact)) > t_seed
    n, markers = cv2.connectedComponents(seed.astype(np.uint8), connectivity=4)
    if n <= 1:
        return []
    sizes = np.bincount(markers.ravel())
    small = sizes < min_seed
    small[0] = False
    markers[small[markers]] = 0
    mask = (body > t_mask) | (markers > 0)
    lab = watershed(-body, markers, mask=mask)
    out = []
    for l in np.unique(lab):
        if l == 0:
            continue
        cs, _ = cv2.findContours((lab == l).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cs:
            continue
        c = cv2.approxPolyDP(max(cs, key=cv2.contourArea), eps, True)
        if len(c) < 3:
            continue
        p = Polygon((c[:, 0, :].astype(np.float64) + 0.5) / SCALE)
        if not p.is_valid:
            p = p.buffer(0)
        if not p.is_empty:
            out.append(p)
    return out


def _work(args):
    iid, kw = args
    return iid, watershed_polys(P[iid], **kw)


def refine(polys, dil, min_area):
    out = []
    for p in polys:
        q = p.buffer(dil, join_style=2) if dil else p
        if q.geom_type == "MultiPolygon":
            q = max(q.geoms, key=lambda g: g.area)
        if not q.is_empty and q.area >= min_area:
            out.append(q)
    return out


def main():
    global P
    ap = argparse.ArgumentParser()
    ap.add_argument("--probs", required=True)
    ap.add_argument("--labels", default="data/sn2/labels")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    z = np.load(a.probs)
    P = {k: z[k] for k in z.files}
    ids = sorted(P)
    gt = {}
    for aoi in AOIS:
        gt.update(load_solution_csv(Path(a.labels) / f"{aoi}_solutions.csv"))
    fold = {i: int(hashlib.md5((i + "ppfold").encode()).hexdigest(), 16) % 2 for i in ids}
    folds = [[i for i in ids if fold[i] == f] for f in (0, 1)]

    ws_grid = [dict(t_mask=tm, t_seed=ts, min_seed=ms) for tm, ts, ms in
               itertools.product((0.3, 0.4, 0.5), (0.5, 0.6, 0.7, 0.8), (15, 30, 60))]
    ref_grid = list(itertools.product((0.0, 0.5, 1.0, 1.5), (20, 40, 60, 100)))  # (dilation px, min area px)
    table = []  # one row per (ws, refine): per-fold, per-city tp/fp/fn
    with Pool(a.workers) as pool:
        for n, kw in enumerate(ws_grid):
            polys = dict(pool.map(_work, [(i, kw) for i in ids], chunksize=16))
            for dil, ma in ref_grid:
                pred = {i: refine(polys[i], dil, ma) for i in ids}
                row = {"ws": kw, "dil": dil, "min_area": ma}
                for f in (0, 1):
                    r = score(gt, pred, folds[f])
                    row[f] = {c: {k: int(r[c][k]) for k in ("tp", "fp", "fn")} for c in AOIS}
                row["full"] = float(np.mean([2 * sum(row[f][c]["tp"] for f in (0, 1)) / max(1, sum(
                    2 * row[f][c]["tp"] + row[f][c]["fp"] + row[f][c]["fn"] for f in (0, 1))) for c in AOIS]))
                table.append(row)
            base = [r for r in table if r["ws"] == kw and r["dil"] == 0 and r["min_area"] == 20][0]
            print(n + 1, "/", len(ws_grid), kw, "full-val F1 (no refine)", round(base["full"], 4), flush=True)

    def f1(c):
        return 2 * c["tp"] / max(2 * c["tp"] + c["fp"] + c["fn"], 1)

    def city_f1(row, f, cities=AOIS):
        return np.mean([f1(row[f][c]) for c in cities])

    def cv(select):  # select(row-list, fold) -> {city: row}; score on the other fold, pool held-out halves
        tot = {c: {"tp": 0, "fp": 0, "fn": 0} for c in AOIS}
        picks = []
        for f in (0, 1):
            pick = select(table, f)
            picks.append({c: {"ws": pick[c]["ws"], "dil": pick[c]["dil"], "min_area": pick[c]["min_area"]}
                          for c in AOIS})
            for c in AOIS:
                for k in tot[c]:
                    tot[c][k] += pick[c][1 - f][c][k]
        per = {c: round(f1(tot[c]), 4) for c in AOIS}
        return {"overall": round(float(np.mean(list(per.values()))), 4), "per_city": per, "picks": picks}

    def fixed(row):
        return lambda t, f: {c: row for c in AOIS}

    baseline = [r for r in table if r["ws"] == dict(t_mask=0.4, t_seed=0.7, min_seed=30)
                and r["dil"] == 0 and r["min_area"] == 20][0]
    no_ref = [r for r in table if r["dil"] == 0 and r["min_area"] == 20]
    res = {
        "baseline_tune_split_pp": cv(fixed(baseline)),
        "global_ws_only": cv(lambda t, f: {c: max(no_ref, key=lambda r: city_f1(r, f)) for c in AOIS}),
        "global_ws_dil_area": cv(lambda t, f: {c: max(t, key=lambda r: city_f1(r, f)) for c in AOIS}),
        "per_city_ws_dil_area": cv(lambda t, f: {c: max(t, key=lambda r: f1(r[f][c])) for c in AOIS}),
        "best_full_val_in_sample": max(r["full"] for r in table),
    }
    print(json.dumps(res, indent=1))
    Path(a.out).write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
