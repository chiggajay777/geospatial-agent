"""AOI (Area of Interest) parsing and validation with Shapely."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shapely.geometry import box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.validation import make_valid


@dataclass
class AOI:
    """Validated AOI in WGS84 (EPSG:4326)."""

    geometry: BaseGeometry
    source: str  # path, bbox, or inline
    crs: str = "EPSG:4326"

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.geometry.bounds  # minx, miny, maxx, maxy

    @property
    def centroid(self) -> tuple[float, float]:
        c = self.geometry.centroid
        return (c.x, c.y)

    @property
    def area_deg2(self) -> float:
        """Rough area in square degrees (not metric). For screening only."""
        return float(self.geometry.area)

    def approx_area_km2(self) -> float:
        """Approximate geodesic-ish area using equirectangular at centroid lat."""
        import math

        minx, miny, maxx, maxy = self.bounds
        lat = self.centroid[1]
        # 1 deg lat ~ 111.32 km; 1 deg lon ~ 111.32 * cos(lat)
        km_per_deg_lat = 111.32
        km_per_deg_lon = 111.32 * math.cos(math.radians(lat))
        # Use shapely area in deg^2 then scale — better: project bbox corners
        # Simple polygon area via shoelace on projected coords
        geom = self.geometry
        if geom.geom_type == "Polygon":
            coords = list(geom.exterior.coords)
        elif geom.geom_type == "MultiPolygon":
            coords = []
            for g in geom.geoms:
                coords.extend(list(g.exterior.coords))
        else:
            # fallback: bbox
            return abs(maxx - minx) * km_per_deg_lon * abs(maxy - miny) * km_per_deg_lat

        # Project lon/lat to local km and use shapely
        from shapely.geometry import Polygon as SPoly

        def to_km(lon: float, lat: float) -> tuple[float, float]:
            x = (lon - self.centroid[0]) * km_per_deg_lon
            y = (lat - self.centroid[1]) * km_per_deg_lat
            return (x, y)

        if geom.geom_type == "Polygon":
            ring = [to_km(lon, lat) for lon, lat in geom.exterior.coords]
            return abs(SPoly(ring).area)
        total = 0.0
        for g in geom.geoms:
            ring = [to_km(lon, lat) for lon, lat in g.exterior.coords]
            total += abs(SPoly(ring).area)
        return total

    def to_geojson_feature(self, properties: dict[str, Any] | None = None) -> dict:
        return {
            "type": "Feature",
            "geometry": mapping(self.geometry),
            "properties": properties or {},
        }

    def to_feature_collection(self, properties: dict[str, Any] | None = None) -> dict:
        return {
            "type": "FeatureCollection",
            "features": [self.to_geojson_feature(properties)],
        }


def _normalize_geom(geom: BaseGeometry) -> BaseGeometry:
    if not geom.is_valid:
        geom = make_valid(geom)
    if geom.is_empty:
        raise ValueError("AOI geometry is empty after validation")
    # Prefer polygonal
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        if not polys:
            raise ValueError("AOI must contain at least one polygon")
        return unary_union(polys)
    # Buffer points/lines slightly? Better fail for flood screening
    raise ValueError(f"AOI must be Polygon/MultiPolygon, got {geom.geom_type}")


def aoi_from_bbox(west: float, south: float, east: float, north: float) -> AOI:
    if not (-180 <= west < east <= 180):
        raise ValueError(f"Invalid lon range: west={west}, east={east}")
    if not (-90 <= south < north <= 90):
        raise ValueError(f"Invalid lat range: south={south}, north={north}")
    # Guard absurd AOIs (continent-scale) for demo safety
    if (east - west) > 5 or (north - south) > 5:
        raise ValueError(
            "AOI bbox too large for v0.1 screening (>5° on a side). "
            "Use a smaller site / district AOI."
        )
    geom = _normalize_geom(box(west, south, east, north))
    return AOI(geometry=geom, source=f"bbox:{west},{south},{east},{north}")


def aoi_from_geojson(data: dict | str | Path) -> AOI:
    if isinstance(data, (str, Path)):
        path = Path(data)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            source = str(path)
        else:
            raw = json.loads(str(data))
            source = "inline-json"
    else:
        raw = data
        source = "dict"

    geom = _extract_geometry(raw)
    geom = _normalize_geom(geom)
    minx, miny, maxx, maxy = geom.bounds
    if (maxx - minx) > 5 or (maxy - miny) > 5:
        raise ValueError(
            "AOI too large for v0.1 screening (>5° on a side). "
            "Use a smaller site / district AOI."
        )
    return AOI(geometry=geom, source=source)


def _extract_geometry(raw: dict) -> BaseGeometry:
    t = raw.get("type")
    if t == "FeatureCollection":
        feats = raw.get("features") or []
        if not feats:
            raise ValueError("FeatureCollection has no features")
        geoms = []
        for f in feats:
            g = f.get("geometry")
            if g:
                geoms.append(shape(g))
        if not geoms:
            raise ValueError("No geometries in FeatureCollection")
        return unary_union(geoms)
    if t == "Feature":
        g = raw.get("geometry")
        if not g:
            raise ValueError("Feature has no geometry")
        return shape(g)
    if t in (
        "Polygon",
        "MultiPolygon",
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "GeometryCollection",
    ):
        return shape(raw)
    raise ValueError(f"Unsupported GeoJSON type: {t}")
