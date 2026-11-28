# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "torch>=2.4", "torchvision", "timm>=1.0.9", "segmentation-models-pytorch>=0.4",
#   "opencv-python-headless", "shapely>=2", "scikit-image", "numpy", "tqdm", "pillow",
# ]
# ///
"""HF Job (GPU): SpaceNet-4 (off-nadir) model with the winner's ideas added to our recipe, trained, tuned and scored
in one run on the native 0.5 m set from jobs/prep_sn4_full.py.

Changes against the plain recipe (0.642 mean-of-bands):
  --cond      angle-aware input, as cannab: constant planes for |look angle|/54 and a one-hot of the 27 collects
              (catalog ids), appended to RGB; the first conv's new filters start at zero
  --scale     internal upsampling (0.5 m x scale); the SN2 model ran at 0.2 m effective, the plain SN4 run at 0.33 m
  tuning      ~5 % of the TRAINING locations (whole locations, all 27 collects) are held out and used to pick the
              watershed thresholds per look-angle band; the test locations are never used for any choice
Evaluation uses the challenge metric: per-building F1 (IoU >= 0.5) pooled per band, mean of the three bands.
--eval_models a,b,... skips training and averages the models' probabilities (an ensemble of runs with the
same --scale and --cond).
"""
import argparse
import hashlib
import itertools
import json
import math
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
import torch

cv2.setNumThreads(1)
T = None
BANDS = ("NADIR", "OFF", "VOFF")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def collect_of(tid):
    return tid.split("_")[0]  # e.g. nadir10-993E00


class CondDS(torch.utils.data.Dataset):
    """train_seg.TileDS plus constant conditioning planes."""

    def __init__(self, imgs, masks, conds, crop, n):
        self.imgs, self.masks, self.conds, self.crop, self.n = imgs, masks, conds, crop, n

    def __len__(self):
        return self.n

    def __getitem__(self, i):
        rng = np.random.default_rng()
        k = rng.integers(len(self.imgs))
        img, msk = self.imgs[k], self.masks[k]
        H, W = img.shape[:2]
        s = T.SCALE * rng.uniform(0.8, 1.25)
        src = min(int(self.crop / s), H, W)
        y0, x0 = rng.integers(0, H - src + 1), rng.integers(0, W - src + 1)
        im = cv2.resize(img[y0:y0 + src, x0:x0 + src], (self.crop, self.crop), interpolation=cv2.INTER_LINEAR)
        mx0, my0, msrc = int(x0 * T.SCALE), int(y0 * T.SCALE), int(src * T.SCALE)
        mk = cv2.resize(msk[my0:my0 + msrc, mx0:mx0 + msrc], (self.crop, self.crop), interpolation=cv2.INTER_NEAREST)
        if rng.random() < 0.5:
            im, mk = im[:, ::-1], mk[:, ::-1]
        r = rng.integers(4)
        im, mk = np.rot90(im, r), np.rot90(mk, r)
        im = im.astype(np.float32) / 255.0
        if rng.random() < 0.8:
            im = np.clip((im - 0.5) * rng.uniform(0.8, 1.2) + 0.5 + rng.uniform(-0.1, 0.1), 0, 1)
            im = im ** rng.uniform(0.8, 1.25)
        x = ((im - T.MEAN[:3]) / T.STD[:3]).transpose(2, 0, 1)
        if self.conds is not None:
            c = self.conds[k]
            x = np.concatenate([x, np.broadcast_to(c[:, None, None], (len(c), self.crop, self.crop))], 0)
        y = np.stack([(mk >> b) & 1 for b in range(3)]).astype(np.float32)
        return torch.from_numpy(np.ascontiguousarray(x, dtype=np.float32)), torch.from_numpy(y.copy())


@torch.no_grad()
def predict(models, img, cond, scale):
    """img uint8 HxWx3 (native) -> probs (3, H*scale, W*scale); 4-view flip/transpose TTA, mean over models."""
    H, W = img.shape[:2]
    h, w = int(round(H * scale)), int(round(W * scale))
    im = cv2.resize(img, (w, h), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255.0
    im = ((im - T.MEAN[:3]) / T.STD[:3]).transpose(2, 0, 1)
    if cond is not None:
        im = np.concatenate([im, np.broadcast_to(cond[:, None, None], (len(cond), h, w))], 0)
    ph, pw = (h + 31) // 32 * 32, (w + 31) // 32 * 32
    x = np.zeros((im.shape[0], ph, pw), np.float32)
    x[:, :h, :w] = im
    x = torch.from_numpy(x).cuda()[None]
    acc, n = 0, 0
    for m in models:
        for f, inv in ((lambda t: t, lambda t: t), (lambda t: t.flip(-1), lambda t: t.flip(-1)),
                       (lambda t: t.flip(-2), lambda t: t.flip(-2)), (lambda t: t.transpose(-1, -2), lambda t: t.transpose(-1, -2))):
            xi = f(x)
            with torch.autocast("cuda", dtype=torch.bfloat16):
                acc = acc + inv(m(xi.contiguous(memory_format=torch.channels_last)).float().sigmoid())
            n += 1
    return (acc / n)[0, :, :h, :w].half().cpu().numpy()


def pp_worker(args):
    """One tile, many post-processing settings: (tp, fp, fn) per setting. The probability map crosses the process
    boundary once per tile."""
    prob, kws, gt_wkt = args
    from shapely import wkt as W
    from metric import match_tile
    gt = [W.loads(s) for s in gt_wkt]
    prob = prob.astype(np.float32)
    return [match_tile(gt, T.postprocess(prob, **kw)) for kw in kws]


def _band_of_angle(ang):
    return "NADIR" if ang <= 25 else ("OFF" if ang < 40 else "VOFF")


def f1(c):
    tp, fp, fn = c
    return 2 * tp / max(2 * tp + fp + fn, 1)


def main():
    global T
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/sn2")
    ap.add_argument("--out", default="/out")
    ap.add_argument("--xds", default="/out/xds/sn4n")
    ap.add_argument("--run", required=True)
    ap.add_argument("--encoder", default="tu-convnext_base.fb_in22k_ft_in1k")
    ap.add_argument("--scale", type=float, default=2.0)
    ap.add_argument("--cond", action="store_true")
    ap.add_argument("--steps", type=int, default=20000)
    ap.add_argument("--bs", type=int, default=12)
    ap.add_argument("--crop", type=int, default=512)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--tune_pct", type=int, default=5, help="percent of training locations held out for tuning")
    ap.add_argument("--eval_models", default="", help="comma list of checkpoints: skip training, ensemble them")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0, help="smoke test: first N ids of each split")
    a = ap.parse_args()
    t_start = time.time()
    sys.path.insert(0, str(Path(a.data) / "code"))
    import train_seg as _T
    T = _T
    T.SCALE = a.scale
    torch.manual_seed(a.seed)
    np.random.seed(a.seed)
    from shapely import wkt as W

    d = Path(a.xds)
    labels = json.loads((d / "labels.json").read_text())
    meta = json.loads((d / "meta.json").read_text())
    sp = json.loads((d / "splits.json").read_text())
    collects = sorted({collect_of(i) for i in meta})
    cidx = {c: k for k, c in enumerate(collects)}

    def cond_of(tid):
        if not a.cond:
            return None
        v = np.zeros(1 + len(collects), np.float32)
        v[0] = meta[tid]["angle"] / 54.0
        v[1 + cidx[collect_of(tid)]] = 1.0
        return v

    in_ch = 3 + (1 + len(collects) if a.cond else 0)
    is_tune = lambda tid: int(hashlib.md5((meta[tid]["loc"] + "sn4tune").encode()).hexdigest(), 16) % 100 < a.tune_pct
    train_ids = [i for i in sp["train"] if not is_tune(i)]
    tune_ids = [i for i in sp["train"] if is_tune(i)]
    test_ids = sp["test"]
    if a.limit:
        train_ids, tune_ids, test_ids = train_ids[:a.limit], tune_ids[:a.limit], test_ids[:a.limit]
    log(f"collects {len(collects)}, train {len(train_ids)}, tune {len(tune_ids)} "
        f"({len({meta[i]['loc'] for i in tune_ids})} locations), test {len(test_ids)}, in_ch {in_ch}, scale {a.scale}")

    def load(i):
        return cv2.cvtColor(cv2.imread(str(d / "images" / f"{i}.png")), cv2.COLOR_BGR2RGB)

    out_dir = Path(a.out) / a.run
    out_dir.mkdir(parents=True, exist_ok=True)
    if a.eval_models:
        models = []
        for ck in a.eval_models.split(","):
            m = T.build_model(a.encoder, in_ch=in_ch).cuda().to(memory_format=torch.channels_last).eval()
            m.load_state_dict(torch.load(ck, map_location="cuda"))
            models.append(m)
        log("ensemble of", len(models))
    else:
        from multiprocessing import Pool

        with ThreadPoolExecutor(64) as ex:
            imgs = list(ex.map(load, train_ids))
        log("loaded images", f"{time.time() - t_start:.0f}s")
        with Pool(16) as pool:
            masks = pool.map(T.render_masks, [([W.loads(w) for w in labels[i]], im.shape[:2])
                                              for i, im in zip(train_ids, imgs)], chunksize=16)
        conds = [cond_of(i) for i in train_ids] if a.cond else None
        log("masks", f"{time.time() - t_start:.0f}s")
        model = T.build_model(a.encoder, in_ch=in_ch).cuda().to(memory_format=torch.channels_last)
        ds = CondDS(imgs, masks, conds, a.crop, a.steps * a.bs)
        dl = torch.utils.data.DataLoader(ds, batch_size=a.bs, num_workers=10, drop_last=True, pin_memory=True,
                                         persistent_workers=True, prefetch_factor=4)
        opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=1e-4)
        warm = 500
        sched = torch.optim.lr_scheduler.LambdaLR(
            opt, lambda s: min(1, (s + 1) / warm) * 0.5 * (1 + math.cos(math.pi * min(s, a.steps) / a.steps)))
        model.train()
        t0, run_loss = time.time(), 0.0
        for step, (x, y) in enumerate(dl):
            x = x.cuda(non_blocking=True).contiguous(memory_format=torch.channels_last)
            y = y.cuda(non_blocking=True)
            with torch.autocast("cuda", dtype=torch.bfloat16):
                logits = model(x)
            loss = T.loss_fn(logits.float(), y)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            run_loss += loss.item()
            if (step + 1) % 500 == 0:
                el = time.time() - t0
                log(f"step {step + 1}/{a.steps} loss {run_loss / 500:.4f} {(step + 1) / el:.2f} it/s")
                run_loss = 0.0
        del dl, imgs, masks
        torch.save(model.state_dict(), out_dir / "model.pt")
        log("saved", f"{time.time() - t_start:.0f}s")
        model.eval()
        models = [model]

    # ---- probabilities for tune + test
    def probs_for(ids):
        out = {}
        with ThreadPoolExecutor(8) as ex:
            for k, (i, img) in enumerate(zip(ids, ex.map(load, ids))):
                out[i] = predict(models, img, cond_of(i), a.scale)
                if (k + 1) % 200 == 0:
                    log("infer", k + 1, "/", len(ids))
        return out

    ms_f = (a.scale / 1.5) ** 2  # seed sizes are in internal pixels; keep them fixed in ground area
    grid = [dict(t_mask=tm, t_seed=ts, min_seed=int(round(ms * ms_f))) for tm, ts, ms in
            itertools.product((0.3, 0.4, 0.5), (0.6, 0.7, 0.8), (30, 60))]
    fixed = dict(t_mask=0.4, t_seed=0.7, min_seed=int(round(30 * ms_f)))
    pool = ProcessPoolExecutor(a.workers)

    def score_many(probs, kws):
        """-> list over kws of {band / angle: [tp, fp, fn]}"""
        futs = {i: pool.submit(pp_worker, (p, kws, labels[i])) for i, p in probs.items()}
        cs = [defaultdict(lambda: np.zeros(3)) for _ in kws]
        for i, f in futs.items():
            for c, r in zip(cs, f.result()):
                c[meta[i]["band"]] += r
                c[f"angle:{meta[i]['angle']:02d}"] += r
        return cs

    tune_probs = probs_for(tune_ids)
    log("tune inference done", f"{time.time() - t_start:.0f}s")
    best = {}
    tune_scores = {}
    for kw, c in zip(grid, score_many(tune_probs, grid)):
        tune_scores[json.dumps(kw)] = {b: round(f1(c[b]), 4) for b in BANDS}
        for b in BANDS:
            if b not in best or f1(c[b]) > best[b][1]:
                best[b] = (kw, f1(c[b]))
    log("best pp per band (tune):", {b: (best[b][0], round(best[b][1], 4)) for b in BANDS})
    del tune_probs
    test_probs = probs_for(test_ids)
    log("test inference done", f"{time.time() - t_start:.0f}s")
    res = {"run": a.run, "scale": a.scale, "cond": a.cond, "eval_models": a.eval_models,
           "n_train": len(train_ids), "n_tune": len(tune_ids), "n_test": len(test_ids)}
    kws = [fixed] + [best[b][0] for b in BANDS]
    cs = score_many(test_probs, kws)
    for name, kwb in (("fixed_pp", {b: fixed for b in BANDS}), ("tuned_pp", {b: best[b][0] for b in BANDS})):
        c = defaultdict(lambda: np.zeros(3))
        for b in BANDS:  # each band scored with its own setting
            cb = cs[kws.index(kwb[b])]
            c[b] = cb[b]
            for k, v in cb.items():
                if k.startswith("angle") and _band_of_angle(int(k.split(":")[1])) == b:
                    c[k] = v
        bands = {b: round(f1(c[b]), 4) for b in BANDS}
        res[name] = {"mean_of_bands_f1": round(float(np.mean(list(bands.values()))), 4), **bands,
                     "pp": {b: kwb[b] for b in BANDS},
                     "precision_recall": {b: [round(c[b][0] / max(c[b][0] + c[b][1], 1), 4),
                                              round(c[b][0] / max(c[b][0] + c[b][2], 1), 4)] for b in BANDS},
                     "per_angle": {k.split(":")[1]: round(f1(v), 4) for k, v in sorted(c.items()) if k.startswith("angle")}}
        log(name, json.dumps({k: v for k, v in res[name].items() if k not in ("per_angle", "pp")}))
    res["tune_grid"] = tune_scores
    res["wall_s"] = round(time.time() - t_start)
    (out_dir / "sn4_results.json").write_text(json.dumps(res, indent=1))
    log("done", res["wall_s"], "s")


if __name__ == "__main__":
    main()
