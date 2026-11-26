"""Zero-shot VLM building detection on the eval_small split (25 val tiles per AOI).

Usage: python scripts/eval_vlm.py --model google/gemma-4-31B-it --mode box [--grid 2]

Raw responses are cached under results/raw/<tag>/ so re-scoring is free.
--grid N splits the 650px tile into NxN crops, each upsampled to 650px, and
queries the model on each crop (more pixels per building).
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image
from shapely import affinity
from tqdm import tqdm

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sn2.metric import AOIS, load_solution_csv, score, format_table  # noqa
from sn2.vlm import PROMPT_BOX, PROMPT_POLY, chat, encode, parse  # noqa

DATA = Path("data/sn2")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--mode", default="box", choices=["box", "poly"])
    ap.add_argument("--grid", type=int, default=1)
    ap.add_argument("--split", default="eval_small")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    tag = f"{args.model.split('/')[-1]}_{args.mode}_g{args.grid}"
    raw = Path("results/raw") / tag
    raw.mkdir(parents=True, exist_ok=True)
    ids = json.loads((DATA / "splits.json").read_text())[args.split]
    gt = {}
    for a in AOIS:
        gt.update(load_solution_csv(DATA / "labels" / f"{a}_solutions.csv"))

    prompt_t = PROMPT_BOX if args.mode == "box" else PROMPT_POLY
    jobs = []
    for iid in ids:
        for gy in range(args.grid):
            for gx in range(args.grid):
                jobs.append((iid, gx, gy))

    cost = [0.0]

    def run(job):
        iid, gx, gy = job
        f = raw / f"{iid}_{gx}_{gy}.txt"
        if f.exists():
            return
        img = Image.open(DATA / "images" / iid.rsplit("_img", 1)[0] / f"{iid}.png").convert("RGB")
        s = 650 / args.grid
        crop = img.crop((round(gx * s), round(gy * s), round((gx + 1) * s), round((gy + 1) * s)))
        crop = crop.resize((650, 650), Image.BICUBIC)
        txt, usage = chat(args.model, prompt_t.format(w=650, h=650), encode(crop))
        cost[0] += usage.get("cost", 0) or 0
        f.write_text(txt)

    with ThreadPoolExecutor(args.workers) as ex:
        list(tqdm(ex.map(run, jobs), total=len(jobs), desc=tag, mininterval=10))

    best = None
    for order in (["yxyx", "xyxy"] if args.mode == "box" else ["-"]):
        pred = {i: [] for i in ids}
        for iid, gx, gy in jobs:
            txt = (raw / f"{iid}_{gx}_{gy}.txt").read_text()
            polys = parse(txt, 650, 650, args.mode, box_order=order)
            s = 650 / args.grid
            for p in polys:  # map crop coords back to tile coords
                p = affinity.scale(p, 1 / args.grid, 1 / args.grid, origin=(0, 0))
                pred[iid].append(affinity.translate(p, gx * s, gy * s))
        res = score(gt, pred, ids)
        res["box_order"] = order
        res["n_pred"] = sum(len(v) for v in pred.values())
        if best is None or res["overall_f1"] > best["overall_f1"]:
            best = res
    best["n_gt"] = sum(len(gt[i]) for i in ids)
    best["model"] = args.model
    best["mode"] = args.mode
    best["grid"] = args.grid
    best["api_cost_usd_this_run"] = round(cost[0], 4)
    out = Path("results") / f"vlm_{tag}.json"
    out.write_text(json.dumps(best, indent=1))
    print(format_table(best, tag), best["box_order"], "n_pred", best["n_pred"], "n_gt", best["n_gt"],
          "cost", best["api_cost_usd_this_run"])


if __name__ == "__main__":
    main()
