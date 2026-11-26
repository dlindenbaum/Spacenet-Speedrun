# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "torch>=2.4", "torchvision", "timm>=1.0.9", "segmentation-models-pytorch>=0.4",
#   "opencv-python-headless", "shapely>=2", "scikit-image", "pandas", "numpy", "tqdm", "pillow",
# ]
# ///
"""Evaluate SN2-trained building models on the other SpaceNet building datasets
(prepared by scripts/prepare_xds.py: RGB uint8 resampled to 0.3 m GSD).

Scores F1 @ IoU>=0.5 (same scorer as SN2), pooled per group:
  (sn1 excluded: label quality) | sn4: per look-angle band (NADIR/OFF/VOFF) + per collect | sn6: Rotterdam
  sn7: Planet 4 m | sn8: per region (Germany / Louisiana)
"""
import argparse
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
import torch
from shapely import wkt


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/sn2")
    ap.add_argument("--out", default="/out")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--models", required=True, help="comma list of ckpt_path=encoder (ensemble mean)")
    ap.add_argument("--datasets", default="sn4,sn6,sn7,sn8")
    ap.add_argument("--pp", default='{"t_mask": 0.4, "t_seed": 0.7, "min_seed": 30}')
    ap.add_argument("--tta", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    root = Path(args.data)
    sys.path.insert(0, str(root / "code"))
    import train_seg as T  # noqa
    from metric import match_tile  # noqa

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    models = []
    for item in args.models.split(","):
        ck, enc = item.split("=")
        m = T.build_model(enc).to(dev).to(memory_format=torch.channels_last).eval()
        m.load_state_dict(torch.load(ck, map_location=dev))
        models.append(m)
        log("model", ck, enc)
    pp = json.loads(args.pp)

    @torch.no_grad()
    def predict_any(img):
        H, W = img.shape[:2]
        h, w = int(round(H * T.SCALE)), int(round(W * T.SCALE))
        im = cv2.resize(img, (w, h), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255.0
        im = (im - T.MEAN) / T.STD
        ph, pw = (h + 31) // 32 * 32, (w + 31) // 32 * 32
        x = np.zeros((ph, pw, 3), np.float32)
        x[:h, :w] = im
        x = torch.from_numpy(x.transpose(2, 0, 1)).to(dev)[None].contiguous(memory_format=torch.channels_last)
        acc = 0
        n = 0
        for m in models:
            views = [(lambda t: t, lambda t: t)]
            if args.tta:
                views += [(lambda t: t.flip(-1), lambda t: t.flip(-1)), (lambda t: t.flip(-2), lambda t: t.flip(-2))]
            for f, finv in views:
                with torch.autocast(dev, dtype=torch.bfloat16):
                    acc = acc + finv(m(f(x)).float().sigmoid())
                n += 1
        return (acc / n)[0, :, :h, :w].cpu().numpy()

    od = Path(args.out) / f"xfer_{args.tag}"
    od.mkdir(parents=True, exist_ok=True)
    summary = {}
    pool = ThreadPoolExecutor(8)
    for ds in args.datasets.split(","):
        d = root / "xds" / ds
        labels = json.loads((d / "labels.json").read_text())
        meta = json.loads((d / "meta.json").read_text())
        ids = json.loads((d / "splits.json").read_text())["test"][: args.limit or None]
        t0 = time.time()
        rows, preds, futs = {}, {}, []

        def finish(iid, prob):
            polys = T.postprocess(prob, **pp)
            gt = [wkt.loads(s) for s in labels[iid]]
            tp, fp, fn = match_tile(gt, polys)
            return iid, (tp, fp, fn), [p.wkt for p in polys]

        for k, iid in enumerate(ids):
            img = cv2.cvtColor(cv2.imread(str(d / "images" / f"{iid}.png")), cv2.COLOR_BGR2RGB)
            futs.append(pool.submit(finish, iid, predict_any(img)))
            if (k + 1) % 100 == 0:
                log(ds, k + 1, "/", len(ids), f"{(time.time() - t0) / (k + 1):.2f}s/tile")
        for f in futs:
            iid, c, ps = f.result()
            rows[iid], preds[iid] = c, ps

        groups = defaultdict(lambda: np.zeros(3, np.int64))
        for iid, c in rows.items():
            m = meta[iid]
            groups["all"] += c
            for key in ("band", "region"):
                if key in m:
                    groups[f"{key}:{m[key]}"] += c
            if "angle" in m:
                groups[f"angle:{m['angle']:02d}"] += c

        def f1(c):
            tp, fp, fn = c
            p, r = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
            return dict(f1=round(2 * p * r / max(p + r, 1e-9), 4), precision=round(p, 4), recall=round(r, 4),
                        tp=int(tp), fp=int(fp), fn=int(fn))

        res = {g: f1(c) for g, c in sorted(groups.items())}
        if ds == "sn4":
            res["mean_of_bands_f1"] = round(np.mean([res[f"band:{b}"]["f1"] for b in ("NADIR", "OFF", "VOFF")]), 4)
        res["n_tiles"] = len(ids)
        summary[ds] = res
        log(ds, json.dumps({k: (v["f1"] if isinstance(v, dict) else v) for k, v in res.items()}))
        (od / f"{ds}_preds.json").write_text(json.dumps(preds))
        (od / "summary.json").write_text(json.dumps(summary, indent=1))
    log("done")


if __name__ == "__main__":
    main()
