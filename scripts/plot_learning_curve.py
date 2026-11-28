"""Label-budget learning curve: F1 on the full SN2 val split vs number of labelled training chips.

Inputs: results/learning_curve/lc_n{N}_s{seed}.json (jobs/train_seg.py --n_train N --subset_seed seed),
results/supervised/convnextb_unet_20k.json (all chips), results/learning_curve/subset_label_counts.json.
Outputs: results/learning_curve/learning_curve.png and summary.json.
"""
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

D = Path("results/learning_curve")
AOIS = ["AOI_2_Vegas", "AOI_3_Paris", "AOI_4_Shanghai", "AOI_5_Khartoum"]
CITY = {"AOI_2_Vegas": "Vegas", "AOI_3_Paris": "Paris", "AOI_4_Shanghai": "Shanghai", "AOI_5_Khartoum": "Khartoum"}
COL = {"AOI_2_Vegas": "#257de0", "AOI_3_Paris": "#f47230", "AOI_4_Shanghai": "#00b894", "AOI_5_Khartoum": "#6a4ce2"}


def main():
    counts = json.loads((D / "subset_label_counts.json").read_text())
    runs = {}
    for f in sorted(D.glob("lc_n*_s*.json")):
        n, s = f.stem.split("_")[1][1:], f.stem.split("_")[2][1:]
        runs.setdefault(int(n), []).append(json.loads(f.read_text()))
    full = json.loads(Path("results/supervised/convnextb_unet_20k.json").read_text())
    n_full = counts["all_s0"]["chips"]
    runs[n_full] = [full]
    ns = sorted(runs)
    summ = {}
    for n in ns:
        rs = runs[n]
        summ[n] = {"overall": [r["overall_f1"] for r in rs],
                   **{CITY[a]: [r[a]["f1"] for r in rs] for a in AOIS}}
        key = next((k for k in counts if k.startswith(f"{n}_")), "all_s0" if n == n_full else None)
        summ[n]["buildings"] = counts[key]["buildings"] if key else None
    (D / "summary.json").write_text(json.dumps(summ, indent=1))

    fig, ax = plt.subplots(figsize=(8, 5), dpi=130)
    for a in AOIS:
        y = [np.mean(summ[n][CITY[a]]) for n in ns]
        ax.plot(ns, y, color=COL[a], lw=1.6, marker="o", ms=4)
        ax.text(ns[-1] * 1.08, y[-1], CITY[a], color="#333", va="center", fontsize=9)
    y = [np.mean(summ[n]["overall"]) for n in ns]
    ax.plot(ns, y, color="#191919", lw=2.6, marker="o", ms=6, zorder=5)
    for n in ns:
        for v in summ[n]["overall"]:
            ax.plot(n, v, "o", ms=4, mfc="white", mec="#191919", zorder=6)
        ax.annotate(f"{np.mean(summ[n]['overall']):.3f}", (n, np.mean(summ[n]["overall"])), textcoords="offset points",
                    xytext=(0, 9), ha="center", fontsize=9, color="#191919")
    ax.text(ns[-1] * 1.08, y[-1], "Overall", color="#191919", va="center", fontsize=9, weight="bold")
    for v, lab, ls in ((0.693, "XD_XD, 2017 winner (0.693)", "--"), (0.642, "SAM 3 zero-shot (0.642)", ":")):
        ax.axhline(v, color="#666", ls=ls, lw=1)
        ax.text(ns[0], v + 0.006, lab, color="#666", fontsize=8.5)
    ax.set_xscale("log")
    ax.set_xticks(ns)
    ax.set_xticklabels([f"{n:,}" for n in ns])
    ax.set_xlim(ns[0] * 0.8, ns[-1] * 1.6)
    ax.set_ylim(0.4, 0.92)
    ax.set_xlabel("labelled training chips (650 × 650 px, 0.038 km² each), log scale")
    ax.set_ylabel("F1 on 1,037 validation chips (IoU ≥ 0.5)")
    ax.set_title("How many labelled chips does the ConvNeXt-B U-Net need?", loc="left", fontsize=11)
    ax.grid(axis="y", color="#e6e6e6")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(D / "learning_curve.png")
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
