"""Convert cached VLM box responses into SAM box prompts (650-px xyxy).

Usage: python scripts/export_prompts.py <raw_tag> <box_order> <grid> <out.json>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sn2.vlm import parse  # noqa

tag, order, grid, out = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
raw = Path("results/raw") / tag
ids = json.loads(Path("data/sn2/splits.json").read_text())["eval_small"]
s = 650 / grid
prompts = {}
for iid in ids:
    boxes = []
    for gy in range(grid):
        for gx in range(grid):
            for p in parse((raw / f"{iid}_{gx}_{gy}.txt").read_text(), 650, 650, "box", box_order=order):
                x0, y0, x1, y1 = p.bounds
                boxes.append([x0 / grid + gx * s, y0 / grid + gy * s, x1 / grid + gx * s, y1 / grid + gy * s])
    prompts[iid] = boxes
Path(out).write_text(json.dumps(prompts))
print(out, sum(len(v) for v in prompts.values()), "boxes")
