"""Fixed screening parameters.

These are pre-specified. They are not tuned on the Sen1Floods11 test split.
"""

from __future__ import annotations

# Analysis grid. Resolution coarsens only when the AOI would exceed MAX_GRID_SIDE.
RES_M = 30.0
MAX_GRID_SIDE = 1200
BUFFER_M = 3000.0

# D8 drainage: cells with upstream area at or above this are the HAND drainage network.
STREAM_AREA_KM2 = 0.5

# Terrain concern saturates at low HAND / flat slope and is 0 beyond these caps.
HAND_CAP_M = 10.0
SLOPE_CAP_DEG = 5.0
W_HAND = 0.8
W_SLOPE = 0.2

# JRC Global Surface Water occurrence (percent of observations). At/above this,
# the pixel is permanent water and is excluded from land scores.
PERMANENT_OCCURRENCE = 80.0

# Screening composite on non-permanent land. Validation reports the terrain
# component alone so historical occurrence is not a predictor.
W_TERRAIN = 0.7
W_WATER = 0.3

# Pre-specified operating point for precision/recall (not fit on the test set).
SCREEN_THRESHOLD = 50.0

# Copernicus DSM sentinels sometimes use large negative fill. Real bathymetry
# in this product is near sea level, not thousands of metres below.
DEM_NODATA_BELOW_M = -1000.0
DEM_NODATA_ABOVE_M = 9000.0
