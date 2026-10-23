"""Flood / inundation screening.

- demo/mock: deterministic geometry heuristics, labeled MOCK, no network.
- live: Copernicus GLO-30 HAND + slope, and JRC Global Surface Water occurrence.
  Permanent water is masked. It is not scored as flood-prone land.
  If a live source fails, that layer falls back to MOCK and is excluded from
  the composite (a mock number does not drive the score).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from geo_flood_agent import __version__ as __version_product__
from geo_flood_agent.aoi import AOI
from geo_flood_agent.cog import TransferStats
from geo_flood_agent.constants import (
    BUFFER_M,
    HAND_CAP_M,
    PERMANENT_OCCURRENCE,
    RES_M,
    SLOPE_CAP_DEG,
    STREAM_AREA_KM2,
    W_TERRAIN,
    W_WATER,
)
from geo_flood_agent.elevation import elevation_to_terrain_score, fetch_elevation_stats
from geo_flood_agent.scoring import summarize_aoi


@dataclass
class LayerResult:
    name: str
    score_0_100: float
    label: str
    notes_en: str
    notes_zh: str
    data_source: str
    is_mock: bool = True
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScreeningResult:
    aoi_bounds: tuple[float, float, float, float]
    aoi_centroid: tuple[float, float]
    aoi_area_km2: float
    composite_score_0_100: float
    composite_label: str
    layers: list[LayerResult] = field(default_factory=list)
    mode: str = "demo"
    disclaimer_en: str = ""
    disclaimer_zh: str = ""
    elevation: dict[str, Any] | None = None
    terrain_summary: dict[str, Any] | None = None
    water_summary: dict[str, Any] | None = None
    parameters: dict[str, Any] | None = None
    transfers: dict[str, Any] | None = None
    # Geoapify place / travel-time context. Not a flood layer and not scored.
    geo_context: dict[str, Any] | None = None
    # Numpy grids for figures. Never serialized.
    rasters: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        rasters = self.rasters
        self.rasters = None
        try:
            d = asdict(self)
        finally:
            self.rasters = rasters
        d.pop("rasters", None)
        d["aoi_bounds"] = list(self.aoi_bounds)
        d["aoi_centroid"] = list(self.aoi_centroid)
        return d


def _label(score: float) -> str:
    if score < 25:
        return "low"
    if score < 50:
        return "moderate"
    if score < 75:
        return "elevated"
    return "high"


def _stable_unit(seed: str) -> float:
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def _jsonable(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {k: v for k, v in summary.items() if not str(k).startswith("_")}


def _mock_hydrology_proxy(aoi: AOI) -> LayerResult:
    lon, lat = aoi.centroid
    minx, miny, maxx, maxy = aoi.bounds
    lat_factor = max(0.0, 1.0 - abs(lat) / 60.0)
    lon_wave = 0.5 + 0.5 * math.sin(math.radians(lon * 3.0))
    jitter = _stable_unit(f"hydro:{minx:.4f}:{miny:.4f}:{maxx:.4f}:{maxy:.4f}")
    score = 15 + 55 * lat_factor * lon_wave + 20 * jitter
    score = max(0.0, min(100.0, score))
    return LayerResult(
        name="hydrology_proxy",
        score_0_100=round(score, 1),
        label=_label(score),
        notes_en=(
            "MOCK heuristic from AOI centroid latitude/longitude only. "
            "Not stream networks, not rainfall, not floodplain maps."
        ),
        notes_zh=(
            "【模拟】仅用 AOI 中心点经纬度做启发式打分。"
            "不是河网、不是降雨、不是官方淹没区图。"
        ),
        data_source="MOCK (deterministic heuristic from AOI geometry)",
        is_mock=True,
    )


def _mock_terrain_proxy(aoi: AOI) -> LayerResult:
    minx, miny, maxx, maxy = aoi.bounds
    w = max(maxx - minx, 1e-9)
    h = max(maxy - miny, 1e-9)
    aspect = min(w / h, h / w)
    area = aoi.approx_area_km2()
    area_factor = min(1.0, math.log10(max(area, 0.01) + 1) / 2.0)
    jitter = _stable_unit(f"terrain:{minx:.4f}:{miny:.4f}:{maxx:.4f}:{maxy:.4f}")
    score = 10 + 40 * (1.0 - aspect) + 30 * area_factor + 15 * jitter
    score = max(0.0, min(100.0, score))
    return LayerResult(
        name="terrain_flatness_proxy",
        score_0_100=round(score, 1),
        label=_label(score),
        notes_en=(
            "MOCK: bbox aspect ratio + area used as a stand-in for flatness. "
            "Use --mode live for Copernicus GLO-30 HAND and slope."
        ),
        notes_zh=(
            "【模拟】用包围盒长宽比与面积代替地形平坦度。"
            "使用 --mode live 可计算 Copernicus GLO-30 的 HAND 与坡度。"
        ),
        data_source="MOCK (bbox geometry proxy; no DEM fetched)",
        is_mock=True,
    )


def _mock_overlay(aoi: AOI, hydro: LayerResult, terrain: LayerResult) -> LayerResult:
    jitter = _stable_unit(f"inund:{aoi.bounds}")
    score = 0.55 * hydro.score_0_100 + 0.35 * terrain.score_0_100 + 10 * jitter
    score = max(0.0, min(100.0, score))
    return LayerResult(
        name="inundation_susceptibility",
        score_0_100=round(score, 1),
        label=_label(score),
        notes_en=(
            "MOCK susceptibility blending hydrology_proxy + terrain proxy. "
            "NOT satellite water detection, NOT insurance flood zones."
        ),
        notes_zh=(
            "【模拟】淹没易感性 = 水文代理与地形代理加权。"
            "不是卫星水体提取，也不是保险洪水区划。"
        ),
        data_source="MOCK (derived from mock layers)",
        is_mock=True,
    )


def _sparse_terrain_fallback(aoi: AOI, open_elevation_url: str | None, reason: str) -> tuple[LayerResult, dict]:
    """Last-resort LIVE path: sparse point elevations. Not HAND."""
    stats = fetch_elevation_stats(aoi, grid_n=5, preferred="opentopodata", open_elevation_url=open_elevation_url)
    info = stats.to_dict()
    info["fallback_reason"] = reason
    if not stats.ok:
        mock = _mock_terrain_proxy(aoi)
        mock.notes_en = f"LIVE terrain FAILED ({reason}; sparse DEM also failed: {stats.error}). " + mock.notes_en
        mock.notes_zh = f"【live 地形失败：{reason}；稀疏 DEM 也失败：{stats.error}】" + mock.notes_zh
        mock.data_source = f"MOCK fallback after live failure ({reason})"
        mock.extras = {"elevation_error": stats.error, "copernicus_error": reason}
        return mock, info
    score, notes_en, notes_zh = elevation_to_terrain_score(stats)
    layer = LayerResult(
        name="terrain_dem_samples",
        score_0_100=score,
        label=_label(score),
        notes_en=(
            f"Copernicus HAND was not available ({reason}). Fell back to sparse point samples. "
            + notes_en
        ),
        notes_zh=(
            f"Copernicus HAND 不可用（{reason}）。已回退到稀疏高程点。"
            + notes_zh
        ),
        data_source=f"LIVE sparse DEM fallback via {stats.provider} (HAND not computed)",
        is_mock=False,
        extras={
            "elev_mean_m": stats.elev_mean_m,
            "elev_min_m": stats.elev_min_m,
            "elev_max_m": stats.elev_max_m,
            "relief_m": stats.relief_m,
            "n_points": stats.n_points,
            "provider": stats.provider,
            "hand": False,
        },
    )
    return layer, info


def _live_layers(
    aoi: AOI,
    *,
    open_elevation_url: str | None,
    stats: TransferStats,
) -> tuple[LayerResult, LayerResult, LayerResult, dict[str, Any]]:
    from geo_flood_agent.dem import fetch_dem_grid
    from geo_flood_agent.surface_water import fetch_occurrence

    dem_pack = None
    dem_error = None
    try:
        dem_pack = fetch_dem_grid(aoi, stats=stats)
    except Exception as exc:  # noqa: BLE001
        dem_error = f"{type(exc).__name__}: {exc}"

    water_pack = None
    water_error = None
    if dem_pack is not None:
        try:
            water_pack = fetch_occurrence(aoi, dem_pack["_grid"], stats=stats)
        except Exception as exc:  # noqa: BLE001
            water_error = f"{type(exc).__name__}: {exc}"

    context: dict[str, Any] = {
        "dem_error": dem_error,
        "water_error": water_error,
        "rasters": None,
        "terrain_summary": None,
        "water_summary": None,
        "elevation": None,
    }

    if dem_pack is None:
        terrain, elev = _sparse_terrain_fallback(aoi, open_elevation_url, dem_error or "unknown")
        context["elevation"] = elev
        if water_pack is None:
            # Try water on its own grid so a DEM failure does not hide a working water layer.
            try:
                from geo_flood_agent.cog import analysis_grid
                from geo_flood_agent.constants import MAX_GRID_SIDE

                grid = analysis_grid(aoi.bounds, buffer_m=0.0, res_m=RES_M, max_side=MAX_GRID_SIDE)
                water_pack = fetch_occurrence(aoi, grid, stats=stats)
                context["water_error"] = None
                context["_water_grid_only"] = grid
            except Exception as exc:  # noqa: BLE001
                water_error = f"{type(exc).__name__}: {exc}"
                context["water_error"] = water_error
        if water_pack is None:
            hydro = _mock_hydrology_proxy(aoi)
            hydro.notes_en = f"LIVE surface water FAILED ({water_error}). " + hydro.notes_en
            hydro.notes_zh = f"【live 地表水失败：{water_error}】" + hydro.notes_zh
            hydro.data_source = f"MOCK fallback after live failure ({water_error})"
            inund = _score_from_scalars(terrain, hydro)
            return hydro, terrain, inund, context
        # Water without HAND: score water only.
        hydro, water_summary, rasters = _water_only_layer(aoi, water_pack, context.get("_water_grid_only"))
        context["water_summary"] = water_summary
        context["rasters"] = rasters
        inund = _inundation_from_live(terrain_score=None, hydro_score=hydro.score_0_100, mixed_mock=terrain.is_mock)
        if terrain.is_mock:
            inund.notes_en += " Terrain fell back to MOCK and was excluded from the composite."
            inund.notes_zh += " 地形回退为 MOCK，未进入综合分。"
        return hydro, terrain, inund, context

    occurrence = None if water_pack is None else water_pack["occurrence"]
    permanent = None if water_pack is None else water_pack["permanent"]
    summary = summarize_aoi(
        dem=dem_pack["dem"],
        hand=dem_pack["hand"],
        slope=dem_pack["slope"],
        terrain=dem_pack["concern"],
        inside=dem_pack["inside"],
        occurrence=occurrence,
        permanent=permanent,
        res_m=float(dem_pack["grid"]["res_m"]),
    )
    context["terrain_summary"] = _jsonable(summary)
    context["terrain_summary"]["centroid_pixel"] = _centroid_pixel(
        aoi,
        dem_pack["grid"],
        dem=dem_pack["dem"],
        hand=dem_pack["hand"],
        occurrence=occurrence,
        permanent=permanent,
        land=summary.get("_land"),
    )
    context["rasters"] = {
        "dem": dem_pack["dem"],
        "hand": dem_pack["hand"],
        "slope": dem_pack["slope"],
        "concern": dem_pack["concern"],
        "inside": dem_pack["inside"],
        "occurrence": occurrence,
        "permanent": permanent if permanent is not None else np.zeros(dem_pack["dem"].shape, dtype=bool),
        "hydro": summary.get("_hydro"),
        "composite": summary.get("_composite"),
        "land": summary.get("_land"),
        "grid": dem_pack["grid"],
        "sources_dem": dem_pack["sources"],
        "sources_water": None if water_pack is None else water_pack["sources"],
    }
    context["elevation"] = {
        "ok": True,
        "provider": "Copernicus DEM GLO-30 (AWS COG)",
        "product": "DSM",
        "grid_crs": dem_pack["grid"]["crs"],
        "res_m": dem_pack["grid"]["res_m"],
        "width": dem_pack["grid"]["width"],
        "height": dem_pack["grid"]["height"],
        "buffer_m": dem_pack["buffer_m"],
        "stream_area_km2": dem_pack["stream_area_km2"],
        "sources": dem_pack["sources"],
        "elev_mean_m": (summary["elevation_aoi"] or {}).get("mean"),
        "elev_min_m": None,
        "elev_max_m": None,
        "hand_land_mean_m": (summary["hand_land_m"] or {}).get("mean"),
        "hand_land_median_m": (summary["hand_land_m"] or {}).get("median"),
        "slope_land_mean_deg": (summary["slope_land_deg"] or {}).get("mean"),
    }
    # min/max elevation inside AOI
    inside = dem_pack["inside"] & np.isfinite(dem_pack["dem"])
    if inside.any():
        context["elevation"]["elev_min_m"] = round(float(np.min(dem_pack["dem"][inside])), 2)
        context["elevation"]["elev_max_m"] = round(float(np.max(dem_pack["dem"][inside])), 2)

    terrain = _terrain_layer(summary, dem_pack)
    if water_pack is None:
        hydro = _mock_hydrology_proxy(aoi)
        hydro.notes_en = f"LIVE surface water FAILED ({water_error}). " + hydro.notes_en
        hydro.notes_zh = f"【live 地表水失败：{water_error}】" + hydro.notes_zh
        hydro.data_source = f"MOCK fallback after live failure ({water_error})"
        score = (summary["terrain_score_land"] or {}).get("mean")
        inund = _inundation_from_live(terrain_score=score, hydro_score=None, mixed_mock=True)
        inund.notes_en += " MOCK hydrology was excluded from the composite."
        inund.notes_zh += " MOCK 水文未进入综合分。"
        return hydro, terrain, inund, context

    context["water_summary"] = {
        "product": water_pack["product"],
        "sources": water_pack["sources"],
        "items": water_pack["source_items"],
        "permanent_threshold": water_pack["permanent_threshold"],
        "permanent_water_km2": summary["permanent_water_km2"],
        "permanent_water_percent_of_aoi": summary["permanent_water_percent_of_aoi"],
        "seasonal_water_land_pixels": summary["seasonal_water_land_pixels"],
        "hydro_score_land": summary["hydro_score_land"],
        "hand_lt_2m_permanent_percent": summary["hand_lt_2m_permanent_percent"],
    }
    hydro = _water_layer(summary, water_pack)
    comp_mean = (summary["composite_score_land"] or {}).get("mean")
    inund = _inundation_from_live(terrain_score=comp_mean, hydro_score=None, mixed_mock=False, already_composite=True)
    return hydro, terrain, inund, context


def _centroid_pixel(aoi: AOI, grid_meta: dict, **arrays) -> dict[str, Any]:
    """Sample the AOI centroid on the analysis grid. Used to show masking, not as a score."""
    from rasterio.transform import Affine, rowcol
    from rasterio.warp import transform as warp_xy

    lon, lat = aoi.centroid
    xs, ys = warp_xy("EPSG:4326", grid_meta["crs"], [lon], [lat])
    transform = Affine(*grid_meta["transform"])
    row, col = rowcol(transform, xs[0], ys[0])
    height = int(grid_meta["height"])
    width = int(grid_meta["width"])
    out: dict[str, Any] = {"lon": round(lon, 6), "lat": round(lat, 6), "row": int(row), "col": int(col)}
    if not (0 <= row < height and 0 <= col < width):
        out["inside_grid"] = False
        return out
    out["inside_grid"] = True

    def _num(name: str):
        arr = arrays.get(name)
        if arr is None:
            return None
        value = arr[row, col]
        if isinstance(value, (bool, np.bool_)):
            return bool(value)
        if not np.isfinite(value):
            return None
        return round(float(value), 3)

    out["elevation_m"] = _num("dem")
    out["hand_m"] = _num("hand")
    out["occurrence_percent"] = _num("occurrence")
    perm = arrays.get("permanent")
    out["permanent_water"] = None if perm is None else bool(perm[row, col])
    land = arrays.get("land")
    out["included_in_score"] = None if land is None else bool(land[row, col])
    return out


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _terrain_layer(summary: dict[str, Any], dem_pack: dict[str, Any]) -> LayerResult:
    stats = summary["terrain_score_land"] or {}
    score = stats.get("mean")
    if score is None:
        score = 0.0
        note_fail = " No resolved land pixels (AOI may be entirely permanent water or nodata)."
    else:
        note_fail = ""
    hand = summary["hand_land_m"] or {}
    slope = summary["slope_land_deg"] or {}
    notes_en = (
        f"Copernicus DEM GLO-30 DSM, {summary['res_m']:.0f} m grid, "
        f"{dem_pack['buffer_m']:.0f} m buffer for flow. "
        f"HAND uses D8 drainage with upstream area ≥ {dem_pack['stream_area_km2']} km². "
        f"On non-permanent land: HAND median {_fmt(hand.get('median'))} m, "
        f"mean {_fmt(hand.get('mean'))} m; slope mean {_fmt(slope.get('mean'))}°. "
        f"Score = 80% HAND (0 at ≥ {HAND_CAP_M:.0f} m) + 20% slope (0 at ≥ {SLOPE_CAP_DEG:.0f}°). "
        f"Land with HAND < 2 m: {summary['hand_lt_2m_land_percent']}%. "
        f"Unresolved HAND: {summary['hand_unresolved_land_percent']}% of non-permanent land. "
        f"Of AOI pixels with HAND < 2 m, {summary['hand_lt_2m_permanent_percent']}% are permanent water and are excluded."
        + note_fail
        + " Not a hydraulic flood depth."
    )
    notes_zh = (
        f"Copernicus DEM GLO-30（DSM），{summary['res_m']:.0f} m 格网，"
        f"汇流缓冲 {dem_pack['buffer_m']:.0f} m。"
        f"HAND 的排水网络为 D8 上游面积 ≥ {dem_pack['stream_area_km2']} km²。"
        f"非永久水体陆地上：HAND 中位数 {_fmt(hand.get('median'))} m，"
        f"均值 {_fmt(hand.get('mean'))} m；坡度均值 {_fmt(slope.get('mean'))}°。"
        f"得分 = 80% HAND（≥ {HAND_CAP_M:.0f} m 记 0）+ 20% 坡度（≥ {SLOPE_CAP_DEG:.0f}° 记 0）。"
        f"HAND < 2 m 的陆地占 {summary['hand_lt_2m_land_percent']}%。"
        f"HAND 未解析像素占非永久水体陆地的 {summary['hand_unresolved_land_percent']}%。"
        f"AOI 内 HAND < 2 m 的像素中，{summary['hand_lt_2m_permanent_percent']}% 是永久水体，已排除、不作为易涝陆地。"
        + (" 没有可评分的陆地像素。" if note_fail else "")
        + " 不是水动力洪水水深。"
    )
    return LayerResult(
        name="terrain_hand",
        score_0_100=round(float(score), 1),
        label=_label(float(score)),
        notes_en=notes_en,
        notes_zh=notes_zh,
        data_source="LIVE Copernicus DEM GLO-30 (AWS public COG) → HAND + slope",
        is_mock=False,
        extras={
            "res_m": summary["res_m"],
            "hand_land_median_m": hand.get("median"),
            "hand_land_mean_m": hand.get("mean"),
            "slope_land_mean_deg": slope.get("mean"),
            "hand_lt_2m_land_percent": summary["hand_lt_2m_land_percent"],
            "hand_lt_2m_permanent_percent": summary["hand_lt_2m_permanent_percent"],
            "permanent_water_percent_of_aoi": summary["permanent_water_percent_of_aoi"],
            "sources": dem_pack["sources"],
        },
    )


def _water_layer(summary: dict[str, Any], water_pack: dict[str, Any]) -> LayerResult:
    stats = summary["hydro_score_land"] or {}
    score = stats.get("mean")
    if score is None:
        score = 0.0
    notes_en = (
        f"JRC Global Surface Water occurrence. Permanent water is occurrence ≥ "
        f"{water_pack['permanent_threshold']:.0f}% and is masked "
        f"({summary['permanent_water_km2']} km², {summary['permanent_water_percent_of_aoi']}% of the AOI). "
        f"The hydro score is the mean of occurrence/{water_pack['permanent_threshold']:.0f} on the remaining land "
        f"(0 = never observed wet, 100 = just below the permanent threshold). "
        f"Seasonal (0 < occurrence < threshold) land pixels: {summary['seasonal_water_land_pixels']}. "
        "This is multi-year water history, not a forecast and not this season's flood map."
    )
    notes_zh = (
        f"JRC 全球地表水出现频率。永久水体定义为出现频率 ≥ {water_pack['permanent_threshold']:.0f}%，"
        f"已掩膜（{summary['permanent_water_km2']} km²，占 AOI 的 {summary['permanent_water_percent_of_aoi']}%），"
        f"不计入易涝陆地。"
        f"水文得分是剩余陆地上 occurrence/{water_pack['permanent_threshold']:.0f} 的均值"
        f"（0 = 从未观测到水体，100 = 略低于永久水体阈值）。"
        f"季节性水体陆地像素：{summary['seasonal_water_land_pixels']}。"
        "这是多年水体历史，不是预报，也不是当季洪水图。"
    )
    return LayerResult(
        name="surface_water",
        score_0_100=round(float(score), 1),
        label=_label(float(score)),
        notes_en=notes_en,
        notes_zh=notes_zh,
        data_source="LIVE " + water_pack["product"],
        is_mock=False,
        extras={
            "permanent_water_km2": summary["permanent_water_km2"],
            "permanent_water_percent_of_aoi": summary["permanent_water_percent_of_aoi"],
            "permanent_threshold": water_pack["permanent_threshold"],
            "seasonal_water_land_pixels": summary["seasonal_water_land_pixels"],
            "hydro_mean": stats.get("mean"),
            "sources": water_pack["sources"],
        },
    )


def _water_only_layer(aoi: AOI, water_pack: dict, grid: dict | None):
    from geo_flood_agent.cog import rasterize_aoi

    if grid is None:
        raise RuntimeError("water-only path requires a grid")
    inside = rasterize_aoi(aoi.geometry, grid)
    # No HAND: terrain arrays are nan so land == inside & ~permanent.
    nan = np.full(inside.shape, np.nan, dtype=np.float32)
    # Treat every finite occurrence pixel as "resolved" by stuffing a dummy finite hand outside permanent.
    dummy_hand = np.zeros(inside.shape, dtype=np.float64)
    dummy_slope = np.zeros(inside.shape, dtype=np.float64)
    summary = summarize_aoi(
        dem=nan,
        hand=dummy_hand,
        slope=dummy_slope,
        terrain=nan,
        inside=inside,
        occurrence=water_pack["occurrence"],
        permanent=water_pack["permanent"],
        res_m=float(grid["res_m"]),
    )
    hydro = _water_layer(summary, water_pack)
    safe = _jsonable(summary) or {}
    # Dummy zeros were only there so the land mask could be built. They are not HAND.
    for key in (
        "hand_land_m",
        "slope_land_deg",
        "terrain_score_land",
        "elevation_aoi",
        "hand_lt_2m_aoi_pixels",
        "hand_lt_2m_permanent_pixels",
        "hand_lt_2m_permanent_percent",
        "hand_lt_2m_land_percent",
        "hand_unresolved_land_pixels",
        "hand_unresolved_land_percent",
        "composite_score_land",
    ):
        safe.pop(key, None)
    safe["hand_computed"] = False
    rasters = {
        "dem": nan,
        "hand": nan,
        "slope": nan,
        "concern": nan,
        "inside": inside,
        "occurrence": water_pack["occurrence"],
        "permanent": water_pack["permanent"],
        "hydro": summary.get("_hydro"),
        "composite": summary.get("_hydro"),
        "land": summary.get("_land"),
        "grid": {
            "crs": grid["crs"],
            "epsg": grid["epsg"],
            "res_m": grid["res_m"],
            "width": grid["width"],
            "height": grid["height"],
            "transform": tuple(grid["transform"])[:6],
        },
        "sources_dem": [],
        "sources_water": water_pack["sources"],
    }
    return hydro, safe, rasters


def _inundation_from_live(
    *,
    terrain_score: float | None,
    hydro_score: float | None,
    mixed_mock: bool,
    already_composite: bool = False,
) -> LayerResult:
    if already_composite:
        score = 0.0 if terrain_score is None else float(terrain_score)
        notes_en = (
            f"Mean of per-pixel composite on non-permanent land: "
            f"{W_TERRAIN:.2f}×terrain HAND/slope + {W_WATER:.2f}×JRC occurrence wetness. "
            "Permanent water pixels are not in this mean. "
            "Screening index only — not a hydraulic model, not a forecast, not an official floodplain."
        )
        notes_zh = (
            f"非永久水体陆地上的像元综合均值："
            f"{W_TERRAIN:.2f}×地形（HAND/坡度）+ {W_WATER:.2f}×JRC 出现频率湿润度。"
            "永久水体不进入该均值。"
            "仅为筛查指数 — 不是水动力模型、不是预报、不是官方洪泛区。"
        )
        src = "LIVE composite: HAND/slope + JRC occurrence, permanent water excluded"
        return LayerResult(
            name="inundation_susceptibility",
            score_0_100=round(score, 1),
            label=_label(score),
            notes_en=notes_en,
            notes_zh=notes_zh,
            data_source=src,
            is_mock=False,
        )
    if terrain_score is not None and hydro_score is None:
        score = float(terrain_score)
        src = "LIVE terrain only (surface-water layer unavailable and not mixed in)"
        notes_en = (
            "Composite equals the live terrain HAND/slope score. "
            "A failed or MOCK water layer was not blended in. Not a hydraulic model."
        )
        notes_zh = "综合分等于 live 地形 HAND/坡度得分。失败或 MOCK 的水文层未混入。不是水动力模型。"
    elif hydro_score is not None and terrain_score is None:
        score = float(hydro_score)
        src = "LIVE surface water only (terrain HAND unavailable and not mixed in)"
        notes_en = (
            "Composite equals the live JRC occurrence score on non-permanent land. "
            "Terrain was not blended in. Not a hydraulic model."
        )
        notes_zh = "综合分等于非永久水体陆地上的 JRC 出现频率得分。地形未混入。不是水动力模型。"
    else:
        score = W_TERRAIN * float(terrain_score or 0) + W_WATER * float(hydro_score or 0)
        src = "LIVE scalar blend"
        notes_en = "Scalar blend of live layer means. Not a hydraulic model."
        notes_zh = "live 图层均值的标量加权。不是水动力模型。"
    return LayerResult(
        name="inundation_susceptibility",
        score_0_100=round(score, 1),
        label=_label(score),
        notes_en=notes_en,
        notes_zh=notes_zh,
        data_source=src,
        is_mock=mixed_mock and terrain_score is None and hydro_score is None,
    )


def _score_from_scalars(terrain: LayerResult, hydro: LayerResult) -> LayerResult:
    """Used when HAND is unavailable. Mock layers do not enter the composite."""
    live = [ly for ly in (terrain, hydro) if not ly.is_mock]
    if not live:
        # Both mock: caller should have used _mock_overlay. Keep a defined result.
        return _mock_overlay_unspecified(terrain, hydro)
    if len(live) == 1:
        only = live[0]
        return _inundation_from_live(
            terrain_score=only.score_0_100 if only is terrain else None,
            hydro_score=only.score_0_100 if only is hydro else None,
            mixed_mock=True,
        )
    return _inundation_from_live(
        terrain_score=terrain.score_0_100,
        hydro_score=hydro.score_0_100,
        mixed_mock=False,
    )


def _mock_overlay_unspecified(terrain: LayerResult, hydro: LayerResult) -> LayerResult:
    score = 0.5 * terrain.score_0_100 + 0.5 * hydro.score_0_100
    return LayerResult(
        name="inundation_susceptibility",
        score_0_100=round(score, 1),
        label=_label(score),
        notes_en="Both live sources failed; composite is an average of MOCK fallbacks and is MOCK.",
        notes_zh="两个 live 数据源都失败了；综合分是 MOCK 回退的平均，标记为 MOCK。",
        data_source="MOCK (both live sources failed)",
        is_mock=True,
    )


def run_screening(
    aoi: AOI,
    mode: str = "demo",
    *,
    open_elevation_url: str | None = None,
    transfer_stats: TransferStats | None = None,
) -> ScreeningResult:
    """Run screening.

    demo/mock → all MOCK (offline).
    live → Copernicus HAND/slope and JRC occurrence, permanent water masked.
    """
    parameters = {
        "res_m": RES_M,
        "buffer_m": BUFFER_M,
        "stream_area_km2": STREAM_AREA_KM2,
        "hand_cap_m": HAND_CAP_M,
        "slope_cap_deg": SLOPE_CAP_DEG,
        "permanent_occurrence": PERMANENT_OCCURRENCE,
        "w_terrain": W_TERRAIN,
        "w_water": W_WATER,
        "w_hand": 0.8,
        "w_slope": 0.2,
    }
    if mode == "live":
        stats = transfer_stats if transfer_stats is not None else TransferStats()
        hydro, terrain, inund, ctx = _live_layers(aoi, open_elevation_url=open_elevation_url, stats=stats)
        any_mock = any(ly.is_mock for ly in (hydro, terrain, inund))
        used_mode = "live" if not any_mock else "live→partial-mock"
        disclaimer_en = (
            "DATA MODE: LIVE screening. Terrain is Copernicus DEM GLO-30 (a digital surface model) "
            "with HAND and slope. Surface water is JRC Global Surface Water occurrence. "
            f"Pixels with occurrence ≥ {PERMANENT_OCCURRENCE:.0f}% are permanent water and are NOT "
            "scored as flood-prone land. MOCK layers, if any, are labeled and are not mixed into the "
            "composite. This is not a hydraulic flood model, not a forecast, and not an official "
            "floodplain. Do not use for engineering, insurance, or regulatory decisions."
        )
        disclaimer_zh = (
            "数据模式：LIVE 筛查。地形为 Copernicus DEM GLO-30（数字表面模型）的 HAND 与坡度。"
            "地表水为 JRC 全球地表水出现频率。"
            f"出现频率 ≥ {PERMANENT_OCCURRENCE:.0f}% 的像素视为永久水体，不作为易涝陆地评分。"
            "若存在 MOCK 图层，会单独标注，且不混入综合分。"
            "这不是水动力洪水模型、不是预报、也不是官方洪泛区。"
            "不得用于工程、保险或合规决策。"
        )
        if ctx.get("dem_error"):
            disclaimer_en += f" DEM note: {ctx['dem_error']}"
            disclaimer_zh += f" DEM 说明：{ctx['dem_error']}"
        if ctx.get("water_error"):
            disclaimer_en += f" Water note: {ctx['water_error']}"
            disclaimer_zh += f" 水体说明：{ctx['water_error']}"
        result = ScreeningResult(
            aoi_bounds=aoi.bounds,
            aoi_centroid=aoi.centroid,
            aoi_area_km2=round(aoi.approx_area_km2(), 4),
            composite_score_0_100=inund.score_0_100,
            composite_label=inund.label,
            layers=[hydro, terrain, inund],
            mode=used_mode,
            disclaimer_en=disclaimer_en,
            disclaimer_zh=disclaimer_zh,
            elevation=ctx.get("elevation"),
            terrain_summary=ctx.get("terrain_summary"),
            water_summary=ctx.get("water_summary"),
            parameters=parameters,
            transfers={
                "http_get_bytes": stats.http_get_bytes,
                "largest_head_content_length": stats.http_head_bytes_reported,
            },
            rasters=ctx.get("rasters"),
        )
        return result

    hydro = _mock_hydrology_proxy(aoi)
    terrain = _mock_terrain_proxy(aoi)
    inund = _mock_overlay(aoi, hydro, terrain)
    return ScreeningResult(
        aoi_bounds=aoi.bounds,
        aoi_centroid=aoi.centroid,
        aoi_area_km2=round(aoi.approx_area_km2(), 4),
        composite_score_0_100=inund.score_0_100,
        composite_label=inund.label,
        layers=[hydro, terrain, inund],
        mode=mode,
        disclaimer_en=(
            "DATA MODE: MOCK / DEMO. Scores are deterministic heuristics from AOI "
            "geometry only. They are NOT live satellite or official floodplain products. "
            "Do not use for engineering, insurance, or regulatory decisions."
        ),
        disclaimer_zh=(
            "数据模式：模拟 / 演示。分数仅由 AOI 几何的确定性启发式产生，"
            "并非实时卫星或官方淹没区产品。不得用于工程、保险或合规决策。"
        ),
        elevation=None,
        parameters=parameters,
        transfers={"http_get_bytes": 0},
    )


def screening_to_geojson(aoi: AOI, result: ScreeningResult) -> dict:
    props = {
        "product": "geo-flood-agent",
        "version": __version_product__,
        "mode": result.mode,
        "composite_score_0_100": result.composite_score_0_100,
        "composite_label": result.composite_label,
        "area_km2_approx": result.aoi_area_km2,
        "parameters": result.parameters,
        "layers": [
            {
                "name": ly.name,
                "score_0_100": ly.score_0_100,
                "label": ly.label,
                "is_mock": ly.is_mock,
                "data_source": ly.data_source,
                "extras": ly.extras,
            }
            for ly in result.layers
        ],
        "disclaimer": result.disclaimer_en,
    }
    if result.terrain_summary:
        props["terrain_summary"] = result.terrain_summary
    if result.water_summary:
        props["water_summary"] = result.water_summary
    if result.transfers:
        props["transfers"] = result.transfers
    fc = aoi.to_feature_collection(props)
    ctx = result.geo_context
    if not ctx:
        return fc
    fc["features"][0]["properties"]["role"] = "screening_aoi"
    fc["features"][0]["properties"]["geoapify_note"] = (
        "Geoapify features in this collection are place and travel-time context. "
        "They are not flood depth."
    )
    fc["features"].extend(ctx.get("features") or [])
    return fc
