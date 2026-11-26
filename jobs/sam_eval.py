# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "torch>=2.4", "torchvision", "sam2>=1.1", "transformers>=4.50", "sentencepiece", "protobuf",
#   "opencv-python-headless", "shapely>=2", "pandas", "numpy", "huggingface_hub>=0.30", "pillow",
# ]
# ///
"""Zero-shot SAM 2.1 baselines for SpaceNet 2 (HF Jobs, GPU).

Variants (all on the eval_small split, 25 val tiles per AOI):
  amg_all     SAM2.1 automatic mask generation, every mask = building
  amg_siglip  AMG masks kept only if SigLIP2 zero-shot says "building roof"
  box:<name>  SAM2.1 prompted with boxes from prompts/<name>.json (e.g. VLM boxes)
  box:gt      SAM2.1 prompted with ground-truth boxes (oracle: SAM mask quality alone)
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("SAM2_BUILD_CUDA", "0")
import cv2
import numpy as np
import torch
from huggingface_hub import hf_hub_download  # noqa
from PIL import Image
from shapely.geometry import Polygon


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def mask_to_poly(m, scale=1.0, eps=1.0):
    cs, _ = cv2.findContours(m.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None
    c = cv2.approxPolyDP(max(cs, key=cv2.contourArea), eps, True)
    if len(c) < 3:
        return None
    p = Polygon((c[:, 0, :].astype(float) + 0.5) / scale)
    return p if p.is_valid else p.buffer(0)


def dedup(masks, scores, max_overlap=0.5):
    order = np.argsort(-np.asarray(scores))
    occ = None
    keep = []
    for k in order:
        m = masks[k]
        a = m.sum()
        if a == 0:
            continue
        if occ is not None and (m & occ).sum() / a > max_overlap:
            continue
        keep.append(k)
        occ = m.copy() if occ is None else (occ | m)
    return keep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/sn2")
    ap.add_argument("--out", default="/out")
    ap.add_argument("--variants", default="amg_all,amg_siglip,box:gt")
    ap.add_argument("--split", default="eval_small")
    ap.add_argument("--up", type=float, default=2.0, help="upsample factor before SAM")
    args = ap.parse_args()

    root = Path(args.data)
    sys.path.insert(0, str(root / "code"))
    from metric import AOIS, load_solution_csv, score  # noqa

    ids = json.loads((root / "splits.json").read_text())[args.split]
    gt = {}
    for a in AOIS:
        gt.update(load_solution_csv(root / "labels" / f"{a}_solutions.csv"))

    from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
    from sam2.sam2_image_predictor import SAM2ImagePredictor

    torch.autocast("cuda", dtype=torch.bfloat16).__enter__()
    predictor = SAM2ImagePredictor.from_pretrained("facebook/sam2.1-hiera-large")
    variants = args.variants.split(",")
    results, preds = {}, {v: {} for v in variants}

    amg = None
    if any(v.startswith("amg") for v in variants):
        amg = SAM2AutomaticMaskGenerator(predictor.model, points_per_side=48, points_per_batch=256,
                                         pred_iou_thresh=0.7, stability_score_thresh=0.88,
                                         crop_n_layers=1, crop_n_points_downscale_factor=2,
                                         min_mask_region_area=40, box_nms_thresh=0.6)
    sig = None
    if "amg_siglip" in variants:
        from transformers import AutoModel, AutoProcessor

        sig_id = "google/siglip2-so400m-patch14-384"
        sig = AutoModel.from_pretrained(sig_id, torch_dtype=torch.bfloat16).cuda().eval()
        sig_proc = AutoProcessor.from_pretrained(sig_id)
        labels = ["a building roof", "a house roof", "a road", "trees", "grass", "a parking lot",
                  "bare soil", "a swimming pool", "a car", "a shadow", "a sidewalk", "sand"]
        n_building = 2
        texts = [f"a satellite photo of {l}." for l in labels]
        with torch.no_grad():
            ti = sig_proc(text=texts, padding="max_length", max_length=64, return_tensors="pt").to("cuda")
            tfeat = sig.get_text_features(**ti)
            tfeat = tfeat if torch.is_tensor(tfeat) else tfeat.pooler_output
            tfeat = tfeat / tfeat.norm(dim=-1, keepdim=True)

    prompts = {}
    for v in variants:
        if v.startswith("box:") and v != "box:gt":
            prompts[v] = json.loads((Path(args.out) / "prompts" / f"{v[4:]}.json").read_text())

    up = args.up
    tag = time.strftime("%Y%m%d-%H%M%S")
    od = Path(args.out) / f"sam_{tag}"
    od.mkdir(parents=True, exist_ok=True)

    def dump(done_ids):
        res = {}
        for v in variants:
            r = score(gt, preds[v], done_ids)
            r["n_pred"] = sum(len(x) for x in preds[v].values())
            r["n_tiles"] = len(done_ids)
            res[v] = r
        (od / "sam_results.json").write_text(json.dumps(res, indent=1))
        (od / "sam_preds.json").write_text(json.dumps(
            {v: {i: [p.wkt for p in ps] for i, ps in preds[v].items()} for v in variants}))
        return res

    t0 = time.time()
    for n, iid in enumerate(ids):
        img = np.array(Image.open(root / "images" / iid.rsplit("_img", 1)[0] / f"{iid}.png").convert("RGB"))
        big = cv2.resize(img, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)
        H, W = big.shape[:2]
        if amg is not None:
            ms = amg.generate(big)
            ms = [m for m in ms if 20 * up * up <= m["area"] <= 40000 * up * up]
            masks = [m["segmentation"] for m in ms]
            keep = dedup(masks, [m["predicted_iou"] for m in ms])
            ms = [ms[k] for k in keep]
            if "amg_all" in variants:
                preds["amg_all"][iid] = [p for p in (mask_to_poly(m["segmentation"], up) for m in ms) if p]
            if sig is not None:
                crops = []
                for m in ms:
                    x, y, w, h = m["bbox"]
                    pad = max(w, h) * 0.25 + 8
                    x0, y0 = int(max(0, x - pad)), int(max(0, y - pad))
                    x1, y1 = int(min(W, x + w + pad)), int(min(H, y + h + pad))
                    crops.append(Image.fromarray(big[y0:y1, x0:x1]))
                keepp = []
                for b in range(0, len(crops), 64):
                    with torch.no_grad():
                        pi = sig_proc(images=crops[b:b + 64], return_tensors="pt").to("cuda")
                        f = sig.get_image_features(pixel_values=pi["pixel_values"].to(torch.bfloat16))
                        f = f if torch.is_tensor(f) else f.pooler_output
                        f = f / f.norm(dim=-1, keepdim=True)
                        lab = (f.float() @ tfeat.float().T).argmax(-1).cpu().numpy()
                    keepp += list(lab < n_building)
                preds["amg_siglip"][iid] = [p for p, k in zip((mask_to_poly(m["segmentation"], up) for m in ms), keepp)
                                            if p and k]
        box_vs = [v for v in variants if v.startswith("box:")]
        if box_vs:
            predictor.set_image(big)
            for v in box_vs:
                if v == "box:gt":
                    boxes = [list(p.bounds) for p in gt.get(iid, []) if p.area >= 20]
                else:
                    boxes = prompts[v].get(iid, [])
                if not boxes:
                    preds[v][iid] = []
                    continue
                bx = np.asarray(boxes, dtype=np.float32) * up
                out = []
                for b in range(0, len(bx), 64):
                    m, s, _ = predictor.predict(box=bx[b:b + 64], multimask_output=False)
                    m = m.reshape(-1, H, W) > 0
                    out += [mask_to_poly(mm, up) for mm in m]
                preds[v][iid] = [p for p in out if p is not None and p.area > 0]
        if (n + 1) % 10 == 0:
            log(f"{n + 1}/{len(ids)} {(time.time() - t0) / (n + 1):.1f}s/tile")
            r = dump(ids[: n + 1])
            log("partial", {v: r[v]["overall_f1"] for v in variants})

    results = dump(ids)
    for v in variants:
        log(v, json.dumps(results[v]))
    log("done", tag)


if __name__ == "__main__":
    main()
