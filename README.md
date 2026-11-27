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
| DINOv3 ViT-L (satellite-pretrained, SAT-493M) U-Net, 128 min on 1×A100 | SN2 train | 0.716 | +0.023 | ~$5.30 |
| **SAM 3, text prompt "building"** | **none (zero-shot)** | **0.642** | −0.051 (≈ 2017's 2nd place) | ~$1 GPU |
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

### Zero-shot SAM 3 (text prompt, full held-out `val`: 1,037 tiles)

`jobs/sam3_eval.py`: SAM 3 (`facebook/sam3`, transformers `Sam3Model`) prompted with a single word on the whole RGB tile;
instances above a score threshold become polygons. Prompt, crop grid and threshold were chosen on `eval_small`
("building" > "house" > "roof"; 2×2 crops hurt), then the whole `val` split was scored once.

| Split / threshold | Vegas | Paris | Shanghai | Khartoum | **Overall** |
|---|---|---|---|---|---|
| eval_small (100 tiles), 0.4 | 0.855 | 0.672 | 0.589 | 0.567 | 0.671 |
| **val (1,037 tiles), 0.4** | 0.810 | 0.678 | 0.584 | 0.497 | **0.642** |
| val, best threshold (0.35) | | | | | 0.644 |

* SAM 3 with one English word is **0.18 better than the best SAM 2.1 / VLM pipeline** and about level with 2017's
  2nd place (0.643), 0.05 behind the winner. It needs no SpaceNet labels at all.
* Precision is decent (0.67–0.88); **recall is the limit** in the dense cities (Khartoum 0.40, Shanghai 0.51):
  it merges or skips small touching buildings.
* The `eval_small` estimate (0.671) was optimistic; it over-weights the easier tiles.

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
| DINOv3 ViT-L/16 SAT-493M U-Net (features from blocks 6/12/18/24 + conv stem; backbone LR 0.1×) | 0.867 | 0.746 | 0.690 | 0.559 | 0.716 |

The satellite-pretrained DINOv3 backbone is **worse** than ImageNet ConvNeXt-B here (−0.04) despite 3× the parameters and
2.4× the training time: at 16 px patches its features are coarse for 3–15 m buildings, and the conv stem/decoder has to
recover the boundary detail. Satellite pretraining does not replace a convolutional, high-resolution encoder for footprints.

**Post-processing is saturated.** `scripts/pp_search.py` re-runs the watershed on the saved val probabilities over
36 threshold settings × polygon dilation (0–1.5 px) × minimum area (20–100 px), with 2-fold cross-validation over `val`
(choose on one half, score on the other). Best honest gain: **+0.005** (0.7555 → 0.7607 with per-city settings;
0.7575 with one global setting). The in-sample optimum is only 0.761, and the chosen settings differ between folds, so
the remaining gap is in the probabilities themselves, not the polygon extraction (`results/supervised/pp_search_convnextb.json`).

Biggest gains are in the hard cities (Shanghai +0.11, Khartoum +0.07, Paris +0.06); Vegas was already near saturation in 2017.
Caveat: our `val` is a held-out slice of the public training AOIs, not the (unreleased) official test set, so numbers are
comparable but not identical to the leaderboard. Precision/recall per city are in `results/supervised/*.json`.

### Do the 8 multispectral bands matter? No.

XD_XD used all 8 WorldView-3 bands. To test whether they help a modern model, the 8-band pansharpened product (PS-MS, 0.3 m)
was converted with the same per-city 1–99 % stretch as our RGB tiles (`jobs/prep_ms.py`), and the ConvNeXt-B recipe was
retrained three times, changing only the input bands (`train_seg.py --bands`). Extra bands enter through the first conv
with **zero-initialised** filters, so each run starts as the RGB model and only changes if the bands carry signal.

| Input (all from PS-MS unless noted) | Vegas | Paris | Shanghai | Khartoum | **Overall** | Δ vs MS-RGB, 95 % CI |
|---|---|---|---|---|---|---|
| PS-RGB product (the 0.755 model) | 0.892 | 0.807 | 0.709 | 0.611 | 0.755 | −0.002 … +0.009 |
| MS red/green/blue (bands 5,3,2) | 0.899 | 0.796 | 0.705 | 0.605 | 0.752 | – |
| MS RGB + NIR1 | 0.896 | 0.802 | 0.709 | 0.595 | 0.750 | −0.007 … +0.004 |
| **MS all 8 bands** (+ coastal, yellow, red-edge, NIR2) | 0.893 | 0.799 | 0.709 | 0.608 | 0.752 | −0.004 … +0.006 |

CI: paired bootstrap over val tiles, resampled within each city (`scripts/band_ablation.py`, `results/supervised/band_ablation.json`).

* **No band combination moves the score beyond run-to-run noise** (all differences ≤ 0.004, every CI contains 0),
  overall or in any city. NIR does not help even in Khartoum, where bare soil vs. roof vs. vegetation was the
  expected win. Building footprints at 0.3 m are a *spatial* problem (edges, shape, shadow); colour from RGB is enough.
* Spectral indices (NDVI etc.) were not run separately: they are fixed functions of red and NIR, and the network with NIR
  available gained nothing, so a precomputed index is very unlikely to.
* PAN is not a separate input: both pansharpened products are already built at PAN's 0.3 m resolution.
* XD_XD's gains from multispectral in 2017 likely came from his small U-Net trained from scratch; an ImageNet-pretrained
  encoder already extracts what it needs from RGB.
* Data note: SpaceNet's PS-MS files are blank (all zeros) for 10 Vegas tiles whose RGB files are fine (2 of them in val);
  excluding them changes no score by more than 0.0001.

### How general is it? Other SpaceNet building datasets

All other SpaceNet building datasets are resampled to SN2's 0.3 m GSD, converted to 8-bit RGB, and cut into sub-tiles of at most 1,600 px
(`scripts/prepare_xds.py`). Scoring uses the same F1 @ IoU ≥ 0.5 scorer, with TP/FP/FN pooled per group. Test splits are
held out **by location**: in SN4, a test location is test at all 27 look angles, and none of the 40 test locations appears in training
(0 overlap; the 300 train locations are disjoint).
`scripts/check_leakage.py` checks the other datasets by geographic footprint:
* **SN8 is clean**: 0 of 48 Germany and 3 of 142 Louisiana test tiles touch a training image, covering 1.4 % of test area on average.
* **SN6 leaked under the original split**: its tiles come from overlapping imaging strips, so a split by tile number did not
  separate ground (99.8 % of test area was covered by training images). SN6 is now split **geographically**: 1 km grid
  cells are hashed to test, and no training tile may come within 50 m of a test tile. Result: 300 test / 1,495 train tiles,
  0 of 300 test tiles overlapping training. The generalist was retrained on this split; all numbers below use it.

* **SN2-only**: the 0.755 ConvNeXt-B above, applied zero-shot.
* **Generalist**: the same recipe, trained on SN2 + the SN4/SN6/SN8 train splits (60 % of crops from the extra datasets, area-weighted),
  20k steps, 81 min on 1×A100.
* **SN7** (Planet 4 m) is never trained on; it is a pure unseen-sensor test.
* **SN1** (Rio) is excluded because its labels are poor quality.

| Dataset (test split) | SN2-only F1 | **Generalist F1** | Generalist P / R |
|---|---|---|---|
| SN2 val (in-domain) | 0.755 | 0.738 | (per city in json) |
| SN4 Atlanta, nadir ≤ 25° | 0.394 | **0.735** | 0.77 / 0.70 |
| SN4 Atlanta, off-nadir 26–39° | 0.195 | **0.504** | 0.57 / 0.45 |
| SN4 Atlanta, very off-nadir ≥ 40° | 0.025 | **0.271** | 0.40 / 0.21 |
| SN6 Rotterdam (optical PS-RGB, geographic split) | 0.335 | **0.436** | 0.73 / 0.31 |
| SN8 Louisiana-East (pre-flood) | 0.534 | **0.809** | 0.81 / 0.81 |
| SN8 Germany (pre-flood) | 0.107 | **0.581** | 0.56 / 0.60 |
| SN7 Planet 4 m (unseen) | 0.0005 | 0.000 | – |

The generalist trains 40 % of crops on SN2 and 20 % each on SN4 (3,000 tiles but only 300 distinct locations), SN6
(1,495 tiles) and SN8 (1,217 tiles). An earlier generalist on the leaky SN6 split scored 0.743 on SN2 and within 0.02 of
these numbers elsewhere (`results/transfer/generalist_nosn1_convnextb.json`).

Takeaways:
* **A model trained on one dataset is narrow.** Out of domain, the SN2-only model stays fairly precise but misses most buildings,
  and it collapses as look angle grows.
* **A single model trained on the pooled data is general.** It loses only 0.017 on SN2 and roughly doubles F1 on SN4 and SN8.
  * For reference, the SN4 paper's best baseline (TernausNet, trained on SN4) scored 0.62 / 0.43 / 0.22 on the three angle bands.
    Our generalist scores 0.74 / 0.50 / 0.27, but on our own location-held-out split, not the official test set.
* **SN6 stays hard** (0.44): recall is only 0.31 on Rotterdam's dense, small, shadowed buildings, and the generalist saw just
  1,495 SN6 tiles. The SN6 challenge winner (~0.42) used SAR, so the scores are not comparable.
* **Some SN4 collects stay near zero even for the generalist**: 29°, 32° and 53° (0.21 / 0.04 / 0.06), even though 29° and 53° were in training.
  Most other collects at similar angles score 0.5–0.7, so this looks like image/label misregistration in those collects, not a
  model limit. This has not been verified.
* **SN7 is out of reach** for any 0.3 m model. At 4 m a house is about 2–3 px, and resampling to 0.3 m cannot create that detail.
  Sensor resolution, not the model, is the limit.
* Reference scores from SN8 are not comparable: it was scored with a different, flood-attribute metric.

**Can it generalise to cities it has never seen?** The same recipe trained only on SN4 + SN6 + SN8 (no SN2 at all; fixed
post-processing, so no SN2 labels used anywhere) scores **0.325** on SN2 val: Vegas 0.497, Paris 0.569, Shanghai 0.184,
Khartoum 0.051 (precision 0.5–0.7, recall 0.03–0.47; `results/supervised/nosn2_convnextb_20k.json`). Three building
datasets from the US and Europe do not cover dense Chinese blocks or Sudanese compounds, and even US suburbs lose most
recall to SN2's tighter labelling. **Zero-shot SAM 3 (0.642) is twice as good on unseen cities** as this supervised model.

Per-angle and per-region numbers: `results/transfer/{sn2only,generalist_geo}_convnextb.json` and
`results/transfer/sn2only_convnextb_sn6geo.json`; run with `jobs/transfer_eval.py`.

### Reproducing XD_XD's 2017 winner on our split (Paris and Khartoum)

To get a strict 1:1 comparison, `jobs/xdxd/` runs XD_XD's original code
([BuildingDetectors_Round2/1-XD_XD](https://github.com/SpaceNetChallenge/BuildingDetectors_Round2)) **unmodified** on our
train/val split: per-city v9s + v13 + v16 (OSM) U-Nets, the v17 ensemble, his own thresholds and min-area tuning.
His SN2 weights were never released, so it is retrained. Only the 2017 environment is replaced:
* Keras 1.1 / Theano 0.9 / CUDA 8 cannot run on current GPUs. `jobs/xdxd/shim/keras` re-implements the Keras-1 API
  that his code uses (layers, `fit`/`fit_generator`, checkpoints, early stopping) on PyTorch.
* 2017 library behaviour: numpy 1.23, pandas 1.5, shapely 1.8 (patched so empty polygons behave like shapely 1.5),
  click 6.7, `gdal` replaced by a rasterio shim. CUDA must be initialised before shapely 1.8 is imported, or torch segfaults.
* His OpenStreetMap layers came from Mapzen Metro Extracts, which went offline in 2018. `jobs/xdxd/osm_2017.py` rebuilds them
  from the **2017-05-01 OSM planet** (61 GB) in the same imposm shapefile layout.

Scored with our metric on our `val` tiles (`results/xdxd/`):

| City | XD_XD retrained, our val (P / R) | XD_XD 2017 leaderboard | **ConvNeXt-B U-Net** | SAM 3 zero-shot |
|---|---|---|---|---|
| Paris (105 tiles) | 0.697 (0.77 / 0.64) | 0.745 | **0.807** | 0.678 |
| Khartoum (87 tiles) | 0.512 (0.62 / 0.44) | 0.544 | **0.611** | 0.497 |

* The retrained solution lands 0.03–0.05 below its leaderboard scores, which is plausible for a different test set and a
  rebuilt OSM/framework stack. It is a faithful reproduction, not a tuned one.
* **On identical tiles the 2026 model is ~0.10 ahead in both cities**, a slightly larger margin than against the leaderboard.
* SAM 3 with no training is within 0.02 of the retrained 2017 winner in both cities.
* Vegas and Shanghai were not completed: with batch size 2, v13/v16 take ~2.5–3 h each per city even on an A100,
  and the runs were cancelled when they could not finish within the remaining credit (models are only saved at the end).

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
jobs/sam3_eval.py          HF Jobs (GPU): zero-shot SAM 3 text-prompted segmentation
scripts/check_leakage.py   geographic train/test overlap audit (SN6, SN8)
jobs/xdxd/                 XD_XD 2017 winner reproduction: Keras-1 → PyTorch shim, data staging, 2017 OSM rebuild
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
(data prep, generalist training incl. a cancelled run with SN1, two transfer evals) added ≈ $14. The second phase
(geo-split generalist, DINOv3, SAM 3, the 2017 OSM rebuild on a CPU job and the partial XD_XD reproduction) used roughly
another $20–25; the no-SN2 run and the XD_XD runs (including the cancelled Vegas/Shanghai attempts) about $24 more; the multispectral ablation about $9.

## Ideas not tried (yet)

* Finish XD_XD for Vegas and Shanghai (high-RAM GPU, ~6 h each; save models per stage first).
* Fine-tune SAM 3 on the pooled SpaceNet data, combining its open-world recognition with SpaceNet label style.
* Larger crops / longer schedules, more ensemble members, multi-scale TTA.
* Fine-tuning SAM 3 on SpaceNet; using the supervised model's boxes as SAM prompts.
