# Data access (no paid keys)

The live path and the validation path download only the windows they need. Nothing in this folder is required before a run.

## Terrain

Copernicus DEM GLO-30 public COGs, AWS bucket `copernicus-dem-30m` (eu-central-1), no credentials:

`https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com/Copernicus_DSM_COG_10_N30_00_E120_00_DEM/Copernicus_DSM_COG_10_N30_00_E120_00_DEM.tif`

The sample AOI sits in tile `N30_00_E120_00`. The object is about 46 MB. The agent reads a buffered window with HTTP range requests (a few MB), reprojects it to UTM, and computes HAND and slope. Ocean tiles are not published.

## Surface water

JRC Global Surface Water occurrence, Planetary Computer collection `jrc-gsw` (COG, v1.3, 1984–2020). The public SAS endpoint signs the blob URL; no account key. Occurrence ≥ 80% is treated as permanent water and is not scored as flood-prone land.

## Validation labels

Sen1Floods11 v1.1 hand labels and the dataset's JRC permanent-water chips, public GCS:

- `https://storage.googleapis.com/sen1floods11/v1.1/splits/flood_handlabeled/flood_test_data.csv`
- `.../HandLabeled/LabelHand/`
- `.../HandLabeled/JRCWaterHand/`

`--validate` uses the official test split. Labels are small. The DEM for each chip is again a COG window, not a full 1° tile.

## Sparse fallback

If the Copernicus window fails, terrain falls back to OpenTopoData SRTM90m (then Open-Elevation). That path is labeled as sparse samples and is not called HAND.
