# Reviewer walkthrough

How to see what Geoapify adds on geo-flood-agent 0.4.0. The flood screener is the same HAND / JRC path as before. Geoapify is an optional place query on top of it.

No API key belongs in this file, in git, or in a screenshot. The variable is `GEOAPIFY_API_KEY`. The value lives in `.env.example` (copy it to `.env`).

## What this demo shows

- **Flood screening.** An area of interest goes in. A bilingual (EN/ZH) memo, GeoJSON, `summary.json`, and a score chart come out. Scores are a screening index. They are not flood depth, not a forecast, and not an official floodplain.
- **Geoapify place context (optional).** `--place` geocodes a name, lists nearby places, and requests a travel-time isoline. Those records are location context. **Geoapify is not flood depth** and is not an input to the composite score.
- **Offline path.** `--demo` uses the hand-drawn Hangzhou sample AOI. It does not call Geoapify and does not use the network.

## Setup

From the repository root (Python 3.10+; this tree was tested on 3.12):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

The key is optional until step 2.

```bash
cp .env.example .env
```

`.env` is gitignored. `python-dotenv` loads it. You can instead export `GEOAPIFY_API_KEY` in the shell. Do not commit `.env`, and do not paste the key into docs or into `docs/geoapify-mcp.json.example`.

`--demo` does not read the key. One `--place` run is a geocode, a places search, and one isoline, plus a single follow-up if the isoline returns HTTP 202.

## Step 1: offline `--demo`

No key and no network. Write to `output/` (gitignored) so the checked-in sample memo stays as it is.

```bash
python -m geo_flood_agent --demo --out output/demo
```

Expect:

- `composite: 45.1 (moderate)`
- `layers: 0 LIVE / 3 MOCK`
- `note: MOCK layers present — see report disclaimer`
- No `geocode`, `nearby places`, or `isoline` lines

The three MOCK layers are `hydrology_proxy` 39.1, `terrain_flatness_proxy` 54.6, and `inundation_susceptibility` 45.1. The memo has no “Geoapify location context” section. The same scores are in `examples/sample_output/` (that copy was written as v0.3.0).

Files: `output/demo/flood_screening_report.md`, `aoi_screening.geojson`, `summary.json`, `tool_trace.json`, `layer_scores.png`.

## Step 2: `--place "West Lake, Hangzhou"`

Needs `GEOAPIFY_API_KEY`. Flood layers stay MOCK unless you pass `--mode live`. Leave `GEO_FLOOD_MODE` at `demo` (the value in `.env.example`) for this step.

```bash
python -m geo_flood_agent --place "West Lake, Hangzhou" --out output/place_demo
```

On a successful call, expect stdout along these lines:

- `geocode: LIVE` plus the formatted name the API returned
- `nearby places: LIVE count=` plus the number returned
- `isoline: LIVE mode=drive range_s=600`
- `layers: 0 LIVE / 3 MOCK`

The geocoded polygon is not `examples/sample_aoi.geojson`. The MOCK composite will **not** be 45.1. The AOI is the geocoder bbox when both sides are between 400 m and 8 km, otherwise a 2 km square around the point (`--aoi-buffer-m`). The memo states which rule was used. That polygon is not a flood extent.

A missing key exits with an error that names `GEOAPIFY_API_KEY` and does not write an AOI. A failed places or isoline call is tagged in the memo (see below). The screening memo is still written when geocoding succeeded.

Open `output/place_demo/flood_screening_report.md` and `aoi_screening.geojson`.

Flood rasters are a different command. They download Copernicus and JRC windows and are not required to see Geoapify:

```bash
python -m geo_flood_agent --place "West Lake, Hangzhou" --mode live --out output/place_live
```

## Step 3 (optional): MCP client

Screening does not call MCP. It uses REST. An AI client that should call Geoapify itself can use the snippet [`docs/geoapify-mcp.json.example`](geoapify-mcp.json.example). The header is `x-api-key: ${env:GEOAPIFY_API_KEY}`. Put the real key in the environment that starts the client. Do not paste it into the JSON file.

- Product: [https://www.geoapify.com](https://www.geoapify.com)
- MCP server docs: [https://apidocs.geoapify.com/docs/mcp/](https://apidocs.geoapify.com/docs/mcp/)
- Connect an AI client: [https://apidocs.geoapify.com/how-to/api-tooling-ai/connect-ai-client-mcp/](https://apidocs.geoapify.com/how-to/api-tooling-ai/connect-ai-client-mcp/)

The snippet URL is `https://api.geoapify.com/v1/mcp`. This package’s REST client uses `https://api.geoapify.com` as well. Geoapify also documents `https://api-eu.geoapify.com/v1/mcp`.

## Honesty tags

| Tag | Where you see it | Meaning |
|-----|------------------|---------|
| **MOCK** | Flood layer table in `--demo` and in `--place` without `--mode live` | Geometry heuristic. Not a satellite layer. Not mixed in as if it were a live raster. |
| **LIVE** | A Geoapify block, or a flood layer after `--mode live` | That response was fetched and parsed. A Geoapify **LIVE** tag is not a flood measurement. |
| **PENDING** | Isoline block | HTTP 202, and still no polygon after one follow-up by `id`. Nothing is drawn in its place. |
| **UNAVAILABLE** | Places or isoline block | That call failed. No names and no circle are filled in. |
| **SKIPPED** | Isoline block after `--no-isoline` | The isoline was not requested. Geocode and places still run. |

## What to look for

**Memo** (`flood_screening_report.md`)

- Disclaimer still says the flood scores are MOCK / DEMO on steps 1 and 2.
- Step 2 adds **Geoapify location context / Geoapify 地点上下文**, with a line that Geoapify is not a flood model and does not report flood depth.
- Three subsections, each with **Kind**: Geocode, Nearby places, Travel time / isoline.
- The places table lists only names the API returned (name, categories, distance, coordinates, place id). An empty LIVE response says none are listed.
- The isoline line names the geometry type and vertex count, and points at `role=geoapify_isoline`. English and Chinese notes say the polygon is a reachable area (`drive`, 600 s, `traffic=free_flow`), not a flood extent.
- The layer table underneath is still the three MOCK flood scores. Those kinds are separate from the Geoapify kinds.

**GeoJSON** (`aoi_screening.geojson`)

- `screening_aoi`: the polygon that was scored, with `composite_score_0_100` and `is_mock: true` on each flood layer. `geoapify_note` says the other features are not flood depth.
- `geoapify_geocode`: one point.
- `geoapify_place`: one point per returned place. Absent when places are not LIVE.
- `geoapify_isoline`: the polygon or multipolygon. Its `note` says it is not a flood extent and not a flood depth. Absent when the isoline is PENDING, UNAVAILABLE, or SKIPPED.

**Other files**

- `summary.json` → `geoapify` repeats the three kinds and place rows. Polygon coordinates stay in the GeoJSON.
- `tool_trace.json` lists `geocode_place`, `search_nearby_places`, and `travel_time_isoline` before the flood tools, with `"geoapify": "context-attached"`.
- `layer_scores.png` is a bar chart. MOCK bars are the sandy color, with the score and the word MOCK on the bar. Step 1 should show 39.1, 54.6, and 45.1.

`python -m geo_flood_agent --list-tools` includes `geocode_place`, `search_nearby_places`, and `travel_time_isoline`. Those three are not part of the `--demo` trace.

## If you capture images

This page has no screenshots. Four captures are enough:

1. Step 1 terminal: composite **45.1** and **0 LIVE / 3 MOCK**. Crop out any environment dump.
2. Step 2 terminal: geocode, nearby places, and isoline each **LIVE**, flood layers still **0 LIVE / 3 MOCK**. Crop the shell prompt if it could show a key.
3. The memo heading “Geoapify location context” with the three **Kind** rows and the sentence that this is not flood depth.
4. The isoline feature in the GeoJSON (`role` = `geoapify_isoline`) next to the screening polygon, so the reachable area is visibly a different object from the flood score.
