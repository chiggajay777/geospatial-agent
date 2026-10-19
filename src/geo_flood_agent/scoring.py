"""Land-only summaries. Permanent water is excluded from every score."""

from __future__ import annotations

from typing import Any

import numpy as np

from geo_flood_agent.constants import (
    HAND_CAP_M,
    PERMANENT_OCCURRENCE,
    SLOPE_CAP_DEG,
    W_HAND,
    W_SLOPE,
    W_TERRAIN,
    W_WATER,
)


def _finite_mean(values: np.ndarray) -> float | None:
    if values.size == 0 or not np.isfinite(values).any():
        return None
    return float(np.nanmean(values))


def _pct(part: int, whole: int) -> float | None:
    if whole <= 0:
        return None
    return float(100.0 * part / whole)


def hydro_concern(occurrence: np.ndarray, permanent: np.ndarray, threshold: float) -> np.ndarray:
    """Historical wetness of non-permanent pixels, 0–100. Permanent and nodata are NaN."""
    score = np.clip(occurrence / threshold, 0.0, 1.0) * 100.0
    score = score.astype(np.float64)
    score[permanent | ~np.isfinite(occurrence)] = np.nan
    return score


def summarize_aoi(
    *,
    dem: np.ndarray,
    hand: np.ndarray,
    slope: np.ndarray,
    terrain: np.ndarray,
    inside: np.ndarray,
    occurrence: np.ndarray | None,
    permanent: np.ndarray | None,
    res_m: float,
    permanent_threshold: float = PERMANENT_OCCURRENCE,
) -> dict[str, Any]:
    """Pixel summaries inside the AOI. Scores use land pixels only."""
    inside = inside.astype(bool)
    n_aoi = int(inside.sum())
    cell_km2 = (res_m * res_m) / 1e6
    if permanent is None:
        permanent = np.zeros(inside.shape, dtype=bool)
    else:
        permanent = permanent.astype(bool)
    perm = inside & permanent
    # Land for scoring: inside AOI, not permanent water, HAND resolved.
    land = inside & ~permanent & np.isfinite(hand) & np.isfinite(slope)
    unresolved = inside & ~permanent & ~np.isfinite(hand)
    water_known = None
    if occurrence is not None:
        water_known = inside & np.isfinite(occurrence)

    def _stats(arr: np.ndarray, mask: np.ndarray) -> dict[str, float | None]:
        vals = arr[mask]
        vals = vals[np.isfinite(vals)]
        if vals.size == 0:
            return {"n": 0, "mean": None, "median": None, "p10": None, "p90": None}
        return {
            "n": int(vals.size),
            "mean": round(float(np.mean(vals)), 3),
            "median": round(float(np.median(vals)), 3),
            "p10": round(float(np.percentile(vals, 10)), 3),
            "p90": round(float(np.percentile(vals, 90)), 3),
        }

    hand_land = _stats(hand, land)
    slope_land = _stats(slope, land)
    elev_aoi = _stats(dem, inside & np.isfinite(dem))
    terrain_land = _stats(terrain, land & np.isfinite(terrain))

    low_hand = inside & np.isfinite(hand) & (hand < 2.0)
    low_hand_n = int(low_hand.sum())
    low_hand_perm_n = int((low_hand & permanent).sum())

    hydro = None
    hydro_stats = None
    composite = None
    composite_stats = None
    if occurrence is not None:
        hydro = hydro_concern(occurrence, permanent, permanent_threshold)
        hydro_stats = _stats(hydro, land & np.isfinite(hydro))
        # Where occurrence is missing, composite falls back to terrain only on that pixel.
        comp = np.full(terrain.shape, np.nan, dtype=np.float64)
        both = land & np.isfinite(terrain) & np.isfinite(hydro)
        terrain_only = land & np.isfinite(terrain) & ~np.isfinite(hydro)
        comp[both] = W_TERRAIN * terrain[both] + W_WATER * hydro[both]
        comp[terrain_only] = terrain[terrain_only]
        composite = comp
        composite_stats = _stats(comp, land & np.isfinite(comp))

    seasonal_n = 0
    if occurrence is not None:
        seasonal_n = int((land & np.isfinite(occurrence) & (occurrence > 0) & (occurrence < permanent_threshold)).sum())

    return {
        "res_m": res_m,
        "cell_km2": cell_km2,
        "aoi_pixels": n_aoi,
        "aoi_km2_raster": round(n_aoi * cell_km2, 4),
        "permanent_water_pixels": int(perm.sum()),
        "permanent_water_km2": round(int(perm.sum()) * cell_km2, 4),
        "permanent_water_percent_of_aoi": None if _pct(int(perm.sum()), n_aoi) is None else round(_pct(int(perm.sum()), n_aoi), 2),
        "land_pixels_scored": int(land.sum()),
        "land_km2_scored": round(int(land.sum()) * cell_km2, 4),
        "hand_unresolved_land_pixels": int(unresolved.sum()),
        "hand_unresolved_land_percent": None
        if _pct(int(unresolved.sum()), int(unresolved.sum()) + int(land.sum())) is None
        else round(_pct(int(unresolved.sum()), int(unresolved.sum()) + int(land.sum())), 2),
        "elevation_aoi": elev_aoi,
        "hand_land_m": hand_land,
        "slope_land_deg": slope_land,
        "terrain_score_land": terrain_land,
        "hand_lt_2m_aoi_pixels": low_hand_n,
        "hand_lt_2m_permanent_pixels": low_hand_perm_n,
        "hand_lt_2m_permanent_percent": None if _pct(low_hand_perm_n, low_hand_n) is None else round(_pct(low_hand_perm_n, low_hand_n), 2),
        "hand_lt_2m_land_percent": None
        if hand_land["n"] == 0
        else round(100.0 * int((land & (hand < 2.0)).sum()) / int(land.sum()), 2),
        "seasonal_water_land_pixels": seasonal_n,
        "hydro_score_land": hydro_stats,
        "composite_score_land": composite_stats,
        "parameters": {
            "hand_cap_m": HAND_CAP_M,
            "slope_cap_deg": SLOPE_CAP_DEG,
            "w_hand": W_HAND,
            "w_slope": W_SLOPE,
            "permanent_occurrence": permanent_threshold,
            "w_terrain": W_TERRAIN,
            "w_water": W_WATER,
        },
        "_hydro": hydro,
        "_composite": composite,
        "_land": land,
        "_permanent": perm,
    }
