"""Deterministic bilingual narrative. Every number is taken from tool outputs."""

from __future__ import annotations

from geo_flood_agent.screening import ScreeningResult


def deterministic_narrative(result: ScreeningResult) -> dict[str, str]:
    parts_en: list[str] = []
    parts_zh: list[str] = []
    for ly in result.layers:
        kind = "MOCK" if ly.is_mock else "LIVE"
        parts_en.append(f"{ly.name} = {ly.score_0_100} ({ly.label}, {kind}).")
        parts_zh.append(f"{ly.name} = {ly.score_0_100}（{ly.label}，{kind}）。")

    ts = result.terrain_summary or {}
    ws = result.water_summary or {}
    centroid = ts.get("centroid_pixel") if ts else None
    if centroid and centroid.get("permanent_water"):
        parts_en.append(
            f"The AOI centroid ({centroid.get('lon')}, {centroid.get('lat')}) "
            f"has occurrence {centroid.get('occurrence_percent')}% and is permanent water, "
            "so that pixel is excluded from the score."
        )
        parts_zh.append(
            f"AOI 中心点（{centroid.get('lon')}, {centroid.get('lat')}）的出现频率为 "
            f"{centroid.get('occurrence_percent')}%，属于永久水体，该像素不进入得分。"
        )
    elif centroid and centroid.get("inside_grid"):
        parts_en.append(
            f"The AOI centroid ({centroid.get('lon')}, {centroid.get('lat')}) "
            f"occurrence is {centroid.get('occurrence_percent')}% "
            f"(permanent={centroid.get('permanent_water')}, included_in_score={centroid.get('included_in_score')})."
        )
        parts_zh.append(
            f"AOI 中心点（{centroid.get('lon')}, {centroid.get('lat')}）出现频率为 "
            f"{centroid.get('occurrence_percent')}%"
            f"（永久水体={centroid.get('permanent_water')}，纳入得分={centroid.get('included_in_score')}）。"
        )

    if ts:
        hand = ts.get("hand_land_m") or {}
        slope = ts.get("slope_land_deg") or {}
        parts_en.append(
            "On non-permanent land, HAND median is "
            f"{hand.get('median')} m and slope mean is {slope.get('mean')}°. "
            f"Permanent water covers {ts.get('permanent_water_percent_of_aoi')}% of the AOI "
            f"({ts.get('permanent_water_km2')} km²) and is excluded from the score. "
            f"Among AOI pixels with HAND < 2 m, {ts.get('hand_lt_2m_permanent_percent')}% "
            "are permanent water."
        )
        parts_zh.append(
            f"非永久水体陆地上，HAND 中位数为 {hand.get('median')} m，坡度均值为 {slope.get('mean')}°。"
            f"永久水体占 AOI 的 {ts.get('permanent_water_percent_of_aoi')}%"
            f"（{ts.get('permanent_water_km2')} km²），不进入得分。"
            f"AOI 内 HAND < 2 m 的像素中，{ts.get('hand_lt_2m_permanent_percent')}% 为永久水体。"
        )
    elif ws and ws.get("permanent_water_percent_of_aoi") is not None:
        parts_en.append(
            f"Permanent water covers {ws.get('permanent_water_percent_of_aoi')}% of the AOI "
            "and is excluded from the score."
        )
        parts_zh.append(
            f"永久水体占 AOI 的 {ws.get('permanent_water_percent_of_aoi')}%，不进入得分。"
        )

    if all(ly.is_mock for ly in result.layers):
        parts_en.append("No elevation or water raster was read. These scores are geometry heuristics.")
        parts_zh.append("没有读取高程或水体栅格。这些分数是几何启发式。")

    parts_en.append(
        f"Authoritative composite is {result.composite_score_0_100} ({result.composite_label}). "
        "This is a screening index, not a flood depth or a forecast."
    )
    parts_zh.append(
        f"以表格为准的综合分是 {result.composite_score_0_100}（{result.composite_label}）。"
        "这是筛查指数，不是洪水水深，也不是预报。"
    )
    return {"en": " ".join(parts_en), "zh": "".join(parts_zh), "source": "deterministic"}
