"""JRC Global Surface Water occurrence for an AOI.

Occurrence is the percent of valid Landsat observations classified as water
(Pekel et al. 2016), here the Planetary Computer COG mirror of JRC GSW
v1.3 (1984–2020). No API key. Assets are signed with the public Planetary
Computer SAS endpoint and read as windows.

Permanent water is occurrence >= PERMANENT_OCCURRENCE. Those pixels are a
mask, not flood-prone land.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

import numpy as np

from geo_flood_agent.aoi import AOI
from geo_flood_agent.cog import TransferStats, mosaic_reproject, public_url, wgs84_envelope
from geo_flood_agent.constants import PERMANENT_OCCURRENCE

STAC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"
USER_AGENT = "geo-flood-agent/0.3 (PhD portfolio demo; research)"


def _get_json(url: str, timeout: float = 60.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def occurrence_asset_hrefs(bounds: tuple[float, float, float, float]) -> list[dict[str, str]]:
    """Unsigned occurrence COG hrefs for every JRC GSW tile touching `bounds`."""
    west, south, east, north = bounds
    url = (
        STAC_SEARCH
        + "?collections=jrc-gsw&limit=20&bbox="
        + urllib.parse.quote(f"{west},{south},{east},{north}", safe=",")
    )
    data = _get_json(url)
    out: list[dict[str, str]] = []
    for feat in data.get("features") or []:
        assets = feat.get("assets") or {}
        occ = assets.get("occurrence") or {}
        href = occ.get("href")
        if not href:
            continue
        out.append({"item": str(feat.get("id")), "href": public_url(href)})
    if not out:
        raise RuntimeError(f"No JRC GSW occurrence tiles for bbox {bounds}")
    return out


def sign_href(href: str) -> str:
    """Sign a Planetary Computer blob href. The signed URL is not persisted."""
    url = SIGN_URL + "?href=" + urllib.parse.quote(href, safe="")
    data = _get_json(url)
    signed = data.get("href")
    if not signed:
        raise RuntimeError("Planetary Computer SAS response had no href")
    return signed


def fetch_occurrence(
    aoi: AOI,
    grid: dict,
    *,
    permanent_threshold: float = PERMANENT_OCCURRENCE,
    stats: TransferStats | None = None,
) -> dict[str, Any]:
    """Occurrence (0–100) and permanent-water mask on an existing analysis grid."""
    envelope = wgs84_envelope(grid)
    # Pad the search box slightly so a tile edge is not missed.
    pad = 0.01
    search_box = (
        envelope[0] - pad,
        envelope[1] - pad,
        envelope[2] + pad,
        envelope[3] + pad,
    )
    items = occurrence_asset_hrefs(search_box)
    signed = [sign_href(item["href"]) for item in items]
    occ, used = mosaic_reproject(
        signed,
        grid,
        resampling="nearest",
        src_nodata=255,
        dst_nodata=-1,
        stats=stats,
        above_is_nodata=101,  # 255 already removed; keep 0–100
    )
    # Nearest sampling of uint-like values can land just off integers.
    occ = np.where(np.isfinite(occ), np.clip(np.rint(occ), 0, 100), np.nan).astype(np.float32)
    permanent = np.isfinite(occ) & (occ >= permanent_threshold)
    seasonal = np.isfinite(occ) & (occ > 0) & (occ < permanent_threshold)
    return {
        "occurrence": occ,
        "permanent": permanent,
        "seasonal": seasonal,
        "sources": [item["href"] for item in items],
        "source_items": [item["item"] for item in items],
        "windows_read": used,
        "permanent_threshold": permanent_threshold,
        "product": "JRC Global Surface Water occurrence v1.3 (1984–2020), Planetary Computer COG",
    }
