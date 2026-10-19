"""Open elevation fetchers (no paid API keys).

Primary: OpenTopoData SRTM90m (https://www.opentopodata.org/)
Fallback: Open-Elevation public API

Results are REAL public DEM samples at sparse grid points — not a full DEM
mosaic, not flood depth, not official floodplain. Always labeled as such.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from geo_flood_agent.aoi import AOI

DEFAULT_OPENTOPODATA = "https://api.opentopodata.org/v1/srtm90m"
DEFAULT_OPEN_ELEVATION = "https://api.open-elevation.com/api/v1/lookup"


@dataclass
class ElevationStats:
    """Sparse DEM sample statistics over an AOI."""

    provider: str
    n_points: int
    elev_min_m: float | None
    elev_max_m: float | None
    elev_mean_m: float | None
    elev_std_m: float | None
    relief_m: float | None  # max - min
    low_share_lt_20m: float | None  # fraction of samples < 20 m
    samples: list[dict[str, float]]  # [{lat, lon, elevation}, ...]
    ok: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def _grid_points(aoi: AOI, n: int = 5) -> list[tuple[float, float]]:
    """n x n lon/lat samples inside AOI bounds (inclusive endpoints)."""
    minx, miny, maxx, maxy = aoi.bounds
    if n < 2:
        n = 2
    lons = [minx + i * (maxx - minx) / (n - 1) for i in range(n)]
    lats = [miny + j * (maxy - miny) / (n - 1) for j in range(n)]
    pts: list[tuple[float, float]] = []
    for lat in lats:
        for lon in lons:
            pts.append((lat, lon))
    return pts


def _http_get_json(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "geo-flood-agent/0.2 (PhD portfolio demo; research)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_opentopodata(
    points: list[tuple[float, float]],
    base_url: str = DEFAULT_OPENTOPODATA,
) -> list[dict[str, float]]:
    # OpenTopoData: locations=lat,lng|lat,lng (max ~100)
    locs = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in points)
    url = f"{base_url}?locations={urllib.parse.quote(locs, safe='|,.')}"
    data = _http_get_json(url)
    if data.get("status") != "OK":
        raise RuntimeError(f"OpenTopoData status={data.get('status')}")
    out: list[dict[str, float]] = []
    for row in data.get("results") or []:
        elev = row.get("elevation")
        loc = row.get("location") or {}
        if elev is None:
            continue
        out.append(
            {
                "lat": float(loc.get("lat", 0.0)),
                "lon": float(loc.get("lng", 0.0)),
                "elevation": float(elev),
            }
        )
    return out


def fetch_open_elevation(
    points: list[tuple[float, float]],
    base_url: str = DEFAULT_OPEN_ELEVATION,
) -> list[dict[str, float]]:
    locs = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in points)
    url = f"{base_url}?locations={urllib.parse.quote(locs, safe='|,.')}"
    data = _http_get_json(url)
    out: list[dict[str, float]] = []
    for row in data.get("results") or []:
        elev = row.get("elevation")
        if elev is None:
            continue
        out.append(
            {
                "lat": float(row.get("latitude", 0.0)),
                "lon": float(row.get("longitude", 0.0)),
                "elevation": float(elev),
            }
        )
    return out


def _stats_from_samples(provider: str, samples: list[dict[str, float]]) -> ElevationStats:
    if not samples:
        return ElevationStats(
            provider=provider,
            n_points=0,
            elev_min_m=None,
            elev_max_m=None,
            elev_mean_m=None,
            elev_std_m=None,
            relief_m=None,
            low_share_lt_20m=None,
            samples=[],
            ok=False,
            error="No elevation samples returned",
        )
    elevs = [s["elevation"] for s in samples]
    mean = sum(elevs) / len(elevs)
    var = sum((e - mean) ** 2 for e in elevs) / len(elevs)
    std = math.sqrt(var)
    low = sum(1 for e in elevs if e < 20.0) / len(elevs)
    return ElevationStats(
        provider=provider,
        n_points=len(elevs),
        elev_min_m=round(min(elevs), 2),
        elev_max_m=round(max(elevs), 2),
        elev_mean_m=round(mean, 2),
        elev_std_m=round(std, 2),
        relief_m=round(max(elevs) - min(elevs), 2),
        low_share_lt_20m=round(low, 3),
        samples=samples,
        ok=True,
        error=None,
    )


def fetch_elevation_stats(
    aoi: AOI,
    *,
    grid_n: int = 5,
    preferred: str = "opentopodata",
    open_elevation_url: str | None = None,
) -> ElevationStats:
    """Fetch sparse DEM samples for AOI. Tries preferred provider then fallback."""
    points = _grid_points(aoi, n=grid_n)
    errors: list[str] = []

    order = ["opentopodata", "open-elevation"]
    if preferred == "open-elevation":
        order = ["open-elevation", "opentopodata"]

    for name in order:
        try:
            if name == "opentopodata":
                samples = fetch_opentopodata(points)
                return _stats_from_samples("OpenTopoData SRTM90m", samples)
            url = open_elevation_url or DEFAULT_OPEN_ELEVATION
            samples = fetch_open_elevation(points, base_url=url)
            return _stats_from_samples("Open-Elevation", samples)
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError, OSError) as e:
            errors.append(f"{name}: {e}")
            continue

    return ElevationStats(
        provider="none",
        n_points=0,
        elev_min_m=None,
        elev_max_m=None,
        elev_mean_m=None,
        elev_std_m=None,
        relief_m=None,
        low_share_lt_20m=None,
        samples=[],
        ok=False,
        error="; ".join(errors) or "unknown elevation fetch failure",
    )


def elevation_to_terrain_score(stats: ElevationStats) -> tuple[float, str, str]:
    """Map elevation stats → inundation-concern score (0–100, higher = more concern).

    Heuristic (screening only):
    - Low mean elevation + high share <20 m → higher concern
    - Very high relief → somewhat lower ponding concern (drainage), but not zero
    """
    if not stats.ok or stats.elev_mean_m is None:
        return 50.0, "Elevation fetch failed; no score from DEM.", "高程拉取失败，无 DEM 得分。"

    mean = stats.elev_mean_m
    low_share = stats.low_share_lt_20m or 0.0
    relief = stats.relief_m or 0.0

    # Lowland pressure: mean elev mapped so ~0 m → high, ~200 m → low
    lowland = max(0.0, min(1.0, 1.0 - mean / 200.0))
    flat_penalty = max(0.0, min(1.0, 1.0 - relief / 100.0))  # flatter → higher
    score = 15 + 45 * lowland + 25 * low_share + 15 * flat_penalty
    score = max(0.0, min(100.0, score))

    notes_en = (
        f"REAL open DEM samples ({stats.provider}): "
        f"n={stats.n_points}, mean={stats.elev_mean_m} m, "
        f"min={stats.elev_min_m} m, max={stats.elev_max_m} m, "
        f"relief={stats.relief_m} m, share<20m={stats.low_share_lt_20m}. "
        "Sparse grid only — NOT a flood model or official floodplain."
    )
    notes_zh = (
        f"真实开放 DEM 采样（{stats.provider}）："
        f"n={stats.n_points}，均值={stats.elev_mean_m} m，"
        f"最小={stats.elev_min_m} m，最大={stats.elev_max_m} m，"
        f"高差={stats.relief_m} m，<20m 占比={stats.low_share_lt_20m}。"
        "仅为稀疏格网采样 — 不是洪水模型，也不是官方淹没区。"
    )
    return round(score, 1), notes_en, notes_zh
