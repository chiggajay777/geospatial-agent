"""Bilingual (EN + ZH) markdown report writer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from geo_flood_agent import __version__
from geo_flood_agent.aoi import AOI
from geo_flood_agent.screening import ScreeningResult


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _cell(value: Any) -> str:
    if value is None:
        return "n/a"
    text = str(value).replace("|", "/").replace("\n", " ").strip()
    return text or "n/a"


def _geoapify_block(ctx: dict) -> str:
    """Bilingual place / travel-time section. Omitted entirely when Geoapify was not used."""
    geocode = ctx.get("geocode") or {}
    places = ctx.get("places") or {}
    isoline = ctx.get("isoline") or {}
    bbox = geocode.get("geocoder_bbox")
    bbox_text = "n/a" if not bbox else ", ".join(f"{float(v):.6f}" for v in bbox)
    bounds = geocode.get("aoi_bounds") or []
    bounds_text = "n/a" if len(bounds) != 4 else ", ".join(f"{float(v):.6f}" for v in bounds)
    confidence = geocode.get("confidence")
    confidence_text = "n/a" if confidence is None else _fmt(float(confidence), 3)
    buffer = geocode.get("aoi_buffer_m")
    buffer_text = "n/a" if buffer is None else f"{float(buffer):.0f} m"
    attr = geocode.get("attribution")
    attr_line = f"\nAttribution as returned: { _cell(attr) }\n" if attr else ""

    place_rows = []
    for row in places.get("places") or []:
        dist = row.get("distance_m")
        dist_text = "n/a" if dist is None else f"{float(dist):.0f}"
        cats = ", ".join(row.get("categories") or [])
        place_rows.append(
            "| "
            + " | ".join(
                [
                    _cell(row.get("name")),
                    _cell(cats),
                    dist_text,
                    f"{float(row['lon']):.6f}",
                    f"{float(row['lat']):.6f}",
                    _cell(row.get("place_id")),
                ]
            )
            + " |"
        )
    if places.get("kind") == "LIVE" and place_rows:
        places_table = (
            "| Name (as returned) | Categories | Distance (m) | Lon | Lat | Place id |\n"
            "|--------------------|------------|-------------:|----:|----:|----------|\n"
            + "\n".join(place_rows)
            + "\n"
        )
    elif places.get("kind") == "LIVE":
        places_table = (
            "Geoapify returned no places for this filter. None are listed.\n\n"
            "Geoapify 对此过滤条件没有返回地点。这里不列任何名称。\n"
        )
    else:
        places_table = (
            f"Request failed (`{_cell(places.get('kind'))}`). "
            f"Error: `{_cell(places.get('error'))}`. No names were filled in.\n"
        )

    iso_kind = isoline.get("kind") or "n/a"
    iso_bbox = isoline.get("bbox")
    iso_bbox_text = "n/a" if not iso_bbox else ", ".join(f"{float(v):.6f}" for v in iso_bbox)
    geom_types = ", ".join(isoline.get("geometry_types") or []) or "n/a"
    if iso_kind == "LIVE":
        iso_geom = (
            f"Geometry: {geom_types}, {isoline.get('vertex_count')} vertices. "
            f"Bounding box of the returned geometry (W,S,E,N): {iso_bbox_text}. "
            "The coordinates are in `aoi_screening.geojson` (`role=geoapify_isoline`).\n"
        )
    elif iso_kind == "PENDING":
        iso_geom = "No polygon was returned. Nothing was drawn in its place.\n"
    elif iso_kind == "SKIPPED":
        iso_geom = "No isoline request was sent.\n"
    else:
        iso_geom = "No polygon was returned. Nothing was drawn in its place.\n"

    place_attrs = places.get("attributions") or []
    place_attr = ""
    if place_attrs:
        place_attr = "Attribution as returned: " + "; ".join(_cell(a) for a in place_attrs) + "\n"

    return f"""## Geoapify location context / Geoapify 地点上下文

Geoapify supplied location context for this run. It is not a flood model and it does not report flood depth. Each block is tagged with the outcome of that call. **LIVE** means the body was fetched and parsed. Names that were not in the response are not added.

### Geocode / 地理编码

| Field | Value |
|-------|-------|
| Kind | **{_cell(geocode.get("kind"))}** |
| Endpoint | `{_cell(geocode.get("endpoint"))}` |
| Query | `{_cell(geocode.get("query"))}` |
| Formatted result | {_cell(geocode.get("formatted"))} |
| Name | {_cell(geocode.get("name"))} |
| Coordinates (lon, lat) | {_fmt(geocode.get("lon"), 6)}, {_fmt(geocode.get("lat"), 6)} |
| Result type | {_cell(geocode.get("result_type"))} |
| Rank confidence | {confidence_text} |
| Place id | `{_cell(geocode.get("place_id"))}` |
| HTTP status | {_cell(geocode.get("http_status"))} |
| Geocoder bbox (W,S,E,N) | {bbox_text} |
| AOI method | `{_cell(geocode.get("aoi_method"))}` |
| Point-buffer radius | {buffer_text} |
| AOI bounds (W,S,E,N) | {bounds_text} |
{attr_line}
- EN: {geocode.get("note_en") or ""}
- 中文: {geocode.get("note_zh") or ""}

### Nearby places / assets / 周边地点与设施

| Field | Value |
|-------|-------|
| Kind | **{_cell(places.get("kind"))}** |
| Endpoint | `{_cell(places.get("endpoint"))}` |
| Filter | `{_cell(places.get("filter"))}` |
| Bias | `{_cell(places.get("bias"))}` |
| Categories | `{_cell(", ".join(places.get("categories") or []))}` |
| Limit | {_cell(places.get("limit"))} |
| Returned | {_cell(places.get("count"))} |
| Skipped (no coordinates) | {_cell(places.get("skipped_without_coordinates"))} |
| HTTP status | {_cell(places.get("http_status"))} |

{place_attr}{places_table}
- EN: {places.get("note_en") or ""}
- 中文: {places.get("note_zh") or ""}

Order is the API order. Distance is the value Geoapify sent (meters to the biased point), or n/a when the field was absent.

### Travel time / isoline / 出行时间等时圈

| Field | Value |
|-------|-------|
| Kind | **{_cell(iso_kind)}** |
| Endpoint | `{_cell(isoline.get("endpoint"))}` |
| Type | `{_cell(isoline.get("isoline_type"))}` |
| Mode | `{_cell(isoline.get("mode"))}` |
| Range (seconds) | {_cell(isoline.get("range_seconds"))} |
| Traffic model | `{_cell(isoline.get("traffic"))}` |
| Origin (lon, lat) | {_fmt(isoline.get("origin_lon"), 6)}, {_fmt(isoline.get("origin_lat"), 6)} |
| Isoline id | `{_cell(isoline.get("isoline_id"))}` |
| HTTP status | {_cell(isoline.get("http_status"))} |
| Follow-up by id | {_cell(isoline.get("follow_up"))} |

{iso_geom}
- EN: {isoline.get("note_en") or ""}
- 中文: {isoline.get("note_zh") or ""}

---

"""


def write_markdown_report(
    aoi: AOI,
    result: ScreeningResult,
    out_path: Path,
    figure_paths: list[Path] | None = None,
    narrative: dict | None = None,
    trace: dict | None = None,
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    minx, miny, maxx, maxy = result.aoi_bounds
    lon, lat = result.aoi_centroid

    layer_rows = []
    for ly in result.layers:
        mock_tag = "MOCK" if ly.is_mock else "LIVE"
        layer_rows.append(
            f"| `{ly.name}` | {ly.score_0_100} | {ly.label} | {mock_tag} | {ly.data_source} |"
        )
    layers_table = "\n".join(layer_rows)

    layer_notes = []
    for ly in result.layers:
        layer_notes.append(
            f"### `{ly.name}` ({ly.label}, {ly.score_0_100}/100)\n\n"
            f"- EN: {ly.notes_en}\n"
            f"- 中文: {ly.notes_zh}\n"
        )
    notes_block = "\n".join(layer_notes)

    terrain_block = "_No continuous DEM in this run._"
    ts = result.terrain_summary or {}
    if ts:
        hand = ts.get("hand_land_m") or {}
        slope = ts.get("slope_land_deg") or {}
        elev = ts.get("elevation_aoi") or {}
        terrain_block = (
            "| Quantity | Value |\n"
            "|----------|-------|\n"
            f"| Grid | { _fmt(ts.get('res_m'), 0) } m, {ts.get('aoi_pixels')} AOI pixels |\n"
            f"| Elevation mean (m, AOI) | {_fmt(elev.get('mean'))} |\n"
            f"| HAND median / mean on land (m) | {_fmt(hand.get('median'))} / {_fmt(hand.get('mean'))} |\n"
            f"| Slope mean on land (deg) | {_fmt(slope.get('mean'))} |\n"
            f"| Land scored | {ts.get('land_km2_scored')} km² ({ts.get('land_pixels_scored')} px) |\n"
            f"| Permanent water | {ts.get('permanent_water_km2')} km² "
            f"({ts.get('permanent_water_percent_of_aoi')}% of AOI) |\n"
            f"| HAND < 2 m on land | {ts.get('hand_lt_2m_land_percent')}% |\n"
            f"| Share of HAND<2 m that is permanent water | {ts.get('hand_lt_2m_permanent_percent')}% |\n"
            f"| Unresolved HAND on non-permanent land | {ts.get('hand_unresolved_land_percent')}% |\n"
        )
        c = ts.get("centroid_pixel") or {}
        if c:
            terrain_block += (
                f"| AOI centroid pixel ({c.get('lon')}, {c.get('lat')}) | "
                f"occurrence {_fmt(c.get('occurrence_percent'))}%, "
                f"permanent={c.get('permanent_water')}, included_in_score={c.get('included_in_score')}, "
                f"HAND {_fmt(c.get('hand_m'))} m |\n"
            )
    elif result.elevation and result.elevation.get("ok"):
        e = result.elevation
        terrain_block = (
            f"Sparse fallback only ({e.get('provider')}), n={e.get('n_points')}, "
            f"mean={e.get('elev_mean_m')} m. HAND was not computed."
        )

    water_block = "_No JRC occurrence raster in this run._"
    ws = result.water_summary or {}
    if ws:
        hydro = ws.get("hydro_score_land") or {}
        water_block = (
            "| Quantity | Value |\n"
            "|----------|-------|\n"
            f"| Product | {ws.get('product')} |\n"
            f"| Permanent threshold | occurrence ≥ {_fmt(ws.get('permanent_threshold'), 0)}% |\n"
            f"| Permanent water | {ws.get('permanent_water_km2')} km² "
            f"({ws.get('permanent_water_percent_of_aoi')}% of AOI), excluded from the score |\n"
            f"| Seasonal-water land pixels | {ws.get('seasonal_water_land_pixels')} |\n"
            f"| Hydro score mean on land | {_fmt(hydro.get('mean'))} |\n"
        )

    params = result.parameters or {}
    param_rows = "\n".join(f"| `{k}` | {v} |" for k, v in params.items())
    transfers = result.transfers or {}
    get_bytes = transfers.get("http_get_bytes")
    head_bytes = transfers.get("largest_head_content_length")

    narrative = narrative or {}
    trace = trace or {}
    if narrative.get("source") == "llm":
        narr_body = (
            f"**Source / 来源:** LLM `{narrative.get('model')}` "
            "(number audit passed; tables remain authoritative)\n\n"
            f"{narrative.get('en', '')}\n"
        )
    else:
        narr_body = (
            "**Source / 来源:** deterministic template (no LLM, or the LLM draft was rejected)\n\n"
            f"- EN: {narrative.get('en', '')}\n"
            f"- 中文: {narrative.get('zh', '')}\n"
        )
        if trace.get("fallback_reason"):
            narr_body += f"\nLLM fallback reason: `{trace['fallback_reason']}`\n"
        if trace.get("number_audit_rejected"):
            narr_body += (
                "\nRejected LLM numbers (not in tool outputs): "
                + ", ".join(str(x) for x in trace["number_audit_rejected"])
                + "\n"
            )

    geo_block = _geoapify_block(result.geo_context) if result.geo_context else ""

    fig_block = ""
    if figure_paths:
        lines = ["## Figures / 附图\n"]
        for p in figure_paths:
            rel = f"figures/{p.name}"
            lines.append(f"![{p.stem}]({rel})\n")
        fig_block = "\n".join(lines) + "\n---\n"

    body = f"""# Flood / Inundation Screening Report
# 洪水 / 淹没筛查报告

**Generated:** {_ts()}  
**Tool:** geo-flood-agent v{__version__}  
**Mode:** `{result.mode}`  
**Planner:** `{(trace or {}).get("planner", "deterministic")}`

---

## Disclaimer / 免责声明

> **EN:** {result.disclaimer_en}

> **中文:** {result.disclaimer_zh}

---

## AOI Summary / 兴趣区摘要

| Field | Value |
|------|-------|
| Source | `{aoi.source}` |
| CRS | {aoi.crs} |
| Bounds (W,S,E,N) | {minx:.6f}, {miny:.6f}, {maxx:.6f}, {maxy:.6f} |
| Centroid (lon, lat) | {lon:.6f}, {lat:.6f} |
| Approx. area | {result.aoi_area_km2} km² |

---

{geo_block}## Composite Result / 综合结果

| Metric | Value |
|--------|-------|
| Composite score (0–100, higher = more concern) | **{result.composite_score_0_100}** |
| Composite label | **{result.composite_label}** |

The composite is the mean susceptibility on **non-permanent land**. Permanent water is masked. MOCK layers are not blended into a live composite.

### Interpretation (EN)
- `low` (<25): Relatively lower inundation concern for this AOI (screening only).
- `moderate` (25–49): Mixed signals; treat as screening-only.
- `elevated` (50–74): Elevated concern — prioritize real floodplain / hydraulic next steps.
- `high` (≥75): High concern — **still not engineering-grade**.

### 解读（中文）
- `low`（<25）：相对较低的淹没关注度（仅筛查）。
- `moderate`（25–49）：信号混合，仅作筛查。
- `elevated`（50–74）：关注度偏高 — 下一步应做真实洪泛区 / 水动力分析。
- `high`（≥75）：关注度高 — **仍非工程级**。

---

## Narrative / 叙述

{narr_body}

---

## Terrain (HAND and slope) / 地形

{terrain_block}

---

## Surface water / 地表水

{water_block}

---

## Layer Scores / 图层得分

| Layer | Score | Label | Kind | Data source |
|-------|-------|-------|------|-------------|
{layers_table}

---

## Layer Notes / 图层说明

{notes_block}

---

## Parameters / 参数

These were fixed before looking at validation results.

| Parameter | Value |
|-----------|-------|
{param_rows}

HTTP GET bytes recorded for COG range reads this run: `{get_bytes}`.  
Largest HEAD Content-Length seen (full source object, not downloaded in full): `{head_bytes}`.

---

{fig_block}
## Method

1. Validate AOI (Shapely). Reject boxes larger than 5° on a side.
2. **Live terrain:** windowed read of Copernicus DEM GLO-30 COGs from the public AWS bucket, reprojected to the local UTM zone. Priority-flood (Barnes et al. 2014) + D8 (O'Callaghan & Mark 1984) + HAND (Nobre et al. 2011) and slope. The DEM is a DSM.
3. **Live water:** windowed read of JRC Global Surface Water occurrence (Planetary Computer COG). Occurrence ≥ threshold is permanent water and is removed from the land score.
4. **Composite:** mean of `0.7 * terrain + 0.3 * occurrence-wetness` on the remaining land. A failed source becomes a labeled MOCK layer and is left out of the composite.
5. **Planner:** deterministic tool sequence by default. With `--llm`, an OpenAI-compatible model calls the same tools and may draft the narrative; numbers that are not in the tool JSON are rejected.
6. Tool trace: `tool_trace.json`.

---

## What this is not

Not a hydraulic simulation, not a flood forecast, not an insurance zone, and not a substitute for a surveyed floodplain. Copernicus GLO-30 is a surface model (vegetation and buildings included). JRC occurrence is a multi-year water history, so it can include past floods; it is used here as context and as a permanent-water mask, not as proof of a future event.

---

*End of report. MOCK vs LIVE is labeled per layer. Do not present mock scores as observed flood maps.*
"""
    out_path.write_text(body, encoding="utf-8")
    return out_path
