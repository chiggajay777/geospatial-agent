"""COG window helpers, UTM analysis grids, and HTTP transfer accounting.

GDAL curl verbose logs are parsed only to count bytes, then discarded.
Signed URLs must not be written into reports.
"""

from __future__ import annotations

import math
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

import numpy as np

_CONTENT_LENGTH = re.compile(r"Content-Length:\s*(\d+)", re.I)


def configure_gdal_env() -> None:
    """Defaults for range-reading remote COGs. Safe to call more than once."""
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.tiff,.TIF")
    os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
    os.environ.setdefault("GDAL_HTTP_MULTIPLEX", "YES")
    os.environ.setdefault("GDAL_INGESTED_BYTES_AT_OPEN", "32768")
    os.environ.setdefault("VSI_CACHE", "TRUE")
    os.environ.setdefault("GDAL_HTTP_TIMEOUT", "90")
    os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "3")
    os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "1")
    os.environ.setdefault("GDAL_HTTP_UNSAFESSL", "NO")


def gdal_env_kwargs() -> dict[str, str]:
    configure_gdal_env()
    return {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff,.TIF",
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "GDAL_HTTP_MULTIPLEX": "YES",
        "GDAL_INGESTED_BYTES_AT_OPEN": "32768",
        "VSI_CACHE": "TRUE",
        "GDAL_HTTP_TIMEOUT": os.environ.get("GDAL_HTTP_TIMEOUT", "90"),
        "GDAL_HTTP_MAX_RETRY": "3",
        "GDAL_HTTP_RETRY_DELAY": "1",
        "CPL_VSIL_CURL_USE_HEAD": "YES",
    }


@dataclass
class TransferStats:
    """Bytes actually returned by HTTP GET bodies (range responses included)."""

    http_get_bytes: int = 0
    http_head_bytes_reported: int = 0
    notes: list[str] = field(default_factory=list)

    def add_log(self, text: str) -> None:
        method = None
        for line in text.splitlines():
            if "> HEAD " in line:
                method = "HEAD"
            elif "> GET " in line:
                method = "GET"
            else:
                m = _CONTENT_LENGTH.search(line)
                if not m or method is None:
                    continue
                n = int(m.group(1))
                if method == "GET":
                    self.http_get_bytes += n
                elif method == "HEAD":
                    # Object size, not bytes transferred. Keep the max seen.
                    if n > self.http_head_bytes_reported:
                        self.http_head_bytes_reported = n


@contextmanager
def _capture_fd_stderr() -> Iterator[tempfile._TemporaryFileWrapper]:
    fd = sys.stderr.fileno()
    old = os.dup(fd)
    tmp = tempfile.TemporaryFile(mode="w+b")
    try:
        sys.stderr.flush()
        os.dup2(tmp.fileno(), fd)
        yield tmp
    finally:
        sys.stderr.flush()
        os.dup2(old, fd)
        os.close(old)


@contextmanager
def measured_gdal_env(stats: TransferStats | None = None):
    """rasterio.Env that optionally counts curl GET bytes via a discarded verbose log."""
    import rasterio

    kwargs = gdal_env_kwargs()
    if stats is not None:
        kwargs["CPL_CURL_VERBOSE"] = "YES"
    with rasterio.Env(**kwargs):
        if stats is None:
            yield
            return
        with _capture_fd_stderr() as tmp:
            yield
        tmp.seek(0)
        raw = tmp.read().decode("utf-8", errors="replace")
        tmp.close()
        stats.add_log(raw)


def vsicurl(url: str) -> str:
    if url.startswith("/vsi"):
        return url
    return "/vsicurl/" + url


def public_url(url: str) -> str:
    """Drop query strings (SAS tokens) before anything is saved."""
    return url.split("?", 1)[0]


def utm_epsg(lon: float, lat: float) -> int:
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    zone = min(60, max(1, zone))
    return (32600 if lat >= 0 else 32700) + zone


def analysis_grid(
    bounds: tuple[float, float, float, float],
    buffer_m: float,
    res_m: float,
    max_side: int,
) -> dict:
    """Square metric grid covering the AOI plus a metre buffer, in the local UTM zone."""
    from rasterio.transform import from_origin
    from rasterio.warp import transform as warp_xy

    minx, miny, maxx, maxy = bounds
    lon_c = (minx + maxx) / 2.0
    lat_c = (miny + maxy) / 2.0
    epsg = utm_epsg(lon_c, lat_c)
    crs = f"EPSG:{epsg}"
    xs, ys = warp_xy(
        "EPSG:4326",
        crs,
        [minx, maxx, minx, maxx],
        [miny, miny, maxy, maxy],
    )
    left = min(xs) - buffer_m
    right = max(xs) + buffer_m
    bottom = min(ys) - buffer_m
    top = max(ys) + buffer_m
    width_m = right - left
    height_m = top - bottom
    res = float(res_m)
    side = max(width_m, height_m)
    while side / res > max_side:
        res *= 2.0
    left = math.floor(left / res) * res
    bottom = math.floor(bottom / res) * res
    right = math.ceil(right / res) * res
    top = math.ceil(top / res) * res
    width = max(2, int(round((right - left) / res)))
    height = max(2, int(round((top - bottom) / res)))
    transform = from_origin(left, top, res, res)
    return {
        "crs": crs,
        "epsg": epsg,
        "transform": transform,
        "width": width,
        "height": height,
        "res_m": res,
        "bounds_wgs84": bounds,
    }


def iter_degree_tiles(west: float, south: float, east: float, north: float) -> list[tuple[int, int]]:
    """1° tiles as (lat_sw, lon_sw) covering a WGS84 box. Name is the SW corner."""
    lon0 = math.floor(west)
    lon1 = math.floor(east - 1e-9)
    lat0 = math.floor(south)
    lat1 = math.floor(north - 1e-9)
    out = []
    for lat in range(int(lat0), int(lat1) + 1):
        for lon in range(int(lon0), int(lon1) + 1):
            out.append((lat, lon))
    return out


def copernicus_tile_name(lat_sw: int, lon_sw: int) -> str:
    ns = "N" if lat_sw >= 0 else "S"
    ew = "E" if lon_sw >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat_sw):02d}_00_{ew}{abs(lon_sw):03d}_00_DEM"


def copernicus_tile_url(lat_sw: int, lon_sw: int) -> str:
    name = copernicus_tile_name(lat_sw, lon_sw)
    return (
        "https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com/"
        f"{name}/{name}.tif"
    )


def wgs84_envelope(grid: dict, pad_m: float = 0.0) -> tuple[float, float, float, float]:
    from rasterio.transform import array_bounds
    from rasterio.warp import transform_bounds

    height = grid["height"]
    width = grid["width"]
    left, bottom, right, top = array_bounds(height, width, grid["transform"])
    if pad_m:
        left -= pad_m
        bottom -= pad_m
        right += pad_m
        top += pad_m
    return transform_bounds(grid["crs"], "EPSG:4326", left, bottom, right, top, densify_pts=21)


def intersect_bounds(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> tuple[float, float, float, float] | None:
    west = max(a[0], b[0])
    south = max(a[1], b[1])
    east = min(a[2], b[2])
    north = min(a[3], b[3])
    if east <= west or north <= south:
        return None
    return west, south, east, north


def read_window_reproject(
    url: str,
    grid: dict,
    *,
    resampling: str,
    src_nodata: float | None,
    dst_nodata: float,
    stats: TransferStats | None = None,
    band: int = 1,
    above_is_nodata: float | None = None,
    below_is_nodata: float | None = None,
) -> np.ndarray:
    """Read the remote raster window that covers `grid` and reproject onto it.

    Returns float32 with NaN outside valid data. A COG is read with HTTP range
    requests (the full source tile is not downloaded).
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import reproject
    from rasterio.windows import from_bounds

    resampling_enum = Resampling.bilinear if resampling == "bilinear" else Resampling.nearest
    empty = np.full((grid["height"], grid["width"]), np.nan, dtype=np.float32)
    envelope = wgs84_envelope(grid, pad_m=grid["res_m"])
    sentinel = -3.4028234e38  # outside any elevation or occurrence value we keep
    with measured_gdal_env(stats):
        with rasterio.open(vsicurl(url)) as src:
            src_bounds = (src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top)
            box = intersect_bounds(envelope, src_bounds)
            if box is None:
                return empty
            window = from_bounds(*box, transform=src.transform).round_offsets().round_lengths()
            window = window.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
            if window.width <= 1 or window.height <= 1:
                return empty
            data = src.read(band, window=window).astype(np.float32)
            if src.nodata is not None:
                data = np.where(data == src.nodata, np.nan, data)
            if src_nodata is not None:
                data = np.where(data == src_nodata, np.nan, data)
            if above_is_nodata is not None:
                data = np.where(data >= above_is_nodata, np.nan, data)
            if below_is_nodata is not None:
                data = np.where(data <= below_is_nodata, np.nan, data)
            data = np.where(np.isfinite(data), data, sentinel).astype(np.float32)
            src_transform = src.window_transform(window)
            tmp = np.full((grid["height"], grid["width"]), sentinel, dtype=np.float32)
            reproject(
                source=data,
                destination=tmp,
                src_transform=src_transform,
                src_crs=src.crs,
                src_nodata=sentinel,
                dst_transform=grid["transform"],
                dst_crs=grid["crs"],
                dst_nodata=sentinel,
                resampling=resampling_enum,
            )
    out = tmp.astype(np.float32)
    out[~np.isfinite(out) | (out <= sentinel / 10.0)] = np.nan
    return out


def mosaic_reproject(
    urls: list[str],
    grid: dict,
    *,
    resampling: str,
    src_nodata: float | None,
    dst_nodata: float,
    stats: TransferStats | None = None,
    above_is_nodata: float | None = None,
    below_is_nodata: float | None = None,
) -> tuple[np.ndarray, list[str]]:
    """Mosaic several COGs onto `grid`. Later tiles fill only cells still empty."""
    acc = np.full((grid["height"], grid["width"]), np.nan, dtype=np.float32)
    used: list[str] = []
    errors: list[str] = []
    for url in urls:
        try:
            piece = read_window_reproject(
                url,
                grid,
                resampling=resampling,
                src_nodata=src_nodata,
                dst_nodata=dst_nodata,
                stats=stats,
                above_is_nodata=above_is_nodata,
                below_is_nodata=below_is_nodata,
            )
        except Exception as exc:  # noqa: BLE001 — remote tiles 404 / network
            errors.append(f"{public_url(url)}: {type(exc).__name__}: {exc}")
            continue
        if not np.isfinite(piece).any():
            continue
        fill = ~np.isfinite(acc) & np.isfinite(piece)
        acc[fill] = piece[fill]
        used.append(public_url(url))
    if not used and errors:
        raise RuntimeError("; ".join(errors[:4]))
    return acc, used


def rasterize_aoi(geometry, grid: dict) -> np.ndarray:
    """Boolean mask, True inside the AOI, on the analysis grid."""
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_geom
    from shapely.geometry import mapping

    geom = transform_geom("EPSG:4326", grid["crs"], mapping(geometry))
    mask = geometry_mask(
        [geom],
        out_shape=(grid["height"], grid["width"]),
        transform=grid["transform"],
        invert=True,
        all_touched=False,
    )
    return mask
