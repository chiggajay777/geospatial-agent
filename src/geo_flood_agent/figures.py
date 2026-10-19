"""Portfolio figures for a screening run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np


def _mpl():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _extent_lonlat(grid: dict) -> tuple[float, float, float, float]:
    from rasterio.transform import Affine, array_bounds
    from rasterio.warp import transform as warp_xy

    aff = Affine(*grid["transform"])
    left, bottom, right, top = array_bounds(grid["height"], grid["width"], aff)
    xs, ys = warp_xy(grid["crs"], "EPSG:4326", [left, right], [bottom, top])
    return min(xs), max(xs), min(ys), max(ys)


def _save(fig, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    import matplotlib.pyplot as plt

    plt.close(fig)


def write_screening_figures(result: Any, out_dir: Path) -> list[Path]:
    """Bar chart plus, when live rasters exist, HAND, occurrence, and susceptibility maps."""
    try:
        plt = _mpl()
    except ImportError:
        return []

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    names = [ly.name for ly in result.layers]
    scores = [ly.score_0_100 for ly in result.layers]
    colors = ["#f4a261" if ly.is_mock else "#2a9d8f" for ly in result.layers]
    fig, ax = plt.subplots(figsize=(8.2, 4.2))
    bars = ax.barh(names, scores, color=colors)
    ax.set_xlim(0, 100)
    ax.set_xlabel("Concern score (0–100, higher = more concern)")
    ax.set_title(f"geo-flood-agent layer scores — mode={result.mode}")
    for x in (25, 50, 75):
        ax.axvline(x, color="#888", ls="--", lw=0.8)
    for bar, sc, ly in zip(bars, scores, result.layers):
        tag = "MOCK" if ly.is_mock else "LIVE"
        ax.text(min(sc + 1.5, 92), bar.get_y() + bar.get_height() / 2, f"{sc} ({tag})", va="center", fontsize=9)
    p1 = out_dir / "layer_scores.png"
    _save(fig, p1)
    paths.append(p1)

    rasters = getattr(result, "rasters", None) or {}
    if rasters.get("hand") is not None and np.isfinite(rasters["hand"]).any():
        paths.extend(_map_figures(result, rasters, out_dir))
    elif (result.elevation or {}).get("samples"):
        paths.append(_sparse_samples(result, out_dir))
    return paths


def _base_mask(arr: np.ndarray, inside: np.ndarray) -> np.ma.MaskedArray:
    data = np.array(arr, dtype=np.float64)
    mask = (~inside) | ~np.isfinite(data)
    return np.ma.array(data, mask=mask)


def _map_figures(result: Any, rasters: dict, out_dir: Path) -> list[Path]:
    plt = _mpl()
    grid = rasters["grid"]
    west, east, south, north = _extent_lonlat(grid)
    extent = (west, east, south, north)
    inside = np.asarray(rasters["inside"], dtype=bool)
    permanent = np.asarray(rasters.get("permanent"), dtype=bool)
    paths: list[Path] = []

    def panel(arr, title, cmap, path, vmin=None, vmax=None, cbar=""):
        fig, ax = plt.subplots(figsize=(6.4, 5.4))
        show = _base_mask(arr, inside)
        im = ax.imshow(show, extent=extent, origin="upper", cmap=cmap, vmin=vmin, vmax=vmax)
        if permanent is not None and permanent.any():
            perm = np.ma.array(np.ones(permanent.shape), mask=~(permanent & inside))
            ax.imshow(perm, extent=extent, origin="upper", cmap="Blues", alpha=0.55, vmin=0, vmax=1)
        cb = fig.colorbar(im, ax=ax, shrink=0.82)
        if cbar:
            cb.set_label(cbar)
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        ax.set_title(title)
        _save(fig, path)
        return path

    elev = rasters["dem"]
    finite = elev[inside & np.isfinite(elev)]
    if finite.size:
        vmin, vmax = np.percentile(finite, [2, 98])
        paths.append(
            panel(
                elev,
                "Elevation (Copernicus GLO-30 DSM)\nblue = permanent water, excluded from the score",
                "terrain",
                out_dir / "dem_elevation.png",
                vmin=float(vmin),
                vmax=float(vmax),
                cbar="Elevation (m)",
            )
        )
    hand = rasters["hand"]
    paths.append(
        panel(
            hand,
            "HAND (m) on non-permanent land\nblue = permanent water (not scored)",
            "viridis_r",
            out_dir / "hand.png",
            vmin=0,
            vmax=15,
            cbar="HAND (m)",
        )
    )
    paths.append(
        panel(
            rasters["slope"],
            "Slope (degrees)",
            "cividis",
            out_dir / "slope.png",
            vmin=0,
            vmax=15,
            cbar="Slope (degrees)",
        )
    )
    if rasters.get("occurrence") is not None:
        paths.append(
            panel(
                rasters["occurrence"],
                "JRC surface-water occurrence (%)\nblue overlay = permanent (≥ threshold), masked",
                "YlGnBu",
                out_dir / "occurrence.png",
                vmin=0,
                vmax=100,
                cbar="Occurrence (%)",
            )
        )
    comp = rasters.get("composite")
    if comp is None:
        comp = rasters.get("concern")
    paths.append(
        panel(
            comp,
            f"Susceptibility on land (mode={result.mode})\npermanent water shown blue and excluded",
            "YlOrRd",
            out_dir / "susceptibility.png",
            vmin=0,
            vmax=100,
            cbar="Concern (0–100)",
        )
    )
    return paths


def _sparse_samples(result: Any, out_dir: Path) -> Path:
    plt = _mpl()
    elev = result.elevation or {}
    samples = elev.get("samples") or []
    lons = [s["lon"] for s in samples]
    lats = [s["lat"] for s in samples]
    zs = [s["elevation"] for s in samples]
    fig, ax = plt.subplots(figsize=(5.5, 5))
    sc = ax.scatter(lons, lats, c=zs, cmap="terrain", s=80, edgecolors="k", linewidths=0.3)
    cb = fig.colorbar(sc, ax=ax, shrink=0.8)
    cb.set_label("Elevation (m)")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title(f"Sparse DEM samples — {elev.get('provider', '')}")
    minx, miny, maxx, maxy = result.aoi_bounds
    ax.plot([minx, maxx, maxx, minx, minx], [miny, miny, maxy, maxy, miny], "r-", lw=1)
    ax.set_aspect("equal", adjustable="box")
    path = out_dir / "dem_samples.png"
    _save(fig, path)
    return path
