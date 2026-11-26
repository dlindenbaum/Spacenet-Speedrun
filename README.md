# SpaceNet 2 Speedrun — how far have we come since 2017?

Revisiting the [SpaceNet 2 building-footprint challenge](https://medium.com/the-downlinq/2nd-spacenet-competition-winners-code-release-c7473eea7c11)
(Vegas, Paris, Shanghai, Khartoum; 0.3 m WorldView-3, 650×650 tiles) with 2026 tools:

1. **Zero-shot VLMs** (Gemma 4, Qwen3-VL, Nemotron VL) through Hugging Face Inference Providers.
2. **Zero-shot SAM 2.1** (automatic masks, SigLIP2-filtered masks, VLM-box-prompted masks) on HF Jobs GPUs.
3. **A modern supervised model** (U-Net + ImageNet-21k EfficientNetV2-M, body/border/contact heads, watershed) trained on HF Jobs, to see whether we can beat the 2017 winner.

## Target to beat (2017 final leaderboard, F1 @ IoU ≥ 0.5)

| Entry | Vegas | Paris | Shanghai | Khartoum | **Overall** |
|---|---|---|---|---|---|
| XD_XD (1st; 3× U-Net ensemble, 8-band MS + OpenStreetMap) | 0.885 | 0.745 | 0.597 | 0.544 | **0.693** |
| wleite (2nd) | | | | | 0.643 |
| nofto (3rd) | | | | | 0.579 |

Source: DeepGlobe 2018 paper, Table 2 ([arXiv:1805.06561](https://arxiv.org/abs/1805.06561)); SpaceNet paper ([arXiv:1807.01232](https://arxiv.org/abs/1807.01232)).

## Evaluation protocol

* The official test labels were never released, so everything is scored on a **held-out 10 % of the public training tiles**
  (deterministic MD5 split, `val` = 1,037 tiles). Supervised post-processing thresholds are tuned on a separate 5 % `tune`
  split carved out of train; the model never sees `val`.
* Expensive zero-shot models are scored on **`eval_small`**: a fixed random 25 val tiles per city (100 tiles, 1,649 buildings).
* Metric (`sn2/metric.py`): one-to-one greedy matching at IoU ≥ 0.5, F1 accumulated per city, overall = mean of the 4 city F1s
  (same as the 2017 leaderboard). Polygons < 20 px² are ignored on both sides. Sanity check: GT → rasterised masks →
  our watershed post-processing → polygons scores 0.998.
* Inputs: PS-RGB (pan-sharpened RGB) only, stretched to 8-bit with a fixed per-city 1–99 % percentile stretch.
  (The 2017 winner used 8-band multispectral + OSM, so RGB-only is a handicap for us.)

## Results so far

### Zero-shot VLMs — boxes (eval_small, F1 @ IoU ≥ 0.5)

Prompt: "Detect EVERY building footprint … return JSON `box_2d` normalized 0–1000". Each model is scored with whichever box
axis order it actually uses (Gemma: `[y0,x0,y1,x1]`; Qwen3-VL/Nemotron: `[x0,y0,x1,y1]`).

| Model (HF Inference Providers) | Vegas | Paris | Shanghai | Khartoum | **Overall** | #pred / #GT | API cost |
|---|---|---|---|---|---|---|---|
| google/gemma-4-26B-A4B-it | 0.271 | 0.033 | 0.160 | 0.154 | **0.155** | 899 / 1649 | $0.011 |
| google/gemma-4-31B-it | 0.247 | 0.045 | 0.124 | 0.158 | **0.143** | 937 / 1649 | $0.014 |
| Qwen/Qwen3-VL-235B-A22B-Instruct | 0.152 | 0.018 | 0.075 | 0.119 | **0.091** | 4337 / 1649 | $0.27 |
| nvidia/Nemotron-3.5-Super-VL-120B-A12B | 0.093 | 0.065 | 0.086 | 0.109 | **0.088** | 611 / 1649 | $0.16 |

Takeaway: general-purpose VLMs "see" buildings and get the coarse layout right, but their boxes are far too loose for
IoU ≥ 0.5 on 5–20 m buildings, they merge rows of houses, and they under-count dense scenes. Even the best is ~4.5× below the
2017 winner.

### Zero-shot SAM 2.1 and supervised model

Pending — the HF prepaid balance ran out mid-run (see *Status*).

## Layout

```
sn2/metric.py              SpaceNet F1 scorer (+ CSV IO)
sn2/vlm.py                 HF router client, prompts, box/polygon parsing
scripts/prepare_dataset.py S3 → 8-bit PNG tiles, labels, splits (train/val/eval_small)
scripts/make_shards.py     per-city uint8 .npy shards for fast loading in Jobs
scripts/eval_vlm.py        zero-shot VLM benchmark (cached raw responses in results/raw/)
scripts/export_prompts.py  VLM boxes → SAM box prompts
jobs/sam_eval.py           HF Jobs (GPU): SAM 2.1 AMG / AMG+SigLIP2 / box-prompted SAM
jobs/train_seg.py          HF Jobs (GPU): supervised U-Net training + tuned watershed + val scoring
results/                   JSON results, raw VLM outputs, SAM prompts
```

Data and job outputs live on the Hub (private): dataset `Rootkit29/spacenet2-speedrun` (tiles, labels, splits, shards)
and bucket `Rootkit29/sn2-runs` (checkpoints, predictions). Jobs mount both as volumes, so no token is needed inside the job:

```bash
hf jobs uv run --flavor a100-large --timeout 5h -d \
  -v hf://datasets/Rootkit29/spacenet2-speedrun:/sn2 -v hf://buckets/Rootkit29/sn2-runs:/out \
  jobs/train_seg.py --run effv2m_unet_20k --steps 20000 --bs 12 --crop 512
hf jobs uv run --flavor a100-large --timeout 90m -d \
  -v hf://datasets/Rootkit29/spacenet2-speedrun:/sn2 -v hf://buckets/Rootkit29/sn2-runs:/out \
  jobs/sam_eval.py --variants amg_all,amg_siglip,box:gt,box:gemma4_26b_g1
```

## Status

Blocked on Hugging Face prepaid credits (HTTP 402 for both Jobs and Inference Providers). Remaining runs and rough cost:

| Run | Hardware | Est. time | Est. cost |
|---|---|---|---|
| SAM 2.1 zero-shot (4 variants, 100 tiles) | A100 | ~20 min | ~$1 |
| Supervised U-Net, 20k steps + tuned eval | A100 | ~2.5–3 h | ~$7 |
| Optional 2nd model for ensembling | A100 | ~2.5 h | ~$6 |
| VLM 2×2-crop / polygon variants | Inference Providers | — | ~$1 |
