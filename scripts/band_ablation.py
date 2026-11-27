"""Score the multispectral band ablation from each run's val_predictions.csv.

Reports per-city F1 on all of val and on val minus the tiles whose PS-MS source file is blank
(SpaceNet data defect; the RGB tile is fine), plus a paired bootstrap over val tiles (resampled
within each city) of each run's overall-F1 difference against the MS-RGB run.

  python scripts/band_ablation.py <pred_dir> results/supervised/band_ablation.json
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sn2.metric import AOIS, load_solution_csv, match_tile  # noqa: E402

RUNS = {"PS-RGB (original 0.755 model)": "convnextb_unet_20k", "MS-RGB (bands 4,2,1)": "msrgb_convnextb_20k",
        "MS-RGB + NIR1": "msrgbn_convnextb_20k", "MS all 8 bands": "ms8_convnextb_20k"}
REF = "MS-RGB (bands 4,2,1)"
# PS-MS file is all zeros for these Vegas tiles (found by scanning the shards; RGB is not blank)
BLANK_MS = {"AOI_2_Vegas_img1975", "AOI_2_Vegas_img2038", "AOI_2_Vegas_img2172", "AOI_2_Vegas_img2245",
            "AOI_2_Vegas_img2397", "AOI_2_Vegas_img2476", "AOI_2_Vegas_img2555", "AOI_2_Vegas_img3898",
            "AOI_2_Vegas_img4056", "AOI_2_Vegas_img4293"}


def f1(c):
    return 2 * c[0] / max(2 * c[0] + c[1] + c[2], 1)


def overall(counts, ids_by_city):
    per = {a: f1(counts[ids_by_city[a]].sum(0)) for a in AOIS}
    return float(np.mean(list(per.values()))), per


def main():
    pred_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
    val = json.loads(Path("data/sn2/splits.json").read_text())["val"]
    gt = {}
    for a in AOIS:
        gt.update(load_solution_csv(f"data/sn2/labels/{a}_solutions.csv"))
    tiles = {}
    for name, run in RUNS.items():
        pr = load_solution_csv(pred_dir / f"{run}.csv")
        tiles[name] = np.array([match_tile(gt.get(i, []), pr.get(i, [])) for i in val], np.float64)
    res = {"n_val": len(val), "blank_ms_tiles_in_val": sorted(BLANK_MS & set(val))}
    for subset, keep in (("all_val", lambda i: True), ("excl_blank_ms", lambda i: i not in BLANK_MS)):
        idx = {a: np.array([k for k, i in enumerate(val) if i.startswith(a) and keep(i)]) for a in AOIS}
        rng = np.random.default_rng(0)
        boots = [{a: rng.choice(idx[a], len(idx[a])) for a in AOIS} for _ in range(1000)]
        ref_b = np.array([overall(tiles[REF], b)[0] for b in boots])
        res[subset] = {}
        for name in RUNS:
            o, per = overall(tiles[name], idx)
            d = np.array([overall(tiles[name], b)[0] for b in boots]) - ref_b
            res[subset][name] = {"overall": round(o, 4), "per_city": {a: round(v, 4) for a, v in per.items()},
                                 "diff_vs_ms_rgb_95ci": [round(float(np.percentile(d, q)), 4) for q in (2.5, 97.5)]}
            print(subset, f"{name:32s}", round(o, 4), {a[6:]: round(v, 3) for a, v in per.items()},
                  "diff vs MS-RGB 95% CI", res[subset][name]["diff_vs_ms_rgb_95ci"], flush=True)
    out.write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
