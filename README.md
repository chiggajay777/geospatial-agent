# geo-flood-agent

**AOI in → bilingual (EN/ZH) flood / inundation screening memo, GeoJSON, `summary.json`, and figures.**

Portfolio piece for a remote-sensing / hydrology PhD application. The core path uses free public data and runs on a CPU (tested here with Python 3.12). No paid API key is required.

> **What a live run is.** Copernicus DEM GLO-30 (a digital surface model) is turned into height above nearest drainage (HAND) and slope. JRC Global Surface Water occurrence supplies a permanent-water mask and a historical-wetness layer. The composite is the mean susceptibility on **land that is not permanent water**. It is a screening index. It is not a hydraulic flood depth, not a forecast, and not an official floodplain.
>
> **`--demo` never touches the network.** Every layer is then labeled **MOCK**.

---

## Problem

Site screening needs a fast, inspectable inundation signal before a hydraulic study: an AOI goes in, and a scored memo comes out, with each layer tagged MOCK or LIVE and with the numbers traceable to a tool output.

The previous version scored a MOCK hydrology heuristic from the AOI centroid, and it treated a 5×5 grid of elevations as terrain. On the Hangzhou sample that grid was coarse enough to call West Lake “low-lying flood-prone land.” This version replaces both of those with rasters, and it checks the terrain score against hand-labeled flood water.

## Data

| Layer | Source | What was actually transferred |
|-------|--------|-------------------------------|
| Terrain | [Copernicus DEM GLO-30](https://registry.opendata.aws/copernicus-dem/) public COGs, AWS bucket `copernicus-dem-30m` (eu-central-1), no credentials. 2021 release. DSM, ~30 m. | Sample AOI: **3,415,324 bytes** of HTTP GET bodies (range reads). The `N30E120` object is 47,859,448 bytes and was not downloaded whole. |
| Surface water | [JRC Global Surface Water](https://global-surface-water.appspot.com/) occurrence, Planetary Computer collection `jrc-gsw`, COG v1.3 (1984–2020). Public SAS signing, no account key. | Same live run. Largest HEAD Content-Length seen: 96,601,892 bytes (the 10° occurrence COG), windowed, not downloaded whole. |
| Observed floods | [Sen1Floods11](https://github.com/cloudtostreet/Sen1Floods11) v1.1 hand labels (`LabelHand`) and the dataset’s permanent-water chips (`JRCWaterHand`), public GCS. Official test split `flood_test_data.csv`. | **601,535 bytes** of GeoTIFFs for all 90 test chips. |
| Validation DEM | Same Copernicus GLO-30 windows, one buffered chip at a time. | **395,924,324 bytes** of HTTP GET bodies (~378 MiB) across 90 chips. Largest source object header: 48,614,529 bytes. |

Live sample + full test-split validation is about **381 MiB** of GET bodies. Cache lives in `.cache/` (gitignored).

Citations: Pekel et al. 2016, *Nature* (JRC GSW); Copernicus DEM licence; Bonafilia, Tellman, Anderson, Issenberg 2020, CVPR Workshops (Sen1Floods11).

## Method

Parameters below were fixed in `src/geo_flood_agent/constants.py` before the validation run. They were not tuned on the test split.

1. Validate the AOI with Shapely. Reject a box larger than 5° on a side.
2. Build a local UTM grid at 30 m covering the AOI plus a **3 km** buffer (resolution coarsens only if a side would exceed 1200 pixels). The buffer exists so D8 flow is not cut off on the AOI boundary.
3. Read Copernicus GLO-30 with HTTP range requests and bilinearly resample it onto that grid.
4. **HAND and slope.** Priority-flood with a tiny epsilon so flats drain (Barnes, Lehman, Mulla 2014), D8 steepest descent and accumulation (O’Callaghan and Mark 1984), HAND as the original elevation minus the downstream drainage elevation, clipped at 0 (Nobre et al. 2011). Drainage cells are those with upstream area ≥ **0.5 km²**. Slope is the gradient of the original DEM, in degrees. The implementation is NumPy plus rasterio (unit-tested on a synthetic valley). pysheds / Whitebox / richdem implement the same family of algorithms; they were not used, so the Mac install stays a pip wheel set (rasterio already carries GDAL).
5. **Permanent water.** JRC occurrence ≥ **80%** is masked. Those pixels are reported (area, fraction) and are excluded from every score. The sample AOI centroid falls on West Lake; see below.
6. **Terrain concern** on remaining land, 0–100: `80% × clip((10 − HAND) / 10, 0, 1) + 20% × clip((5 − slope) / 5, 0, 1)`.
7. **Water concern** on remaining land: occurrence / 80, scaled to 0–100. This is multi-year wetness, so the screening composite may use it, but the **validation score does not** (occurrence history can contain the floods being scored).
8. **Composite:** mean over non-permanent land of `0.70 × terrain + 0.30 × water`. If one live source fails, that layer is labeled MOCK and is left out of the composite. A MOCK number does not drive the score.
9. **Planner.** Default is a fixed tool sequence (`analyze_terrain` → `analyze_surface_water` → `score_susceptibility`), written to `tool_trace.json`. `--llm` sends those tools to any OpenAI-compatible `/chat/completions` endpoint (OpenAI or a local Ollama server). The model may draft the narrative. A draft that cites a number which is not a rounding of a tool output is rejected, and the deterministic narrative is kept. This verification run used the deterministic planner (`llm_enabled: false`).

## Sample AOI (this live run)

`examples/sample_aoi.geojson` — 120.10–120.18°E, 30.22–30.28°N, about 51.4 km², Hangzhou, including West Lake. Command: `--mode live`. Wall clock **8.4 s**. Mode `live`, 3 LIVE / 0 MOCK. Planner `deterministic`.

| Layer | Score | Kind |
|-------|------:|------|
| `surface_water` | 1.4 | LIVE |
| `terrain_hand` | 36.3 | LIVE |
| `inundation_susceptibility` (composite) | **25.8** (moderate) | LIVE |

Raster summaries on this run:

| Quantity | Value |
|----------|------:|
| Elevation mean / median inside the AOI | 34.07 m / 15.18 m |
| HAND on non-permanent land, median / mean | 6.62 m / 23.44 m |
| Slope mean on that land | 8.06° |
| Land scored | 46.28 km² (51,422 px at 30 m) |
| Permanent water (occurrence ≥ 80%) | **4.99 km² (9.74% of the AOI)** |
| Land with HAND < 2 m | 22.52% of scored land |
| Share of AOI pixels with HAND < 2 m that are permanent water | **32.31%** (excluded) |
| Unresolved HAND | 0% |
| Composite p90 on land | 60.8 |

**West Lake.** The AOI centroid (120.14°E, 30.25°N) samples occurrence **97%**, elevation **7.5 m**, HAND **0 m**, `permanent_water=true`, `included_in_score=false`. The lake is drawn and masked. It is not a flood-prone land pixel. The low water-layer score (1.4) is the historical wetness of the *remaining* land: once the lake is removed, most of the AOI was rarely mapped as water. Terrain still carries the low HAND ground around it (terrain score 36.3; composite 25.8).

![Layer scores](figures/sample_aoi_layer_scores.png)

![Elevation, permanent water in blue](figures/sample_aoi_elevation.png)

![HAND](figures/sample_aoi_hand.png)

![JRC occurrence](figures/sample_aoi_occurrence.png)

![Susceptibility on land](figures/sample_aoi_susceptibility.png)

`--demo` on the same AOI is unchanged as a geometry heuristic: hydrology proxy 39.1, terrain proxy 54.6, composite 45.1, all MOCK. See `examples/sample_output/`.

## Validation

**Question.** Does the terrain concern rank hand-labeled flood water above dry land, on pixels that are not permanent water?

**Setup.** All 90 chips in the Sen1Floods11 **hand-labeled test split** (10 events, file order, not chosen by score). Score = the terrain concern above, resampled bilinearly onto the 10 m label grid. Permanent water = `JRCWaterHand > 0`. Label `-1` is nodata. JRC occurrence is **not** a predictor. Operating point score ≥ 50 was pre-specified.

**Coverage.** 82 chips entered the metrics. 8 were skipped because a chip must contain both flooded and non-flooded land after the mask: 6 had dry land and no flood pixels, 1 (Ghana) had no valid land pixels, and 1 (Paraguay) was entirely flooded land. Skipped chips do not contribute pixels. Pooled N = 18,275,983 land pixels (1,560,373 flooded, prevalence 0.085). Wall clock about **2 min 40 s** from the first label file to `validation_summary.json`.

### Pooled

| Metric | Value |
|--------|------:|
| AUROC | **0.823** |
| Average precision | **0.340** |
| Prevalence (AP of an uninformative score) | 0.085 |
| Macro AUROC (unweighted mean of 10 events) | 0.811 |
| Precision at score ≥ 50 | 0.138 |
| Recall at score ≥ 50 | 0.955 |

### By event

| Event | Chips scored | Flood pixels | Dry land pixels | AUROC | AP |
|-------|-------------:|-------------:|----------------:|------:|---:|
| Ghana | 7 | 104,377 | 1,400,435 | 0.891 | 0.561 |
| India | 13 | 428,080 | 2,248,686 | 0.812 | 0.476 |
| Mekong | 6 | 188,244 | 1,242,872 | 0.797 | 0.332 |
| Nigeria | 4 | 106,891 | 636,842 | 0.961 | 0.760 |
| Pakistan | 5 | 195,399 | 824,366 | 0.857 | 0.501 |
| Paraguay | 12 | 103,364 | 2,844,162 | 0.723 | 0.096 |
| Somalia | 6 | 86,664 | 1,240,014 | 0.665 | 0.107 |
| Spain | 6 | 246,072 | 1,124,201 | 0.824 | 0.481 |
| Sri-Lanka | 9 | 39,030 | 1,627,131 | 0.741 | 0.068 |
| USA | 14 | 62,252 | 3,526,901 | 0.838 | 0.147 |

![ROC and precision-recall](figures/validation_roc_pr.png)

![AUROC by event](figures/validation_by_event.png)

**How to read this.** AUROC 0.823 means a flooded land pixel outranks a dry land pixel about 82% of the time. Average precision 0.340 is above the 0.085 prevalence baseline and is pulled down by rare-flood scenes (Sri-Lanka, Paraguay, Somalia, USA). The score ≥ 50 threshold recalls 95.5% of flooded land and is precise on only 13.8% of its alarms: HAND lights up susceptible ground that this particular Sentinel-1 scene did not inundate. That is what a susceptibility index does. It is not a detector of one flood date. Somalia is the weakest event (AUROC 0.665). Nigeria is the strongest (0.961). No threshold or HAND cap was adjusted after seeing these numbers.

## LLM setup

Default: no model is called.

```bash
# Ollama on the Mac mini (OpenAI-compatible)
export GEO_FLOOD_LLM=1
export OPENAI_BASE_URL=http://127.0.0.1:11434/v1
export OPENAI_API_KEY=ollama
export OPENAI_MODEL=llama3.2

python -m geo_flood_agent --aoi examples/sample_aoi.geojson --mode live --llm --out output/live_llm
```

OpenAI uses the same variables with `OPENAI_BASE_URL=https://api.openai.com/v1` and a real key. If the endpoint errors, or the draft contains a number that is not in the tool JSON, `tool_trace.json` records `fallback_reason` and the memo keeps the deterministic narrative. Tables are always filled from tools.

## Limitations

- Copernicus GLO-30 is a **surface** model. Canopy and buildings are in the elevation, so urban HAND is not bare-earth HAND.
- D8 on a 30 m UTM grid with a 0.5 km² channel threshold and a 3 km buffer is a local approximation. The sample AOI had no unresolved HAND cells; a basin larger than the buffer can still truncate flow paths.
- The 10 m HAND cap and the 80% permanent-water threshold are screening choices, not calibrated flood frequencies.
- JRC occurrence in the **screening** composite is historical wetness. It is weak on the sample once the lake is masked (score 1.4), which is expected, and it is deliberately absent from the validation score.
- Validation covers the Sen1Floods11 test split only (11 countries’ events in that file, 82 usable chips). It does not measure performance outside those scenes, and it does not test the water-history term.
- There is no rainfall, no hydrograph, and no forecast.
- Do not use the memo for engineering, insurance, or regulatory decisions.

## Reproduce (macOS, Python 3.12)

From the repository root. Rasterio’s wheel supplies GDAL; a separate GDAL install is unnecessary. No API key.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -e .

# Offline MOCK demo (no network)
python -m geo_flood_agent --demo --out examples/sample_output

# Live screening: Copernicus GLO-30 HAND + JRC occurrence, sample AOI
python -m geo_flood_agent --aoi examples/sample_aoi.geojson --mode live --out output/live_demo

# Validation: full Sen1Floods11 hand-labeled test split (about 380 MiB of COG windows + labels)
python -m geo_flood_agent --validate --out output/validation

# Optional: first 2 test chips per event, CSV order, still not chosen by score
# python -m geo_flood_agent --validate --validate-max-per-event 2 --out output/validation_small

python -m geo_flood_agent --list-tools
```

## Layout

```
src/geo_flood_agent/    CLI, deterministic + LLM planners, HAND, JRC mask, validation
examples/               sample AOI and the offline MOCK memo
figures/                live sample maps and validation curves from the run recorded above
tests/test_core.py      synthetic valley, metrics, mock scores, local tool-calling stub
```

**Tools:** `validate_aoi_geojson` · `validate_aoi_bbox` · `analyze_terrain` · `analyze_surface_water` · `score_susceptibility` · `run_flood_screening` · `write_report` · `fetch_open_elevation`

## License

MIT for this code. Copernicus DEM, JRC Global Surface Water, and Sen1Floods11 keep their own terms. Do not commit `.env`, `.venv`, `.cache/`, or `output/`.
