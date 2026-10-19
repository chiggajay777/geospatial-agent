# STATUS — geo-flood-agent v0.3.0

**Date:** 2026-10-18 (UTC)

## What this version does

AOI in → bilingual screening memo. Live mode reads Copernicus DEM GLO-30 (HAND + slope) and JRC Global Surface Water occurrence, and it **masks permanent water** before scoring. `--demo` stays offline and MOCK. The default planner is a logged tool sequence with no API key. `--llm` can call the same tools through any OpenAI-compatible endpoint (OpenAI or local Ollama); drafts that invent numbers are rejected.

## Verified on this machine (Python 3.12, CPU)

```bash
python -m geo_flood_agent --demo --out examples/sample_output
python -m geo_flood_agent --aoi examples/sample_aoi.geojson --mode live --out output/live_demo
python -m geo_flood_agent --validate --out output/validation
python -m unittest tests.test_core
```

| Run | Result |
|-----|--------|
| `--demo` sample AOI | Composite **45.1**, all MOCK (unchanged geometry heuristic) |
| `--mode live` sample AOI | **8.4 s**. Composite **25.8** (moderate). Terrain HAND **36.3**, surface water **1.4**. 3 LIVE / 0 MOCK. GET bodies **3,415,324 bytes**. |
| West Lake (AOI centroid 120.14°E, 30.25°N) | Occurrence **97%**, elevation 7.5 m, HAND 0 m, permanent, **excluded from the score**. Permanent water in the AOI: **4.99 km² (9.74%)**. Of HAND < 2 m pixels, **32.31%** are permanent water and are excluded. |
| Sen1Floods11 test split | **90/90 chips attempted, 82 scored, 8 skipped** (a class missing after the permanent-water mask). Pooled AUROC **0.823**, AP **0.340** (prevalence 0.085), macro AUROC **0.811**. Precision/recall at the pre-specified score ≥ 50: **0.138 / 0.955**. Copernicus GET bodies **395,924,324 bytes**. Labels **601,535 bytes**. About **2 min 40 s**. |
| Unit tests | `tests/test_core.py` — synthetic valley HAND, slope plane, permanent-water exclusion, AUROC/AP, stable MOCK scores, number audit, local OpenAI-compatible tool loop |
| LLM on this run | Not called. `llm_enabled: false`. Deterministic trace written to `tool_trace.json`. |

The validation score is terrain concern only. JRC occurrence is not a predictor there, because the occurrence record can include past floods.

## Honesty

- Every layer is tagged **MOCK** or **LIVE**.
- A failed live source is labeled and is not blended into the composite.
- Copernicus GLO-30 is a DSM. HAND here is not bare-earth flood depth.
- The score ≥ 50 cut recalls most labeled flood water and also flags a lot of land that the single Sen1Floods11 scene did not inundate. That is reported, not tuned away.
- Somalia is the weakest event in this split (AUROC 0.665). Nigeria is the strongest (0.961).

## Not in this version

- Rainfall, hydrographs, or a forecast.
- Bare-earth DEM (FABDEM / a lidar DTM).
- Using JRC occurrence as a validated predictor (kept out on purpose).
