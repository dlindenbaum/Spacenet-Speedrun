"""Qualitative comparison: GT vs zero-shot (Gemma 4 31B 2x2 boxes -> SAM 2.1) vs supervised ConvNeXt-B U-Net."""
import json, sys
from pathlib import Path
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MPoly
from PIL import Image
from shapely import wkt
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sn2.metric import AOIS, load_solution_csv, match_tile, _clean

D = Path("data/sn2")
gt = {}
for a in AOIS: gt.update(load_solution_csv(D / "labels" / f"{a}_solutions.csv"))
sup = load_solution_csv("data/runs/convnextb_val.csv")
sam = {i: [wkt.loads(w) for w in ws] for i, ws in json.load(open("data/runs/sam_g2_preds.json"))["box:gemma4_31b_g2"].items()}
ids = json.loads((D / "splits.json").read_text())["eval_small"]
pick = []
for a in AOIS:  # most-building tile per city among eval_small, capped for readability
    c = sorted([i for i in ids if i.startswith(a)], key=lambda i: -len(gt.get(i, [])))
    pick.append(next(i for i in c if 15 <= len(gt[i]) <= 70) if any(15 <= len(gt[i]) <= 70 for i in c) else c[0])

def f1(g, p):
    tp, fp, fn = match_tile(g, p); return 2 * tp / max(2 * tp + fp + fn, 1)

fig, axes = plt.subplots(4, 3, figsize=(13, 17.5))
for r, iid in enumerate(pick):
    img = Image.open(D / "images" / iid.rsplit("_img", 1)[0] / f"{iid}.png")
    cols = [("Ground truth", gt.get(iid, []), "#ffd400"),
            ("Zero-shot: Gemma 4 → SAM 2.1", sam.get(iid, []), "#00e0ff"),
            ("Supervised: ConvNeXt-B U-Net", sup.get(iid, []), "#ff4fd8")]
    for c, (title, polys, color) in enumerate(cols):
        ax = axes[r, c]; ax.imshow(img); ax.set_xticks([]); ax.set_yticks([])
        for p in _clean(polys):
            ax.add_patch(MPoly(list(p.exterior.coords), closed=True, fill=False, ec=color, lw=1.3))
        sub = f"{len(_clean(polys))} bldgs" + ("" if c == 0 else f" · tile F1 {f1(gt.get(iid, []), polys):.2f}")
        ax.set_title(f"{title}\n{sub}" if r == 0 else sub, fontsize=11)
        if c == 0: ax.set_ylabel(iid.split("_")[2], fontsize=13)
plt.tight_layout()
plt.savefig("results/qualitative.png", dpi=90)
print(pick)
