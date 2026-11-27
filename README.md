# SpaceNet 2 Speedrun: how far have we come since 2017?

The [SpaceNet 2 building-footprint challenge](https://medium.com/the-downlinq/2nd-spacenet-competition-winners-code-release-c7473eea7c11)
(2017) asked for every building footprint in 0.3 m WorldView-3 imagery of four cities: Las Vegas, Paris, Shanghai and
Khartoum (650 × 650 px tiles). This repo re-runs it with 2026 tools as a measure of progress. It covers:

1. **Zero-shot models** with no SpaceNet training: vision-language models (Gemma 4, Qwen3-VL, Nemotron VL), SAM 2.1
   (with and without VLM box prompts) and SAM 3 (text prompt "building").
2. **A modern supervised model** (ConvNeXt-B U-Net with watershed post-processing, about an hour on one GPU), plus
   EfficientNetV2-M, a DINOv3 satellite-pretrained ViT and an ensemble.
3. **A 1:1 rerun of the 2017 winner** (XD_XD's original code, retrained on our split) to compare on identical tiles.
4. **Ablations**: post-processing search, 8-band multispectral vs RGB input.
5. **Generalisation**: the same models on the other SpaceNet building datasets (SN4, SN6, SN7, SN8), a model trained on
   all of them, and a model trained without SN2 tested on the SN2 cities.

All compute ran on Hugging Face (Inference Providers for the VLMs, HF Jobs for GPUs and CPUs). The work was done by Claude
(Claude Code, Opus 5.5) over about 38 hours of wall-clock time. **Total cost ≈ $115: $73 Hugging Face + $42 Claude**
([details](#cost-claude-and-hugging-face)). The full session trace is in [`trace/`](#session-trace). The headline
model itself costs $2.26 of GPU time.

## Key findings

| Approach (scored on our held-out SN2 `val`, 1,037 tiles, unless noted) | SpaceNet training data | Overall F1 |
|---|---|---|
| **ConvNeXt-B U-Net, RGB, 1 model, 53 min on 1×A100 (~$2.20)** | SN2 train | **0.755** |
| … with per-city post-processing (2-fold cross-validated on val) | SN2 train | 0.761 |
| … with all 8 multispectral bands | SN2 train | 0.752 |
| … EfficientNetV2-M encoder / ensemble of both | SN2 train | 0.731 / 0.752 |
| … trained on SN2 + SN4 + SN6 + SN8 ("generalist") | SN2, SN4, SN6, SN8 | 0.738 |
| … DINOv3 ViT-L satellite-pretrained encoder | SN2 train | 0.716 |
| *XD_XD, 2017 winner (official test set; [leaderboard](#target-to-beat-2017-final-leaderboard))* | *SN2 train* | *0.693* |
| XD_XD's code retrained on our split: Paris / Khartoum only | SN2 train | 0.697 / 0.512 |
| **SAM 3, text prompt "building", zero-shot** | **none** | **0.642** |
| Gemma 4 31B boxes → SAM 2.1, zero-shot (100-tile `eval_small`) | none | 0.462 |
| ConvNeXt-B U-Net trained on SN4 + SN6 + SN8 only (SN2 cities unseen) | SN4, SN6, SN8 | 0.325 |
| Gemma 4 31B boxes alone, zero-shot (`eval_small`) | none | 0.279 |

1. **We win now.** A single RGB ConvNeXt-B U-Net trained for under an hour scores 0.755 against XD_XD's 0.693, and leads
   in all four cities. On identical tiles (XD_XD's own code retrained on our split) the lead is about 0.10 in both
   cities we could finish.
2. **The 2017 winner's extra inputs no longer matter.** 8-band multispectral input gives no gain over RGB (every
   difference within ±0.004 and inside its 95 % CI). XD_XD's OpenStreetMap layers are not needed either: our models use
   none.
3. **Zero-shot is now competitive with 2017.** SAM 3 with one English word scores 0.642, level with 2017's 2nd place and
   0.05 behind the winner, with no SpaceNet labels. It is within 0.02 of the retrained XD_XD in Paris and Khartoum.
   VLMs alone cannot draw footprints, but their boxes are usable SAM prompts.
4. **The gains come from the model, not post-processing.** A cross-validated post-processing search adds only +0.005.
   The satellite-pretrained DINOv3 ViT is worse than an ImageNet ConvNeXt (−0.04).
5. **Supervised models need data from the target region.** Pooling SN4/SN6/SN8 into training costs 0.017 on SN2 and
   roughly doubles F1 on those datasets. But a model trained without SN2 scores only 0.325 on the SN2 cities, half of
   zero-shot SAM 3. Planet 4 m imagery (SN7) is out of reach for any 0.3 m model.

![qualitative](results/qualitative.png)

*Left: ground truth. Middle: fully zero-shot Gemma 4 → SAM 2.1. Right: supervised ConvNeXt-B U-Net. One val tile per city.*

## Target to beat (2017 final leaderboard)

F1 at IoU ≥ 0.5:

| Entry | Vegas | Paris | Shanghai | Khartoum | **Overall** |
|---|---|---|---|---|---|
| XD_XD (1st: per-city U-Net ensemble, 8-band multispectral + OpenStreetMap) | 0.885 | 0.745 | 0.597 | 0.544 | **0.693** |
| wleite (2nd) | | | | | 0.643 |
| nofto (3rd) | | | | | 0.579 |

Sources: DeepGlobe 2018 paper, Table 2 ([arXiv:1805.06561](https://arxiv.org/abs/1805.06561)); SpaceNet paper
([arXiv:1807.01232](https://arxiv.org/abs/1807.01232)).

## Evaluation protocol

* **Splits.** The official test labels were never released, so everything is scored on a held-out 10 % of the public
  training tiles. The split is a deterministic MD5 hash: `val` = 1,037 tiles, 20,859 buildings. Supervised
  post-processing thresholds are tuned on a separate 5 % `tune` split carved out of train. No model ever trains on `val`.
  * Expensive zero-shot runs use `eval_small`: a fixed 25 val tiles per city (100 tiles, 1,649 buildings).
  * Our `val` comes from the same AOIs as the 2017 test set but is not the same tiles. Comparisons with the leaderboard
    are therefore close but not exact. The XD_XD rerun gives an exact comparison.
* **Metric** (`sn2/metric.py`): one-to-one greedy matching at IoU ≥ 0.5, F1 accumulated per city. Overall = mean of the
  four city F1s, as on the 2017 leaderboard. Polygons under 20 px² are ignored on both sides.
  * Sanity check: ground truth → rasterised masks → our watershed → polygons scores 0.998.
* **Input.** Pansharpened RGB (PS-RGB) at 0.3 m, stretched to 8-bit with a fixed per-city 1–99 % percentile stretch
  (`scripts/prepare_dataset.py`). The multispectral ablation uses the 8-band PS-MS product with the same stretch.
* **Noise level.** Most supervised numbers are single runs. The same recipe on the RGB product and on the RGB bands of
  the 8-band product differs by 0.003, so differences under about 0.005 are within noise. For the band ablation this is
  measured with a paired bootstrap over val tiles.

## Results

### 1. Zero-shot vision-language models

`eval_small`: 100 val tiles, 1,649 buildings.

Prompt: "Detect EVERY building footprint … return JSON `box_2d` normalized 0–1000". Each model is scored with whichever box
axis order it actually emits (Gemma: `[y0,x0,y1,x1]`; Qwen3-VL/Nemotron: `[x0,y0,x1,y1]`). "2×2" means the tile is split into
4 crops, each upsampled to 650 px and queried separately (`scripts/eval_vlm.py`).

| Model (HF Inference Providers) | Output | Vegas | Paris | Shanghai | Khartoum | **Overall** | API cost |
|---|---|---|---|---|---|---|---|
| google/gemma-4-31B-it | boxes, 2×2 crops | 0.487 | 0.138 | 0.240 | 0.252 | **0.279** | $0.04 |
| google/gemma-4-26B-A4B-it | boxes, 2×2 crops | 0.424 | 0.080 | 0.200 | 0.213 | **0.229** | $0.03 |
| google/gemma-4-26B-A4B-it | boxes | 0.271 | 0.033 | 0.160 | 0.154 | 0.155 | $0.01 |
| google/gemma-4-31B-it | boxes | 0.247 | 0.045 | 0.124 | 0.158 | 0.143 | $0.01 |
| Qwen/Qwen3-VL-235B-A22B-Instruct | boxes | 0.152 | 0.018 | 0.075 | 0.119 | 0.091 | $0.27 |
| nvidia/Nemotron-3.5-Super-VL-120B-A12B | boxes | 0.093 | 0.065 | 0.086 | 0.109 | 0.088 | $0.16 |
| google/gemma-4-31B-it | polygons | 0.000 | 0.008 | 0.008 | 0.000 | 0.004 | $0.03 |

### 2. Zero-shot SAM 2.1

hiera-large, tile upsampled 2×, same 100 tiles (`jobs/sam_eval.py`):

| Pipeline | Vegas | Paris | Shanghai | Khartoum | **Overall** |
|---|---|---|---|---|---|
| *Oracle: ground-truth boxes → SAM 2.1* | *0.982* | *0.956* | *0.973* | *0.967* | ***0.970*** |
| **Gemma 4 31B boxes (2×2 crops) → SAM 2.1** | 0.639 | 0.425 | 0.381 | 0.403 | **0.462** |
| Gemma 4 26B boxes (2×2 crops) → SAM 2.1 | 0.600 | 0.433 | 0.395 | 0.364 | 0.448 |
| Gemma 4 26B boxes → SAM 2.1 | 0.576 | 0.355 | 0.377 | 0.302 | 0.403 |
| Gemma 4 31B boxes → SAM 2.1 | 0.554 | 0.260 | 0.337 | 0.339 | 0.373 |
| SAM 2.1 automatic masks + SigLIP2 zero-shot "building roof" filter | 0.438 | 0.287 | 0.275 | 0.297 | 0.324 |
| SAM 2.1 automatic masks (everything) | 0.196 | 0.135 | 0.149 | 0.161 | 0.160 |

* **SAM's masks are essentially solved.** Given perfect boxes it scores 0.97, so the bottleneck is *recognition*: finding
  every building.
* **VLMs can't produce footprints on their own.** Their boxes are too loose for IoU ≥ 0.5 and their polygons are nonsense.
* **VLM boxes make good SAM prompts.** VLM → SAM is a 0.46 fully zero-shot pipeline for a few cents of API calls.
  Cropping (more pixels per building) is the biggest single lever.
* **Recall is the limiter.** Gemma finds about 50–60 % of buildings and under-counts dense blocks.

### 3. Zero-shot SAM 3

`jobs/sam3_eval.py`: SAM 3 (`facebook/sam3`, transformers `Sam3Model`) is prompted with a single word on the whole RGB
tile, and instances above a score threshold become polygons. Prompt, crop grid and threshold were chosen on `eval_small`
("building" beat "house" and "roof"; 2×2 crops hurt). The full `val` split was then scored once.

| Split / threshold | Vegas | Paris | Shanghai | Khartoum | **Overall** |
|---|---|---|---|---|---|
| eval_small (100 tiles), 0.4 | 0.855 | 0.672 | 0.589 | 0.567 | 0.671 |
| **val (1,037 tiles), 0.4** | 0.810 | 0.678 | 0.584 | 0.497 | **0.642** |
| val, best threshold (0.35) | | | | | 0.644 |

* One English word beats the best SAM 2.1 / VLM pipeline by 0.18. It is level with 2017's 2nd place (0.643) and 0.05
  behind the winner.
* Precision is decent (0.67–0.88). **Recall is the limit in the dense cities** (Khartoum 0.40, Shanghai 0.51): SAM 3
  merges or skips small touching buildings.
* The `eval_small` estimate (0.671) was optimistic because it over-weights easier tiles.

### 4. Supervised U-Net

Full `val`: 1,037 tiles, 20,859 buildings.

**Recipe** (`jobs/train_seg.py`):
* **Model:** a U-Net (segmentation_models_pytorch) with a timm ImageNet-21k/22k encoder. It has 3 sigmoid outputs:
  building body, a 2-px border, and building-building contact.
* **Training:** weighted BCE + Dice loss on images upsampled 1.5× (0.2 m/px), using random 512 px crops with scale
  jitter, flips/rot90 and photometric jitter. AdamW with cosine LR in bf16, 20k steps × batch 12 (about 25 epochs).
* **Inference:**
  * 4-way flip/transpose test-time augmentation.
  * Marker-controlled watershed, with seeds = body·(1−border)·(1−contact).
  * `approxPolyDP` turns the instances into polygons.
  * Thresholds are tuned on the `tune` split.

| Model | Vegas | Paris | Shanghai | Khartoum | **Overall** |
|---|---|---|---|---|---|
| XD_XD, 2017 winner (official test set) | 0.885 | 0.745 | 0.597 | 0.544 | 0.693 |
| **ConvNeXt-B U-Net (RGB)** | **0.892** | **0.807** | **0.709** | 0.611 | **0.755** |
| EfficientNetV2-M U-Net (RGB) | 0.878 | 0.768 | 0.691 | 0.586 | 0.731 |
| Ensemble (mean of both; wider post-processing grid) | 0.890 | 0.800 | 0.706 | **0.612** | 0.752 |
| DINOv3 ViT-L/16 SAT-493M U-Net (blocks 6/12/18/24 + conv stem; backbone LR 0.1×) | 0.867 | 0.746 | 0.690 | 0.559 | 0.716 |

* **The biggest gains over 2017 are in the hard cities:** Shanghai +0.11, Khartoum +0.07, Paris +0.06. Vegas was already
  near saturation in 2017.
* **The satellite-pretrained DINOv3 backbone is worse than ImageNet ConvNeXt-B (−0.04)**, despite 3× the parameters and
  2.4× the training time.
  * Its 16 px patches give features too coarse for 3–15 m buildings, and the conv stem and decoder must recover the
    boundary detail.
  * Satellite pretraining does not replace a convolutional, high-resolution encoder for footprints.
* Precision and recall per city are in `results/supervised/*.json`.

### 5. Head to head: XD_XD's 2017 code retrained on our split

For a strict 1:1 comparison, `jobs/xdxd/` runs XD_XD's original code
([BuildingDetectors_Round2/1-XD_XD](https://github.com/SpaceNetChallenge/BuildingDetectors_Round2)) **unmodified** on our
train/val split. That means:
* his per-city v9s, v13 and v16 (OpenStreetMap) U-Nets;
* his v17 ensemble;
* his own thresholds and min-area tuning.

His SN2 weights were never released, so the models are retrained. Only the 2017 environment is replaced:
* **Framework.** Keras 1.1 / Theano 0.9 / CUDA 8 cannot run on current GPUs. `jobs/xdxd/shim/keras` re-implements the
  Keras-1 API his code uses on PyTorch: layers, `fit`/`fit_generator`, checkpoints and early stopping.
* **Libraries.** 2017 library behaviour is pinned:
  * numpy 1.23, pandas 1.5 and click 6.7;
  * shapely 1.8, patched so empty polygons behave like shapely 1.5;
  * `gdal`, replaced by a rasterio shim.

  CUDA must be initialised before shapely 1.8 is imported, or torch segfaults.
* **OpenStreetMap.** His OSM layers came from Mapzen Metro Extracts, which went offline in 2018. `jobs/xdxd/osm_2017.py`
  rebuilds them from the **2017-05-01 OSM planet** (61 GB) in the same imposm shapefile layout.
* **Data.** `jobs/xdxd/stage_data.py` lays out our split in the 2017 directory structure, with the original 16-bit
  RGB-PanSharpen, MUL-PanSharpen and PAN tiles.

Scored with our metric on our `val` tiles (`results/xdxd/`):

| City | XD_XD retrained, our val (P / R) | XD_XD 2017 leaderboard | **ConvNeXt-B U-Net** | SAM 3 zero-shot |
|---|---|---|---|---|
| Paris (105 tiles) | 0.697 (0.77 / 0.64) | 0.745 | **0.807** | 0.678 |
| Khartoum (87 tiles) | 0.512 (0.62 / 0.44) | 0.544 | **0.611** | 0.497 |

* **The reproduction looks faithful, not tuned.** It lands 0.03–0.05 below the leaderboard scores, which is plausible
  for a different test set and a rebuilt framework and OSM stack.
* **On identical tiles the 2026 model is about 0.10 ahead in both cities**, slightly more than against the leaderboard.
* **SAM 3 with no training is within 0.02** of the retrained 2017 winner in both cities.
* **Vegas and Shanghai were not completed.**
  * With batch size 2, his v13 and v16 models take about 2.5–3 h each per city, even on an A100.
  * Those runs were cancelled because they could not finish within the remaining credit, and models are only saved at
    the end.
  * They also need more than 46 GB of RAM for preprocessing.

### 6. Post-processing is saturated

`scripts/pp_search.py` re-runs the watershed on the ConvNeXt-B model's saved val probabilities over:
* 36 threshold settings;
* polygon dilation of 0–1.5 px;
* minimum polygon area of 20–100 px;
* one global setting, or a setting per city.

To keep it honest, settings are chosen on one half of `val` and scored on the other, then the halves are swapped
(2-fold cross-validation).

| Post-processing | Vegas | Paris | Shanghai | Khartoum | **Overall** |
|---|---|---|---|---|---|
| Thresholds tuned on `tune` (the 0.755 model) | 0.893 | 0.807 | 0.709 | 0.613 | 0.7555 |
| Best global thresholds | 0.891 | 0.812 | 0.713 | 0.614 | 0.7575 |
| + 0.5 px dilation and min-area filter | 0.892 | 0.816 | 0.713 | 0.615 | 0.7588 |
| Everything per city | 0.892 | 0.815 | 0.716 | 0.620 | **0.7607** |

* **The ceiling is close.** Even the in-sample optimum is only 0.761.
* **The chosen settings differ between the two halves**, so part of the +0.005 is noise.
* **The most consistent change is a stricter seed threshold**, 0.8 rather than 0.7, which merges fewer touching
  buildings. Going higher (0.85–0.9) is worse.
* **The remaining errors are in the probabilities, not the polygon extraction**
  (`results/supervised/pp_search_convnextb.json`).

### 7. Do the 8 multispectral bands matter? No.

XD_XD used all 8 WorldView-3 bands. To test whether they help a modern model:
* **Data.** The 8-band pansharpened product (PS-MS, 0.3 m, same pixel grid as RGB) was converted with the same per-city
  stretch as our RGB tiles (`jobs/prep_ms.py`). Its red, green and blue bands correlate 0.9985–0.9999 with the RGB
  tiles.
* **Runs.** The ConvNeXt-B recipe was retrained three times, changing only the input bands (`train_seg.py --bands`).
* **Initialisation.** Extra bands enter the first conv with **zero-initialised** filters. Each run therefore starts
  exactly as the RGB model (verified bit-identical) and only changes if the bands carry signal.

| Input (from PS-MS unless noted) | Vegas | Paris | Shanghai | Khartoum | **Overall** | Δ vs MS-RGB, 95 % CI |
|---|---|---|---|---|---|---|
| PS-RGB product (the 0.755 model) | 0.892 | 0.807 | 0.709 | 0.611 | 0.755 | −0.002 … +0.009 |
| MS red/green/blue (WV-3 bands 5,3,2) | 0.899 | 0.796 | 0.705 | 0.605 | 0.752 | – |
| MS RGB + NIR1 | 0.896 | 0.802 | 0.709 | 0.595 | 0.750 | −0.007 … +0.004 |
| **MS all 8 bands** (+ coastal, yellow, red-edge, NIR2) | 0.893 | 0.799 | 0.709 | 0.608 | 0.752 | −0.004 … +0.006 |

CI: paired bootstrap over val tiles, resampled within each city (`scripts/band_ablation.py`,
`results/supervised/band_ablation.json`).

* **No band combination moves the score beyond run-to-run noise.** All differences are ≤ 0.004 and every CI contains 0,
  overall and in every city.
* **NIR does not help even in Khartoum.** Telling bare soil, roof and vegetation apart was the expected win there.
  At 0.3 m, footprint extraction is a *spatial* problem (edges, shape, shadow), and RGB colour is enough.
* **Spectral indices (NDVI etc.) were not run separately.** They are fixed functions of red and NIR, and the network
  gained nothing with NIR available, so a precomputed index is very unlikely to help.
* **PAN is not a separate input.** Both pansharpened products are already built at PAN's 0.3 m resolution.
* **Why the bands may have helped in 2017 (not tested):** XD_XD's U-Nets were trained from scratch, while an
  ImageNet-pretrained encoder already extracts what it needs from RGB.
* **Data defect:** SpaceNet's PS-MS files are blank (all zeros) for 10 Vegas tiles whose RGB files are fine, 2 of them in
  val. Excluding those tiles changes no score by more than 0.0001.

### 8. How general is it? Other SpaceNet building datasets

**Data.** The other SpaceNet building datasets are resampled to SN2's 0.3 m GSD, converted to 8-bit RGB, and cut into
sub-tiles of at most 1,600 px (`scripts/prepare_xds.py`). They are scored with the same F1 @ IoU ≥ 0.5 scorer, with
TP/FP/FN pooled per group.

**Test splits are held out by location**, and `scripts/check_leakage.py` audits train/test overlap by geographic
footprint:
* **SN4 (Atlanta, 27 look angles).** A test location is test at every angle. None of the 40 test locations appears in
  the 300 training locations.
* **SN8 is clean.** 0 of 48 Germany and 3 of 142 Louisiana test tiles touch a training image, covering 1.4 % of test area
  on average.
* **SN6 (Rotterdam) is split geographically.** Its tiles come from overlapping imaging strips, so a split by tile number
  leaked: 99.8 % of test area was covered by training images.
  * 1 km grid cells are hashed to test, and no training tile may come within 50 m of a test tile.
  * That gives 300 test and 1,495 train tiles, with 0 test tiles overlapping training.
* **SN1 (Rio) is excluded** because of poor label quality.
* **SN7 (Planet 4 m) is never trained on.** It is a pure unseen-sensor test.

**Models.**
* **SN2-only:** the 0.755 ConvNeXt-B above, applied zero-shot.
* **Generalist:** the same recipe trained on SN2 plus the SN4/SN6/SN8 train splits, 20k steps, 81 min on 1×A100.
  * 40 % of crops come from SN2 and 20 % each from SN4, SN6 and SN8 (area-weighted within each).
  * SN4 has 3,000 tiles but only 300 distinct locations; SN6 has 1,495 tiles and SN8 1,217.

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

* **A model trained on one dataset is narrow.** Out of domain, the SN2-only model stays fairly precise but misses most
  buildings, and it collapses as look angle grows.
* **A single model trained on the pooled data is general.** It loses only 0.017 on SN2 and roughly doubles F1 on SN4 and
  SN8.
  * For reference, the SN4 paper's best baseline (TernausNet, trained on SN4) scored 0.62 / 0.43 / 0.22 on the three
    angle bands. Our generalist scores 0.74 / 0.50 / 0.27, but on our own location-held-out split, not the official test
    set.
* **SN6 stays hard (0.44).** Recall is only 0.31 on Rotterdam's dense, small, shadowed buildings. The SN6 challenge
  winner (~0.42) used SAR, so the scores are not comparable.
* **Three SN4 collects stay near zero even for the generalist:** 29°, 32° and 53° (0.21 / 0.04 / 0.06). Most collects at
  similar angles score 0.5–0.7, so this looks like image/label misregistration in those collects. This has not been
  verified.
* **SN7 is out of reach for any 0.3 m model.** At 4 m a house is about 2–3 px, and resampling cannot create that detail.
* **SN8 reference scores are not comparable**, because SN8 was scored with a different, flood-attribute metric.
* An earlier generalist on the leaky SN6 split scored 0.743 on SN2 and within 0.02 of these numbers elsewhere
  (`results/transfer/generalist_nosn1_convnextb.json`).

**Can it generalise to cities it has never seen?** Not well. The same recipe was trained only on SN4 + SN6 + SN8, with no
SN2 at all and fixed post-processing, so no SN2 labels were used anywhere.

| City | No-SN2 model F1 | Precision / recall | Trained on SN2 | SAM 3 zero-shot |
|---|---|---|---|---|
| Vegas | 0.497 | 0.69 / 0.39 | 0.892 | 0.810 |
| Paris | 0.569 | 0.71 / 0.47 | 0.807 | 0.678 |
| Shanghai | 0.184 | 0.58 / 0.11 | 0.709 | 0.584 |
| Khartoum | 0.051 | 0.51 / 0.03 | 0.611 | 0.497 |
| **Overall** | **0.325** | | **0.755** | **0.642** |

* **The US and European datasets don't cover these cities.** Three building datasets from the US and Europe contain
  nothing like dense Chinese blocks or Sudanese compounds.
* **Even Vegas loses most of its recall.** US suburbs should look familiar, so this is probably SN2's tighter labelling
  style.
* **Zero-shot SAM 3 is twice as good on unseen cities** as this supervised model.

Per-angle and per-region numbers are in `results/transfer/{sn2only,generalist_geo}_convnextb.json`,
`results/transfer/sn2only_convnextb_sn6geo.json` and `results/supervised/nosn2_convnextb_20k.json`, produced by
`jobs/transfer_eval.py`.

## What it took to win in 2017 vs. now

XD_XD's 2017 winning recipe had four parts:
* a separate U-Net set per city, trained from scratch;
* 8-band multispectral input;
* OpenStreetMap layers (buildings, roads, land use, water) rasterised as extra input channels;
* a three-model ensemble with per-city minimum-area tuning.

In 2026 none of those extras is needed:
* **One model handles all four cities**, with RGB input, no OSM, no ensemble and about an hour of training.
* **The gain comes from two things:**
  * an ImageNet-21k/22k-pretrained encoder;
  * a body/border/contact output with watershed instance separation, which splits touching buildings.
* **The ablations rule out the other explanations.** Multispectral bands add nothing, post-processing adds +0.005, and a
  bigger satellite-pretrained ViT is worse.
* **Zero-shot now reaches 2017's 2nd place.** SAM 3 needs only a text prompt.

## Repository layout

```
sn2/metric.py              SpaceNet F1 scorer (+ CSV IO)
sn2/vlm.py                 HF router client, prompts, box/polygon parsing
scripts/prepare_dataset.py S3 → 8-bit PS-RGB PNG tiles, labels, splits (train/val/eval_small), per-city stretch
scripts/make_shards.py     per-city uint8 .npy shards for fast loading in Jobs
scripts/eval_vlm.py        zero-shot VLM benchmark (cached raw responses in results/raw/)
scripts/export_prompts.py  VLM boxes → SAM box prompts
scripts/make_figure.py     qualitative figure; scripts/overlay.py: prediction overlays
scripts/prepare_xds.py     other SpaceNet building datasets → 0.3 m RGB sub-tiles, labels, location-held-out splits
scripts/check_leakage.py   geographic train/test overlap audit (SN6, SN8)
scripts/pp_search.py       post-processing search on saved val probabilities (2-fold CV)
scripts/band_ablation.py   scores the multispectral runs, paired bootstrap CIs
scripts/session_costs.py   Claude + Hugging Face cost from the transcript and job records → results/costs.json
scripts/export_trace.py    scrubbed session transcript → trace/
jobs/sam_eval.py           HF Jobs (GPU): SAM 2.1 automatic / SigLIP2-filtered / box-prompted
jobs/sam3_eval.py          HF Jobs (GPU): SAM 3 text-prompted segmentation
jobs/train_seg.py          HF Jobs (GPU): supervised U-Net training, tuned watershed, val scoring
                             --extra sn4,sn6,sn8 (generalist), --bands 4,2,1,6 (multispectral), --pp (fixed thresholds)
jobs/transfer_eval.py      HF Jobs (GPU): cross-dataset scoring per angle band / region
jobs/prep_ms.py            HF Jobs (CPU): 8-band PS-MS → uint8 shards aligned with the RGB shards
jobs/xdxd/                 XD_XD 2017 reproduction: Keras-1 → PyTorch shim, data staging, 2017 OSM rebuild, run script
trace/                     the Claude Code session that built this repo (readable TRACE.md + full transcript)
results/                   JSON results per section (vlm_*, sam/, sam3/, supervised/, transfer/, xdxd/), raw VLM outputs
```

Data and job outputs live on the Hub (private):
* **Dataset** `Rootkit29/spacenet2-speedrun`: tiles, labels, splits, RGB shards, other-dataset tiles and code.
* **Bucket** `Rootkit29/sn2-runs`: checkpoints, val probabilities and predictions, multispectral shards, XD_XD outputs
  and the rebuilt 2017 OSM layers.

Jobs mount both as volumes, so no token is needed inside a job. Typical commands:

```bash
M="-v hf://datasets/Rootkit29/spacenet2-speedrun:/sn2 -v hf://buckets/Rootkit29/sn2-runs:/out"
ENC=tu-convnext_base.fb_in22k_ft_in1k
# supervised RGB model (0.755)
hf jobs uv run --flavor a100-large --timeout 3h -d $M jobs/train_seg.py --run convnextb_unet_20k \
  --encoder $ENC --steps 20000 --bs 12 --crop 512 --lr 2e-4
# generalist (SN2 + SN4/SN6/SN8); no-SN2 model: --extra_frac 1.0 --pp '{"t_mask":0.3,"t_seed":0.8,"min_seed":30}'
hf jobs uv run --flavor a100-large --timeout 3h -d $M jobs/train_seg.py --run generalist_geo_convnextb_20k \
  --encoder $ENC --steps 20000 --bs 12 --crop 512 --lr 2e-4 --extra sn4,sn6,sn8 --extra_frac 0.6
# multispectral: build shards once, then pick bands (RGB first)
hf jobs uv run --flavor cpu-performance --timeout 90m -d $M jobs/prep_ms.py
hf jobs uv run --flavor a100-large --timeout 2h -d $M jobs/train_seg.py --run ms8_convnextb_20k \
  --encoder $ENC --steps 20000 --bs 12 --crop 512 --lr 2e-4 --bands 4,2,1,0,3,5,6,7
# zero-shot SAM 3 (weights copied to the bucket; gated on the Hub)
hf jobs uv run --flavor a100-large --timeout 2h -d $M jobs/sam3_eval.py --split val --prompts building --grids 1
# cross-dataset transfer
hf jobs uv run --flavor a100-large --timeout 2h -d $M jobs/transfer_eval.py --tag generalist_geo_convnextb \
  --models /out/generalist_geo_convnextb_20k/model.pt=$ENC --datasets sn4,sn6,sn7,sn8
# XD_XD 2017 reproduction, one city per job (Vegas/Shanghai need > 46 GB RAM)
hf jobs run --flavor a10g-large --timeout 6h -d $M python:3.11-bookworm bash /sn2/xdxd/run_xdxd.sh AOI_3_Paris
# local, no GPU
python scripts/pp_search.py --probs val_probs_u8.npz --out results/supervised/pp_search_convnextb.json
python scripts/band_ablation.py <dir with val_predictions csvs> results/supervised/band_ablation.json
```

## Cost: Claude and Hugging Face

Computed by `scripts/session_costs.py` → `results/costs.json`. Inputs are the session transcript (token usage of every
Claude API request) and the Hugging Face job records (`list_jobs` × the per-minute price in `list_jobs_hardware`). The
session ran from 2026-11-26 00:47 to 2026-11-27 14:40 UTC.

| | Cost |
|---|---|
| Claude (agent doing all the work), API list-price equivalent | **$41.68** |
| Hugging Face Jobs (56 jobs: 20.2 A100-hours, 10.6 A10G-hours, CPU) | **$72.14** |
| Hugging Face Inference Providers (VLM benchmark) | $0.70 |
| **Total** | **≈ $114.5** |

**Claude.**

| Model | Requests | Input tokens | Output tokens | Cache reads | Cache writes (1 h) | Cost |
|---|---|---|---|---|---|---|
| Claude Opus 5.5 (main agent) | 489 | 1.1 k | 387 k | 99.6 M | 1.75 M | $41.67 |
| Claude Haiku 5.5 (two small helper agents) | 5 | – | 4 k | 36 k | 45 k (5 min) | $0.01 |

* **Prices are API list prices** (Opus 5.5: $4 in / $20 out per MTok, $0.20 cache read, $8 for 1-hour cache writes).
  The work ran through Claude Code, so actual billing depends on the plan; these numbers are what the same tokens cost
  on the API.
* **Most of the cost is re-reading context, not writing.** The output (code, analysis, this README) is $7.75. Cache
  reads of the growing conversation are $19.92 and cache writes $14.00: every step of a long agent session re-reads the
  history from cache.
* **The harness agrees.** Claude Code's own running total was $35.87 at its last checkpoint, which came before the final
  write-up turns.
* **About 1 hour of model time against 3 hours of tool time.** Most of the ~38 h wall clock was waiting on GPU jobs.

**Hugging Face, by experiment.**

| Experiment | Jobs | Running time | Cost |
|---|---|---|---|
| XD_XD 2017 reproduction (incl. OSM planet rebuild, failed environment attempts, cancelled Vegas/Shanghai) | 23 | 19.2 h | $34.78 |
| Generalist training (incl. a cancelled run with SN1) | 3 | 3.4 h | $8.57 |
| Multispectral ablation (shard prep, 3 runs, data checks) | 7 | 3.1 h | $7.60 |
| Supervised SN2 models (EffNetV2-M, ConvNeXt-B, ensemble) | 4 | 2.5 h | $6.26 |
| DINOv3 | 1 | 2.2 h | $5.40 |
| Transfer / generalisation evals | 5 | 1.6 h | $3.78 |
| No-SN2 training | 1 | 1.3 h | $3.26 |
| SAM 2.1 | 4 | 0.6 h | $1.38 |
| SAM 3 | 2 | 0.4 h | $1.11 |
| Smoke tests and checks | 6 | – | $0.00 |

**Hugging Face, by hardware.**

| Flavor | Price | Jobs | Running time | Cost |
|---|---|---|---|---|
| a100-large (A100 80 GB) | $2.50/h | 25 | 20.2 h | $50.50 |
| a10g-large | $1.50/h | 13 | 10.4 h | $15.62 |
| cpu-performance | $1.90/h | 2 | 3.1 h | $5.82 |
| a10g-small, cpu-basic, cpu-upgrade, l40sx1 | | 16 | 0.6 h | $0.20 |

Notes on the Hugging Face figures:
* **Cancelled jobs.** These report no running time. Their end time is taken from the session record: the cancel command,
  or the last log write for the two jobs the platform cancelled when prepaid credit ran out. The two earliest are upper
  bounds.
* **Not included:** storage of the private dataset and bucket.
* **Where the money went:** almost half the compute was the XD_XD reproduction, because his 2017 schedule trains with
  batch size 2. The 2026 model that beats it costs $2.26.

## Session trace

[`trace/`](trace/) holds the complete record of the Claude Code session that produced this repo:

* `trace/TRACE.md`: a readable rendering of every user prompt, every Claude reply, and every tool call with its command.
  Tool results are truncated to 1,500 characters. The context-compaction summary is included where the conversation was
  compacted.
* `trace/session.jsonl.gz`: the full transcript (about 2,900 entries), with every tool call and untruncated result.
* `trace/subagents/*.jsonl.gz`: the two helper-agent transcripts.

Personal details are scrubbed by `scripts/export_trace.py`: e-mail addresses, the Hugging Face account response (name,
e-mail, orgs, billing) and the claude.ai organisation id. No credentials were ever in the session, because all access
was proxy-injected.

Some mistakes are visible in the trace and were fixed:
* **SAM 3.** A running score was mis-reported as the final one.
* **SN6.** The first split leaked test ground into training, and was redone geographically.
* **XD_XD.** Several environment failures came before the reproduction ran.
* **Multispectral check.** Five blank-tile IDs were briefly invented in a script before being replaced with the
  real list.

## Limitations and open questions

* **`val` is not the official test set.** Use the XD_XD rerun rows for exact like-for-like comparison.
* **The XD_XD rerun covers only Paris and Khartoum.** Finishing Vegas and Shanghai needs a high-RAM GPU for about 6 h per
  city, and checkpoints saved per stage first.
* **Most supervised numbers are single training runs.** Differences under about 0.005 are within noise.
* **Ideas not tried:**
  * Multi-scale test-time augmentation.
  * Seed ensembles.
  * Longer schedules on larger crops.
  * Fine-tuning SAM 3 on pooled SpaceNet data, to combine its open-world recognition with SpaceNet's labelling style.
  * Using the supervised model's boxes as SAM prompts.
