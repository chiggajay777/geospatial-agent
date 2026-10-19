"""D8 terrain derivatives used for flood screening.

Algorithms, not a hydraulic model:

- Depression filling: priority-flood with epsilon flat resolution
  (Barnes, Lehman, Mulla 2014).
- Flow direction and accumulation: D8 steepest descent
  (O'Callaghan and Mark 1984).
- HAND: height above the nearest downstream drainage cell
  (Nobre et al. 2011). Negative values (pits below the channel) are clipped to 0.
- Slope: gradient of the original DEM, in degrees.

Implemented with NumPy so the core path stays pip-installable on a CPU Mac
(rasterio + numpy). pysheds, whitebox, and richdem wrap the same family of
algorithms but pull heavier native stacks; this module is unit-tested on a
synthetic valley instead.
"""

from __future__ import annotations

import heapq
import math

import numpy as np

from geo_flood_agent.constants import (
    HAND_CAP_M,
    SLOPE_CAP_DEG,
    W_HAND,
    W_SLOPE,
)

# (drow, dcol) in D8 order. Diagonal distance is cell * sqrt(2).
_NEIGH = (
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, -1),
    (0, 1),
    (1, -1),
    (1, 0),
    (1, 1),
)


def priority_flood(dem: np.ndarray, valid: np.ndarray, eps: float = 1e-5) -> np.ndarray:
    """Fill depressions. Invalid cells stay NaN and act as barriers."""
    filled = np.array(dem, dtype=np.float64, copy=True)
    filled[~valid] = np.nan
    h, w = filled.shape
    visited = ~valid
    heap: list[tuple[float, int, int]] = []
    for r in range(h):
        for c in range(w):
            if visited[r, c]:
                continue
            if r == 0 or c == 0 or r == h - 1 or c == w - 1:
                visited[r, c] = True
                heapq.heappush(heap, (float(filled[r, c]), r, c))

    while heap:
        z, r, c = heapq.heappop(heap)
        for dr, dc in _NEIGH:
            rr, cc = r + dr, c + dc
            if rr < 0 or cc < 0 or rr >= h or cc >= w or visited[rr, cc]:
                continue
            visited[rr, cc] = True
            zz = float(filled[rr, cc])
            if zz <= z:
                zz = z + eps
                filled[rr, cc] = zz
            heapq.heappush(heap, (zz, rr, cc))
    return filled


def d8_flow(filled: np.ndarray, valid: np.ndarray, cell_m: float) -> np.ndarray:
    """Steepest-descent D8. -1 means no downstream cell."""
    h, w = filled.shape
    diag = cell_m * math.sqrt(2.0)
    dists = (diag, cell_m, diag, cell_m, cell_m, diag, cell_m, diag)
    pad = np.pad(filled, 1, mode="constant", constant_values=np.inf)
    slopes = []
    for (dr, dc), dist in zip(_NEIGH, dists):
        neigh = pad[1 + dr : 1 + dr + h, 1 + dc : 1 + dc + w]
        slopes.append((filled - neigh) / dist)
    stacked = np.stack(slopes, axis=0)
    k = np.argmax(stacked, axis=0).astype(np.int16)
    max_slope = np.max(stacked, axis=0)
    flow = np.where(max_slope > 0, k, np.int16(-1)).astype(np.int16)
    flow[~valid] = np.int16(-1)
    return flow


def d8_accumulation(flow: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Upstream cell count, including the cell itself. Invalid cells are 0."""
    h, w = flow.shape
    indeg = np.zeros((h, w), dtype=np.int32)
    acc = np.zeros((h, w), dtype=np.float64)
    acc[valid] = 1.0
    drs = np.array([d[0] for d in _NEIGH], dtype=np.int32)
    dcs = np.array([d[1] for d in _NEIGH], dtype=np.int32)

    rr, cc = np.where(valid & (flow >= 0))
    f = flow[rr, cc].astype(np.int32)
    nr = rr + drs[f]
    nc = cc + dcs[f]
    inside = (nr >= 0) & (nr < h) & (nc >= 0) & (nc < w)
    np.add.at(indeg, (nr[inside], nc[inside]), 1)

    from collections import deque

    q = deque(zip(*(np.where(valid & (indeg == 0)))))
    while q:
        r, c = q.popleft()
        fdir = int(flow[r, c])
        if fdir < 0:
            continue
        r2 = r + int(drs[fdir])
        c2 = c + int(dcs[fdir])
        if not (0 <= r2 < h and 0 <= c2 < w):
            continue
        acc[r2, c2] += acc[r, c]
        indeg[r2, c2] -= 1
        if indeg[r2, c2] == 0 and valid[r2, c2]:
            q.append((r2, c2))
    acc[~valid] = 0
    return acc


def compute_hand(
    dem: np.ndarray,
    flow: np.ndarray,
    acc: np.ndarray,
    stream: np.ndarray,
    valid: np.ndarray,
) -> np.ndarray:
    """HAND in metres. Drainage cells are 0. Unresolved cells (no downstream stream) are NaN."""
    h, w = dem.shape
    outlet = np.full((h, w), np.nan, dtype=np.float64)
    outlet[stream & valid] = dem[stream & valid]
    drs = np.array([d[0] for d in _NEIGH], dtype=np.int32)
    dcs = np.array([d[1] for d in _NEIGH], dtype=np.int32)

    flat = np.flatnonzero(valid.ravel())
    order = flat[np.argsort(acc.ravel()[flat], kind="mergesort")[::-1]]
    rows, cols = np.unravel_index(order, (h, w))
    for r, c in zip(rows.tolist(), cols.tolist()):
        if stream[r, c]:
            continue
        fdir = int(flow[r, c])
        if fdir < 0:
            continue
        r2 = r + int(drs[fdir])
        c2 = c + int(dcs[fdir])
        if not (0 <= r2 < h and 0 <= c2 < w):
            continue
        if np.isfinite(outlet[r2, c2]):
            outlet[r, c] = outlet[r2, c2]
    hand = dem - outlet
    hand = np.where(np.isfinite(hand), np.maximum(hand, 0.0), np.nan)
    hand[stream & valid] = 0.0
    hand[~valid] = np.nan
    return hand


def slope_degrees(dem: np.ndarray, valid: np.ndarray, cell_m: float) -> np.ndarray:
    """Slope magnitude in degrees from the original DEM."""
    dem64 = np.array(dem, dtype=np.float64, copy=True)
    dem64[~valid] = np.nan
    dy, dx = np.gradient(dem64, cell_m, cell_m)
    slope = np.degrees(np.arctan(np.hypot(dx, dy)))
    slope[~valid] = np.nan
    return slope


def terrain_derivatives(
    dem_m: np.ndarray,
    cell_m: float,
    stream_area_km2: float,
) -> dict[str, np.ndarray]:
    """HAND, slope, and drainage mask for one projected DEM grid."""
    # np.array(..., copy=False) raises on NumPy 2 when a dtype cast is required.
    dem = np.asarray(dem_m, dtype=np.float64)
    valid = np.isfinite(dem)
    if int(valid.sum()) < 9:
        nan = np.full(dem.shape, np.nan, dtype=np.float64)
        return {
            "valid": valid,
            "filled": nan,
            "flow": np.full(dem.shape, -1, dtype=np.int16),
            "accumulation": np.zeros(dem.shape, dtype=np.float64),
            "stream": np.zeros(dem.shape, dtype=bool),
            "hand": nan,
            "slope": nan,
        }
    filled = priority_flood(dem, valid)
    flow = d8_flow(filled, valid, cell_m)
    acc = d8_accumulation(flow, valid)
    cell_area_km2 = (cell_m * cell_m) / 1e6
    stream = valid & (acc * cell_area_km2 >= stream_area_km2)
    hand = compute_hand(dem, flow, acc, stream, valid)
    slope = slope_degrees(dem, valid, cell_m)
    return {
        "valid": valid,
        "filled": filled,
        "flow": flow,
        "accumulation": acc,
        "stream": stream,
        "hand": hand,
        "slope": slope,
    }


def terrain_concern(hand: np.ndarray, slope: np.ndarray) -> np.ndarray:
    """0–100 concern. Low HAND and low slope score higher. NaN where either input is NaN."""
    hand_s = np.clip((HAND_CAP_M - hand) / HAND_CAP_M, 0.0, 1.0)
    slope_s = np.clip((SLOPE_CAP_DEG - slope) / SLOPE_CAP_DEG, 0.0, 1.0)
    score = 100.0 * (W_HAND * hand_s + W_SLOPE * slope_s)
    score[~np.isfinite(hand) | ~np.isfinite(slope)] = np.nan
    return score
