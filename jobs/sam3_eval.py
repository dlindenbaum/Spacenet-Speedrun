# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "torch>=2.4", "torchvision", "transformers>=5.0", "accelerate",
#   "opencv-python-headless", "shapely>=2", "pandas", "numpy", "pillow",
# ]
# ///
"""Zero-shot SAM 3 (text-prompted "Promptable Concept Segmentation") on SpaceNet 2 eval_small.

SAM 3 detects + segments every instance of a noun phrase. Weights are read from the mounted bucket
(/out/weights/sam3, a private copy of the gated facebook/sam3), since Jobs have no HF token.

Variants: prompt in {building, house, roof} x {full tile, 2x2 crops}. Each crop is resized by the processor
to SAM 3's 1008 px input, so 2x2 gives ~3x more pixels per building. Instances from all crops are merged
(score-ordered overlap suppression) and scored at several score thresholds.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image
from shapely.geometry import Polygon


DEV = "cuda" if torch.cuda.is_available() else "cpu"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def mask_to_poly(m, eps=1.0):
    cs, _ = cv2.findContours(m.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None
    c = cv2.approxPolyDP(max(cs, key=cv2.contourArea), eps, True)
    if len(c) < 3:
        return None
    p = Polygon(c[:, 0, :].astype(float) + 0.5)
    p = p if p.is_valid else p.buffer(0)
    return p if p.area > 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/sn2")
    ap.add_argument("--out", default="/out")
    ap.add_argument("--weights", default="/out/weights/sam3")
    ap.add_argument("--prompts", default="building,house,roof")
    ap.add_argument("--grids", default="1,2")
    ap.add_argument("--split", default="eval_small")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--thresholds", default="0.2,0.3,0.4,0.5,0.6")
    args = ap.parse_args()
    root = Path(args.data)
    sys.path.insert(0, str(root / "code"))
    from metric import AOIS, load_solution_csv, score  # noqa
    from transformers import Sam3Model, Sam3Processor

    ids = json.loads((root / "splits.json").read_text())[args.split][: args.limit or None]
    gt = {}
    for a in AOIS:
        gt.update(load_solution_csv(root / "labels" / f"{a}_solutions.csv"))
    model = Sam3Model.from_pretrained(args.weights, torch_dtype=torch.bfloat16).to(DEV).eval()
    proc = Sam3Processor.from_pretrained(args.weights)
    prompts, grids = args.prompts.split(","), [int(g) for g in args.grids.split(",")]
    ths = [float(t) for t in args.thresholds.split(",")]
    variants = [f"{p}_g{g}" for p in prompts for g in grids]
    raw = {v: {} for v in variants}  # iid -> [(score, wkt)]
    od = Path(args.out) / "sam3_eval"
    od.mkdir(parents=True, exist_ok=True)

    @torch.no_grad()
    def run(crop, text):
        inp = proc(images=Image.fromarray(crop), text=text, return_tensors="pt").to(DEV)
        inp["pixel_values"] = inp["pixel_values"].to(torch.bfloat16)
        out = model(**inp)
        r = proc.post_process_instance_segmentation(out, threshold=min(ths), mask_threshold=0.5,
                                                    target_sizes=inp.get("original_sizes").tolist())[0]
        return r["masks"].cpu().numpy().astype(bool), r["scores"].float().cpu().numpy()

    def merged(img, text, g):
        H, W = img.shape[:2]
        ov = 32 if g > 1 else 0
        st = H // g
        masks, scores = [], []
        for gy in range(g):
            for gx in range(g):
                y0, x0 = max(0, gy * st - ov), max(0, gx * st - ov)
                y1, x1 = min(H, (gy + 1) * st + ov), min(W, (gx + 1) * st + ov)
                m, s = run(img[y0:y1, x0:x1], text)
                for mm, ss in zip(m, s):
                    full = np.zeros((H, W), bool)
                    full[y0:y1, x0:x1] = mm
                    masks.append(full)
                    scores.append(ss)
        occ = np.zeros((H, W), bool)
        keep = []
        for k in np.argsort(-np.asarray(scores)):
            a = masks[k].sum()
            if a == 0 or (masks[k] & occ).sum() / a > 0.5:
                continue
            p = mask_to_poly(masks[k] & ~occ)
            occ |= masks[k]
            if p is not None:
                keep.append((float(scores[k]), p.wkt))
        return keep

    def dump(done):
        from shapely import wkt as W
        res = {}
        for v in variants:
            for t in ths:
                pr = {i: [W.loads(w) for s, w in raw[v][i] if s >= t] for i in done}
                r = score(gt, pr, done)
                r["n_pred"] = sum(len(x) for x in pr.values())
                res[f"{v}@{t}"] = r
        (od / "results.json").write_text(json.dumps(res, indent=1))
        (od / "preds.json").write_text(json.dumps(raw))
        return res

    t0 = time.time()
    for n, iid in enumerate(ids):
        img = np.array(Image.open(root / "images" / iid.rsplit("_img", 1)[0] / f"{iid}.png").convert("RGB"))
        for p in prompts:
            for g in grids:
                raw[f"{p}_g{g}"][iid] = merged(img, p, g)
        if (n + 1) % 10 == 0:
            r = dump(ids[: n + 1])
            best = max(r, key=lambda k: r[k]["overall_f1"])
            log(f"{n + 1}/{len(ids)} {(time.time() - t0) / (n + 1):.1f}s/tile best so far {best} {r[best]['overall_f1']}")
    r = dump(ids)
    for k in sorted(r, key=lambda k: -r[k]["overall_f1"])[:10]:
        log(k, json.dumps({a: r[k][a]["f1"] for a in AOIS if a in r[k]}), "overall", r[k]["overall_f1"], "n_pred", r[k]["n_pred"])
    log("done")


if __name__ == "__main__":
    main()
