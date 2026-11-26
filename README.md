# SpaceNet 2 Speedrun — how far have we come since 2017?

Revisiting the [SpaceNet 2 building-footprint challenge](https://medium.com/the-downlinq/2nd-spacenet-competition-winners-code-release-c7473eea7c11)
(Vegas, Paris, Shanghai, Khartoum; 0.3 m WorldView-3, 650×650 tiles) with 2026 tools:

1. **Zero-shot VLMs** (Gemma 4, Qwen3-VL, Nemotron VL) through Hugging Face Inference Providers.
2. **Zero-shot SAM 2.1** (automatic masks, SigLIP2-filtered masks, VLM-box-prompted masks) on HF Jobs GPUs.
3. **A modern supervised model** (U-Net + ImageNet-21k EfficientNetV2-M, body/border/contact heads, watershed) trained on HF Jobs, to see whether we can beat the 2017 winner.

## TL;DR — yes, we win now

| Approach (2026) | Training data | Overall F1 | vs 2017 winner (0.693) | Cost |
|---|---|---|---|---|
| **ConvNeXt-B (IN-22k) U-Net, RGB only, 1 model, 53 min on 1×A100** | SN2 train | **0.755** | **+0.062, wins all 4 cities** | ~$2.20 |
| EffNetV2-M (IN-21k) U-Net, RGB only, 1 model, 56 min on 1×A100 | SN2 train | 0.731 | +0.038 | ~$2.35 |
| Ensemble of both | SN2 train | 0.752 | +0.059 | +$0.45 |
| Gemma 4 31B (2×2 crops) boxes → SAM 2.1 | **none (zero-shot)** | 0.462 | −0.23 (≈ 2/3 of winner) | ~$0.05 API + GPU |
| Gemma 4 26B-A4B boxes alone | none | 0.155 | | $0.01 |

![qualitative](results/qualitative.png)

*Left: ground truth. Middle: fully zero-shot Gemma 4 → SAM 2.1. Right: supervised ConvNeXt-B U-Net. One val tile per city.*

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

## Results

### Zero-shot VLMs (eval_small: 100 val tiles, 1,649 buildings; F1 @ IoU ≥ 0.5)

Prompt: "Detect EVERY building footprint … return JSON `box_2d` normalized 0–1000". Each model is scored with whichever box
axis order it actually emits (Gemma: `[y0,x0,y1,x1]`; Qwen3-VL/Nemotron: `[x0,y0,x1,y1]`). "2×2" = the tile is split into
4 crops, each upsampled to 650 px and queried separately.

| Model (HF Inference Providers) | Output | Vegas | Paris | Shanghai | Khartoum | **Overall** | API cost |
|---|---|---|---|---|---|---|---|
| google/gemma-4-31B-it | boxes, 2×2 crops | 0.487 | 0.138 | 0.240 | 0.252 | **0.279** | $0.04 |
| google/gemma-4-26B-A4B-it | boxes, 2×2 crops | 0.424 | 0.080 | 0.200 | 0.213 | **0.229** | $0.03 |
| google/gemma-4-26B-A4B-it | boxes | 0.271 | 0.033 | 0.160 | 0.154 | 0.155 | $0.01 |
| google/gemma-4-31B-it | boxes | 0.247 | 0.045 | 0.124 | 0.158 | 0.143 | $0.01 |
| Qwen/Qwen3-VL-235B-A22B-Instruct | boxes | 0.152 | 0.018 | 0.075 | 0.119 | 0.091 | $0.27 |
| nvidia/Nemotron-3.5-Super-VL-120B-A12B | boxes | 0.093 | 0.065 | 0.086 | 0.109 | 0.088 | $0.16 |
| google/gemma-4-31B-it | polygons | 0.000 | 0.008 | 0.008 | 0.000 | 0.004 | $0.03 |

### Zero-shot SAM 2.1 (hiera-large, tile upsampled 2×; same 100 tiles)

| Pipeline | Vegas | Paris | Shanghai | Khartoum | **Overall** |
|---|---|---|---|---|---|
| *Oracle: ground-truth boxes → SAM 2.1* | *0.982* | *0.956* | *0.973* | *0.967* | ***0.970*** |
| **Gemma 4 31B boxes (2×2 crops) → SAM 2.1** | 0.639 | 0.425 | 0.381 | 0.403 | **0.462** |
| Gemma 4 26B boxes (2×2 crops) → SAM 2.1 | 0.600 | 0.433 | 0.395 | 0.364 | 0.448 |
| Gemma 4 26B boxes → SAM 2.1 | 0.576 | 0.355 | 0.377 | 0.302 | 0.403 |
| Gemma 4 31B boxes → SAM 2.1 | 0.554 | 0.260 | 0.337 | 0.339 | 0.373 |
| SAM 2.1 automatic masks + SigLIP2 zero-shot "building roof" filter | 0.438 | 0.287 | 0.275 | 0.297 | 0.324 |
| SAM 2.1 automatic masks (everything) | 0.196 | 0.135 | 0.149 | 0.161 | 0.160 |

Takeaways:
* **SAM's masks are essentially solved**: given perfect boxes it scores 0.97. The bottleneck is *recognition* — finding every building.
* VLMs alone can't produce footprints (boxes too loose for IoU ≥ 0.5, polygons are nonsense), but their boxes make excellent
  SAM prompts: **VLM → SAM is a 0.46 fully zero-shot pipeline** — ~2/3 of the 2017 winner with zero SpaceNet training data,
  for a few cents of API calls. Cropping (more pixels per building) is the biggest single lever for the VLMs.
* Recall is the limiter (Gemma finds ~50–60 % of buildings, under-counting dense blocks).

### Supervised (full held-out `val`: 1,037 tiles, 20,859 buildings)

Recipe (`jobs/train_seg.py`): U-Net (segmentation_models_pytorch) with a timm ImageNet-21k/22k encoder; 3 sigmoid outputs
(building body, 2-px border, building-building contact) trained with weighted BCE + Dice; images upsampled 1.5× (0.2 m/px);
random 512 px crops with scale jitter, flips/rot90, photometric jitter; AdamW, cosine LR, bf16, 20k steps × batch 12
(≈ 25 epochs). Inference: 4-way flip/transpose TTA, marker-controlled watershed (seeds = body·(1−border)·(1−contact)),
`approxPolyDP` polygons. Watershed thresholds tuned on the separate `tune` split.

| Model | Vegas | Paris | Shanghai | Khartoum | **Overall** |
|---|---|---|---|---|---|
| XD_XD — 2017 winner (8-band MS + OSM, 3-model ensemble; official test set) | 0.885 | 0.745 | 0.597 | 0.544 | 0.693 |
| **ConvNeXt-B U-Net (RGB only)** | **0.892** | **0.807** | **0.709** | 0.611 | **0.755** |
| EffNetV2-M U-Net (RGB only) | 0.878 | 0.768 | 0.691 | 0.586 | 0.731 |
| Ensemble (mean of both; wider pp grid) | 0.890 | 0.800 | 0.706 | **0.612** | 0.752 |

Biggest gains are in the hard cities (Shanghai +0.11, Khartoum +0.07, Paris +0.06); Vegas was already near saturation in 2017.
Caveat: our `val` is a held-out slice of the public training AOIs, not the (unreleased) official test set, so numbers are
comparable but not identical to the leaderboard. Precision/recall per city are in `results/supervised/*.json`.

### How general is it? Other SpaceNet building datasets

All other SpaceNet building datasets are resampled to SN2's 0.3 m GSD, converted to 8-bit RGB, and cut into sub-tiles of at most 1,600 px
(`scripts/prepare_xds.py`). Scoring uses the same F1 @ IoU ≥ 0.5 scorer, with TP/FP/FN pooled per group. Test splits are
held out **by location**: in SN4, a test location is test at all 27 look angles.

* **SN2-only**: the 0.755 ConvNeXt-B above, applied zero-shot.
* **Generalist**: the same recipe, trained on SN2 + the SN4/SN6/SN8 train splits (60 % of crops from the extra datasets, area-weighted),
  20k steps, 81 min on 1×A100.
* **SN7** (Planet 4 m) is never trained on; it is a pure unseen-sensor test.
* **SN1** (Rio) is excluded because its labels are poor quality.

| Dataset (test split) | SN2-only F1 | **Generalist F1** | Generalist P / R |
|---|---|---|---|
| SN2 val (in-domain) | 0.755 | 0.743 | (per city in json) |
| SN4 Atlanta, nadir ≤ 25° | 0.394 | **0.722** | 0.77 / 0.68 |
| SN4 Atlanta, off-nadir 26–39° | 0.195 | **0.489** | 0.56 / 0.44 |
| SN4 Atlanta, very off-nadir ≥ 40° | 0.025 | **0.246** | 0.37 / 0.18 |
| SN6 Rotterdam (optical PS-RGB) | 0.322 | **0.471** | 0.75 / 0.34 |
| SN8 Louisiana-East (pre-flood) | 0.534 | **0.813** | 0.82 / 0.80 |
| SN8 Germany (pre-flood) | 0.107 | **0.600** | 0.57 / 0.63 |
| SN7 Planet 4 m (unseen) | 0.0005 | 0.000 | – |

Takeaways:
* **A model trained on one dataset is narrow.** Out of domain, the SN2-only model stays fairly precise but misses most buildings,
  and it collapses as look angle grows.
* **A single model trained on the pooled data is general.** It loses only 0.012 on SN2 and roughly doubles F1 on every other optical
  0.3 m dataset.
  * For reference, the SN4 paper's best baseline (TernausNet, trained on SN4) scored 0.62 / 0.43 / 0.22 on the three angle bands.
    Our generalist scores 0.72 / 0.49 / 0.25, but on our own location-held-out split, not the official test set.
* **Some SN4 collects stay near zero even for the generalist**: 29°, 32° and 53°, even though 29° and 53° were in training.
  Most other collects at similar angles score 0.5–0.7, so this looks like image/label misregistration in those collects, not a
  model limit. This has not been verified.
* **SN6's low recall** is a domain gap: Rotterdam's optical tiles are dense row houses with an unusual colour balance.
* **SN7 is out of reach** for any 0.3 m model. At 4 m a house is about 2–3 px, and resampling to 0.3 m cannot create that detail.
  Sensor resolution, not the model, is the limit.
* Reference scores from the other challenges are not comparable: the SN6 winner (~0.42) used SAR, and SN8 was scored with a
  different, flood-attribute metric.

Per-angle and per-region numbers: `results/transfer/{sn2only,generalist_nosn1}_convnextb.json`; run with `jobs/transfer_eval.py`.

## Layout

```
sn2/metric.py              SpaceNet F1 scorer (+ CSV IO)
sn2/vlm.py                 HF router client, prompts, box/polygon parsing
scripts/prepare_dataset.py S3 → 8-bit PNG tiles, labels, splits (train/val/eval_small)
scripts/make_shards.py     per-city uint8 .npy shards for fast loading in Jobs
scripts/eval_vlm.py        zero-shot VLM benchmark (cached raw responses in results/raw/)
scripts/export_prompts.py  VLM boxes → SAM box prompts
jobs/sam_eval.py           HF Jobs (GPU): SAM 2.1 AMG / AMG+SigLIP2 / box-prompted SAM
jobs/train_seg.py          HF Jobs (GPU): supervised U-Net training + tuned watershed + val scoring (--extra sn4,sn6,sn8)
scripts/prepare_xds.py     other SpaceNet building datasets → 0.3 m RGB sub-tiles, labels, location splits
jobs/transfer_eval.py      HF Jobs (GPU): cross-dataset scoring per angle band / region
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

## Compute & cost

Everything ran on Hugging Face: Inference Providers for VLMs (~$0.70 total) and HF Jobs (1×A100-80GB, $2.50/h) for SAM and
training (~2.8 GPU-hours incl. a smoke test and a billing-interrupted SAM run, ≈ $7). The generalization study
(data prep, generalist training incl. a cancelled run with SN1, two transfer evals) added ≈ $14. Total ≈ $22.

## Ideas not tried (yet)

* SAM 3 (text-prompted "building" segmentation) — gated model; needs access approval on the Hub.
* 8-band multispectral input, larger crops / longer schedules, more ensemble members, multi-scale TTA.
* Fine-tuning SAM 2.1 / a satellite-pretrained ViT backbone; using the supervised model's boxes as SAM prompts.
