# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "torch>=2.4", "torchvision", "timm>=1.0.9", "segmentation-models-pytorch>=0.4",
#   "opencv-python-headless", "shapely>=2", "scikit-image", "pandas", "numpy",
#   "tqdm", "pillow",
# ]
# ///
"""Supervised SpaceNet-2 building footprint model, run on Hugging Face Jobs.

Model: U-Net with a modern pretrained encoder predicting 3 channels
(building body, building border, building-building contact). Instances are
recovered with marker-controlled watershed, then vectorised to polygons.

Images are upsampled 1.5x (0.3 m -> 0.2 m/px) before the network, which
helps small buildings. Post-processing thresholds are tuned on a `tune`
split carved out of train (never trained on); final numbers are on `val`.
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn.functional as F

cv2.setNumThreads(1)
SCALE = 1.5
TILE = 650
BIG = int(TILE * SCALE)  # 975
PAD = 992


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ------------------------------------------------------------------ data
def render_masks(polys):
    """polys in 650-px coords -> uint8 bitmask at BIG res: bit0 body, bit1 border, bit2 contact."""
    body = np.zeros((BIG, BIG), np.uint8)
    border = np.zeros((BIG, BIG), np.uint8)
    count = np.zeros((BIG, BIG), np.uint8)
    k3 = np.ones((3, 3), np.uint8)
    for p in polys:
        ext = (np.asarray(p.exterior.coords)[:, :2] * SCALE).round().astype(np.int32)
        m = np.zeros((BIG, BIG), np.uint8)
        cv2.fillPoly(m, [ext], 1)
        for r in p.interiors:
            cv2.fillPoly(m, [(np.asarray(r.coords)[:, :2] * SCALE).round().astype(np.int32)], 0)
        if m.sum() == 0:
            continue
        body |= m
        border |= m - cv2.erode(m, k3, iterations=2)
        count += cv2.dilate(m, k3, iterations=3)
    contact = ((count > 1) & (body == 0) | (count > 1) & (border > 0)).astype(np.uint8)
    return body | (border << 1) | (contact << 2)


class TileDS(torch.utils.data.Dataset):
    def __init__(self, imgs, masks, crop, n_per_epoch, train=True):
        self.imgs, self.masks, self.crop, self.n, self.train = imgs, masks, crop, n_per_epoch, train

    def __len__(self):
        return self.n

    def __getitem__(self, i):
        rng = np.random.default_rng()
        k = rng.integers(len(self.imgs))
        img, msk = self.imgs[k], self.masks[k]
        # scale jitter: sample source window in 650-px space
        s = SCALE * rng.uniform(0.8, 1.25)
        src = min(int(self.crop / s), TILE)
        x0, y0 = rng.integers(0, TILE - src + 1, size=2)
        im = cv2.resize(img[y0:y0 + src, x0:x0 + src], (self.crop, self.crop), interpolation=cv2.INTER_LINEAR)
        mx0, my0, msrc = int(x0 * SCALE), int(y0 * SCALE), int(src * SCALE)
        mk = cv2.resize(msk[my0:my0 + msrc, mx0:mx0 + msrc], (self.crop, self.crop), interpolation=cv2.INTER_NEAREST)
        if rng.random() < 0.5:
            im, mk = im[:, ::-1], mk[:, ::-1]
        r = rng.integers(4)
        im, mk = np.rot90(im, r), np.rot90(mk, r)
        im = im.astype(np.float32) / 255.0
        if rng.random() < 0.8:  # brightness / contrast / gamma
            im = np.clip((im - 0.5) * rng.uniform(0.8, 1.2) + 0.5 + rng.uniform(-0.1, 0.1), 0, 1)
            im = im ** rng.uniform(0.8, 1.25)
        y = np.stack([(mk >> b) & 1 for b in range(3)]).astype(np.float32)
        x = torch.from_numpy(((im - MEAN) / STD).transpose(2, 0, 1).copy())
        return x, torch.from_numpy(y.copy())


MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


# ------------------------------------------------------------------ model / loss
def build_model(encoder, arch="Unet"):
    import segmentation_models_pytorch as smp

    w = True if encoder.startswith("tu-") else "imagenet"
    return getattr(smp, arch)(encoder_name=encoder, encoder_weights=w, classes=3)


def loss_fn(logits, y):
    w = torch.tensor([1.0, 0.5, 2.0], device=y.device)[None, :, None, None]
    bce = (F.binary_cross_entropy_with_logits(logits, y, reduction="none") * w).mean()
    p = torch.sigmoid(logits)
    inter = (p * y).sum((0, 2, 3))
    dice = 1 - (2 * inter + 1) / (p.sum((0, 2, 3)) + y.sum((0, 2, 3)) + 1)
    return bce + (dice * torch.tensor([1.0, 0.5, 0.5], device=y.device)).sum() / 2


# ------------------------------------------------------------------ inference / postproc
@torch.no_grad()
def predict(model, img, tta=True):
    """img uint8 HxWx3 (650) -> probs float16 3xBIGxBIG. `model` may be a list (ensemble mean)."""
    if isinstance(model, (list, tuple)):
        return (sum(predict(m, img, tta).astype(np.float32) for m in model) / len(model)).astype(np.float16)
    im = cv2.resize(img, (BIG, BIG), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255.0
    im = (im - MEAN) / STD
    x = np.zeros((PAD, PAD, 3), np.float32)
    x[:BIG, :BIG] = im
    x = torch.from_numpy(x.transpose(2, 0, 1)).cuda()[None]
    xs = [x, x.flip(-1), x.flip(-2), x.transpose(-1, -2)] if tta else [x]
    with torch.autocast("cuda", dtype=torch.bfloat16):
        out = model(torch.cat(xs).contiguous(memory_format=torch.channels_last)).float().sigmoid()
    if tta:
        out = (out[0] + out[1].flip(-1) + out[2].flip(-2) + out[3].transpose(-1, -2)) / 4
    else:
        out = out[0]
    return out[:, :BIG, :BIG].half().cpu().numpy()


def postprocess(prob, t_mask=0.5, t_seed=0.6, min_seed=20, min_area=20, eps=1.0):
    from shapely.geometry import Polygon
    from skimage.segmentation import watershed

    body, border, contact = [p.astype(np.float32) for p in prob]
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
    polys = []
    for l in np.unique(lab):
        if l == 0:
            continue
        m = (lab == l).astype(np.uint8)
        cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cs:
            continue
        c = max(cs, key=cv2.contourArea)
        c = cv2.approxPolyDP(c, eps, True)
        if len(c) < 3:
            continue
        p = Polygon((c[:, 0, :].astype(np.float64) + 0.5) / SCALE)
        if not p.is_valid:
            p = p.buffer(0)
        if p.area >= min_area:
            polys.append(p)
    return polys


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/sn2", help="mounted HF dataset with images/, labels/, splits.json, code/")
    ap.add_argument("--out", default="/out", help="mounted HF bucket (read-write) for outputs")
    ap.add_argument("--run", required=True)
    ap.add_argument("--encoder", default="tu-tf_efficientnetv2_m.in21k_ft_in1k")
    ap.add_argument("--arch", default="Unet")
    ap.add_argument("--steps", type=int, default=20000)
    ap.add_argument("--bs", type=int, default=12)
    ap.add_argument("--crop", type=int, default=512)
    ap.add_argument("--lr", type=float, default=4e-4)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--max_hours", type=float, default=3.0)
    ap.add_argument("--init_from", default="", help="checkpoint path to fine-tune / eval only")
    ap.add_argument("--eval_only", action="store_true")
    ap.add_argument("--ensemble", default="", help="eval-only: comma list of ckpt_path=encoder")
    args = ap.parse_args()
    t_start = time.time()

    root = Path(args.data)
    sys.path.insert(0, str(root / "code"))
    from metric import AOIS, load_solution_csv, score  # noqa

    splits = json.loads((root / "splits.json").read_text())
    gt = {}
    for a in AOIS:
        gt.update(load_solution_csv(root / "labels" / f"{a}_solutions.csv"))
    tune = [i for i in splits["train"] if int(hashlib.md5((i + "tune").encode()).hexdigest(), 16) % 100 < 5]
    tune_set = set(tune)
    train_ids = [i for i in splits["train"] if i not in tune_set]
    val_ids = splits["val"]
    log("train", len(train_ids), "tune", len(tune), "val", len(val_ids))

    # read the per-AOI uint8 shards (a few large sequential reads; fast on the mounted dataset)
    shard, where = {}, {}
    for a in AOIS:
        t = time.time()
        local = Path("/tmp") / f"{a}.npy"
        if not local.exists():
            os.system(f"cp {root / 'shards' / f'{a}.npy'} {local}")
        shard[a] = np.load(local, mmap_mode="r")
        for k, i in enumerate(json.loads((root / "shards" / f"{a}.json").read_text())):
            where[i] = (a, k)
        log("shard", a, shard[a].shape, f"{time.time() - t:.0f}s")

    def load_img(i):
        a, k = where[i]
        return np.ascontiguousarray(shard[a][k])

    out_dir = Path(args.out) / args.run
    out_dir.mkdir(parents=True, exist_ok=True)

    model = build_model(args.encoder, args.arch).cuda().to(memory_format=torch.channels_last)
    if args.init_from:
        model.load_state_dict(torch.load(args.init_from, map_location="cuda"))
        log("loaded", args.init_from)

    if not args.eval_only:
        from multiprocessing import Pool

        imgs = [load_img(i) for i in train_ids]
        with Pool(os.cpu_count()) as pool:
            masks = pool.map(render_masks, [gt.get(i, []) for i in train_ids], chunksize=32)
        log("loaded train set", f"{time.time() - t_start:.0f}s")
        ds = TileDS(imgs, masks, args.crop, args.steps * args.bs)
        dl = torch.utils.data.DataLoader(ds, batch_size=args.bs, num_workers=args.workers, drop_last=True,
                                         pin_memory=True, persistent_workers=True, prefetch_factor=4)
        opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
        warm = 500
        sched = torch.optim.lr_scheduler.LambdaLR(
            opt, lambda s: min(1, (s + 1) / warm) * 0.5 * (1 + math.cos(math.pi * min(s, args.steps) / args.steps)))
        model.train()
        t0, run_loss = time.time(), 0.0
        for step, (x, y) in enumerate(dl):
            x = x.cuda(non_blocking=True).contiguous(memory_format=torch.channels_last)
            y = y.cuda(non_blocking=True)
            with torch.autocast("cuda", dtype=torch.bfloat16):
                logits = model(x)
            loss = loss_fn(logits.float(), y)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            run_loss += loss.item()
            if (step + 1) % 200 == 0:
                el = time.time() - t0
                log(f"step {step + 1}/{args.steps} loss {run_loss / 200:.4f} lr {sched.get_last_lr()[0]:.2e} "
                    f"{(step + 1) / el:.2f} it/s eta {(args.steps - step - 1) / ((step + 1) / el) / 60:.1f} min")
                run_loss = 0.0
            if (step + 1) % 4000 == 0:
                out_dir.mkdir(parents=True, exist_ok=True)
                torch.save(model.state_dict(), out_dir / "model.pt")
            if time.time() - t_start > args.max_hours * 3600:
                log("time budget hit, stopping at step", step + 1)
                break
        del dl, imgs, masks
        out_dir.mkdir(parents=True, exist_ok=True)
        torch.save(model.state_dict(), out_dir / "model.pt")
        log("saved checkpoint")

    # ---- inference on tune + val
    model.eval()
    if args.ensemble:
        model = []
        for item in args.ensemble.split(","):
            ck, enc = item.split("=")
            m = build_model(enc, args.arch).cuda().to(memory_format=torch.channels_last)
            m.load_state_dict(torch.load(ck, map_location="cuda"))
            model.append(m.eval())
            log("ensemble member", ck, enc)

    def infer(ids):
        probs = {}
        for k, i in enumerate(ids):
            probs[i] = predict(model, load_img(i))
            if (k + 1) % 200 == 0:
                log("infer", k + 1, "/", len(ids))
        return probs

    tune_probs = infer(tune)
    grid = [dict(t_mask=tm, t_seed=ts, min_seed=ms) for tm in (0.4, 0.5) for ts in (0.5, 0.6, 0.7)
            for ms in (10, 30)]

    def run_pp(probs, kw):
        with ThreadPoolExecutor(12) as ex:
            polys = list(ex.map(lambda i: postprocess(probs[i], **kw), list(probs)))
        return dict(zip(probs, polys))

    best = None
    for kw in grid:
        r = score(gt, run_pp(tune_probs, kw), list(tune_probs))
        log("tune", kw, r["overall_f1"])
        if best is None or r["overall_f1"] > best[1]["overall_f1"]:
            best = (kw, r)
    log("best pp", best)
    del tune_probs
    val_probs = infer(val_ids)
    pred = run_pp(val_probs, best[0])
    res = score(gt, pred, val_ids)
    res["postproc"] = best[0]
    res["tune_score"] = best[1]
    res["encoder"] = args.ensemble or args.encoder
    res["arch"] = args.arch
    res["steps"] = args.steps
    res["wall_minutes"] = round((time.time() - t_start) / 60, 1)
    log("VAL", json.dumps(res))
    (out_dir / "val_results.json").write_text(json.dumps(res, indent=1))
    from metric import write_solution_csv

    write_solution_csv(pred, out_dir / "val_predictions.csv")
    np.savez_compressed(out_dir / "val_probs_u8.npz", **{
        k: (cv2.resize(np.clip(v.astype(np.float32), 0, 1).transpose(1, 2, 0), (TILE, TILE),
                    interpolation=cv2.INTER_AREA) * 255).round().astype(np.uint8)
        for k, v in val_probs.items()})
    log("done")


if __name__ == "__main__":
    main()
