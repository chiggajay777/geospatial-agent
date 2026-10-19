"""Copernicus DEM GLO-30 window → HAND and slope on a metric grid.

Source: public AWS bucket `copernicus-dem-30m` (eu-central-1), Cloud Optimized
GeoTIFF, no AWS credentials. The product is a digital surface model (includes
canopy and buildings), not bare earth and not a flood depth.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from geo_flood_agent.aoi import AOI
from geo_flood_agent.cog import (
    TransferStats,
    analysis_grid,
    copernicus_tile_url,
    iter_degree_tiles,
    mosaic_reproject,
    rasterize_aoi,
    wgs84_envelope,
)
from geo_flood_agent.constants import (
    BUFFER_M,
    DEM_NODATA_ABOVE_M,
    DEM_NODATA_BELOW_M,
    MAX_GRID_SIDE,
    RES_M,
    STREAM_AREA_KM2,
)
from geo_flood_agent.hydrology import terrain_concern, terrain_derivatives


def fetch_dem_grid(
    aoi: AOI,
    *,
    buffer_m: float = BUFFER_M,
    res_m: float = RES_M,
    stream_area_km2: float = STREAM_AREA_KM2,
    stats: TransferStats | None = None,
) -> dict[str, Any]:
    """Return projected DEM, HAND, slope, concern, and the AOI mask.

    The hydrological conditioning uses the buffered grid so flow is not cut off
    at the AOI edge. Scores later use only AOI pixels.
    """
    grid = analysis_grid(aoi.bounds, buffer_m=buffer_m, res_m=res_m, max_side=MAX_GRID_SIDE)
    envelope = wgs84_envelope(grid)
    tiles = iter_degree_tiles(*envelope)
    urls = [copernicus_tile_url(lat, lon) for lat, lon in tiles]
    dem, used = mosaic_reproject(
        urls,
        grid,
        resampling="bilinear",
        src_nodata=None,
        dst_nodata=-9999.0,
        stats=stats,
        below_is_nodata=DEM_NODATA_BELOW_M,
        above_is_nodata=DEM_NODATA_ABOVE_M,
    )
    if not np.isfinite(dem).any():
        raise RuntimeError(
            "Copernicus GLO-30 window was empty "
            f"(tiles tried: {len(urls)}). Ocean tiles are not published."
        )
    deriv = terrain_derivatives(dem, cell_m=float(grid["res_m"]), stream_area_km2=stream_area_km2)
    concern = terrain_concern(deriv["hand"], deriv["slope"])
    inside = rasterize_aoi(aoi.geometry, grid)
    return {
        "grid": {
            "crs": grid["crs"],
            "epsg": grid["epsg"],
            "res_m": grid["res_m"],
            "width": grid["width"],
            "height": grid["height"],
            "transform": tuple(grid["transform"])[:6],
        },
        "_grid": grid,
        "dem": dem,
        "hand": deriv["hand"],
        "slope": deriv["slope"],
        "stream": deriv["stream"],
        "concern": concern,
        "inside": inside,
        "valid": deriv["valid"],
        "sources": used,
        "buffer_m": buffer_m,
        "stream_area_km2": stream_area_km2,
        "tile_count_attempted": len(urls),
    }
