"""Geoapify REST client for place context around a flood screening.

Calls the public HTTP APIs documented at https://apidocs.geoapify.com
(OpenAPI 3.1 query names). This module does not talk to the MCP server.
MCP is for AI clients; see the README.

Three calls:

- Geocoding: ``GET https://api.geoapify.com/v1/geocode/search``
- Places: ``GET https://api.geoapify.com/v2/places``
- Isoline (travel time): ``GET https://api.geoapify.com/v1/isoline`` with ``type=time``

The API key is ``GEOAPIFY_API_KEY`` from the environment. ``python-dotenv``
is loaded when it is installed; a missing package is ignored.

Returned places and polygons are copied from the JSON body. Nothing in
this module invents a place name or a reachable-area geometry. A travel-time
polygon is not a flood extent and not a flood depth.

The free plan is about 3000 requests per day. An isoline that answers
HTTP 202 is fetched once more by ``id``. There is no retry loop.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from geo_flood_agent.aoi import AOI, aoi_from_bbox

GEOCODE_URL = "https://api.geoapify.com/v1/geocode/search"
PLACES_URL = "https://api.geoapify.com/v2/places"
ISOLINE_URL = "https://api.geoapify.com/v1/isoline"

# Public assets used as exposure context. Not a commercial POI catalogue.
DEFAULT_PLACE_CATEGORIES: tuple[str, ...] = (
    "healthcare.hospital",
    "education.school",
    "education.university",
    "service.fire_station",
    "emergency.ambulance_station",
    "service.police",
    "public_transport.train",
    "public_transport.subway",
)

TRAVEL_MODES: tuple[str, ...] = ("drive", "walk", "bicycle", "transit")

# Geocoder extents outside this band are a poor screening AOI: a building
# footprint is tiny, and a city or country box can exceed the 5° guard.
# Sides are approximate equirectangular meters.
_BBOX_MIN_SIDE_M = 400.0
_BBOX_MAX_SIDE_M = 8000.0
_BUFFER_MIN_M = 200.0
_BUFFER_MAX_M = 20000.0
_RADIUS_MIN_M = 100.0
_RADIUS_MAX_M = 10000.0
_PLACES_LIMIT_MAX = 20
_ISOLINE_MIN_MINUTES = 1.0
_ISOLINE_MAX_MINUTES = 60.0

_M_PER_DEG_LAT = 111_320.0

NOT_FLOOD_DEPTH_EN = (
    "Geoapify records are location context. They are not inputs to the "
    "susceptibility score, and they are not flood depths."
)
NOT_FLOOD_DEPTH_ZH = (
    "Geoapify 记录是地点上下文。它们不进入易感性得分，也不是洪水水深。"
)

KEY_MESSAGE = (
    "GEOAPIFY_API_KEY is not set. --place and the Geoapify tools "
    "(geocode_place, search_nearby_places, travel_time_isoline) need it. "
    "Export the variable, or copy .env.example to .env and fill it in. "
    "Do not commit .env. --demo does not use Geoapify and does not need a key."
)

HttpGet = Callable[[str, float], tuple[int, dict]]


class GeoapifyError(Exception):
    """Base error. Messages must not contain the API key."""


class GeoapifyKeyError(GeoapifyError):
    """Missing GEOAPIFY_API_KEY. Place mode should stop."""


class GeoapifyRequestError(GeoapifyError):
    """The HTTP call failed or the body could not be used."""


class GeoapifyNoResult(GeoapifyError):
    """Geocoding returned no feature, so no AOI was built."""


def _redact(text: str, api_key: str | None) -> str:
    if not text:
        return text
    if api_key and api_key in text:
        text = text.replace(api_key, "REDACTED")
    return text


def resolve_api_key(explicit: str | None = None) -> str:
    """Read ``GEOAPIFY_API_KEY``. ``explicit`` wins when it is non-empty.

    ``load_dotenv(override=False)`` runs when python-dotenv is installed,
    so a key already set in the process (including an empty test override)
    is left alone.
    """
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None  # type: ignore[assignment]
    if load_dotenv is not None:
        load_dotenv(override=False)
    key = (os.getenv("GEOAPIFY_API_KEY") or "").strip()
    if not key:
        raise GeoapifyKeyError(KEY_MESSAGE)
    return key


def http_get(url: str, timeout: float = 40.0) -> tuple[int, dict]:
    """GET ``url`` and return ``(status, json_object)``.

    HTTP error bodies are parsed and returned with their status. The URL,
    which carries ``apiKey``, is not included in raised messages.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, application/geo+json",
            "User-Agent": "geo-flood-agent academic-screening",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200))
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read() or b""
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", "request failed")
        raise GeoapifyRequestError(f"Geoapify network error: {reason}") from None
    if not raw:
        return status, {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GeoapifyRequestError(f"Geoapify returned non-JSON (HTTP {status}).") from None
    if not isinstance(data, dict):
        raise GeoapifyRequestError(
            f"Geoapify returned JSON that is not an object (HTTP {status})."
        )
    return status, data


def _coord(value: float) -> str:
    return f"{float(value):.6f}"


def _meters_per_degree(lat: float) -> tuple[float, float]:
    lon_m = _M_PER_DEG_LAT * math.cos(math.radians(lat))
    return max(lon_m, 1.0), _M_PER_DEG_LAT


def bbox_side_meters(
    west: float, south: float, east: float, north: float
) -> tuple[float, float]:
    mid_lat = (south + north) / 2.0
    lon_m, lat_m = _meters_per_degree(mid_lat)
    return abs(east - west) * lon_m, abs(north - south) * lat_m


def point_buffer_bbox(lon: float, lat: float, radius_m: float) -> tuple[float, float, float, float]:
    """Square around a point. Equirectangular, not a geodesic circle."""
    lon_m, lat_m = _meters_per_degree(lat)
    dlon = float(radius_m) / lon_m
    dlat = float(radius_m) / lat_m
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def validate_buffer_m(buffer_m: float) -> float:
    try:
        value = float(buffer_m)
    except (TypeError, ValueError) as exc:
        raise GeoapifyError("AOI buffer must be a number of meters.") from exc
    if value < _BUFFER_MIN_M or value > _BUFFER_MAX_M:
        raise GeoapifyError(
            f"AOI buffer must be between {_BUFFER_MIN_M:.0f} and {_BUFFER_MAX_M:.0f} meters."
        )
    return value


def validate_radius_m(radius_m: float) -> float:
    try:
        value = float(radius_m)
    except (TypeError, ValueError) as exc:
        raise GeoapifyError("Places radius must be a number of meters.") from exc
    if value < _RADIUS_MIN_M or value > _RADIUS_MAX_M:
        raise GeoapifyError(
            f"Places radius must be between {_RADIUS_MIN_M:.0f} and {_RADIUS_MAX_M:.0f} meters."
        )
    return value


def validate_places_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise GeoapifyError("Places limit must be an integer.") from exc
    if value < 1 or value > _PLACES_LIMIT_MAX:
        raise GeoapifyError(f"Places limit must be from 1 to {_PLACES_LIMIT_MAX}.")
    return value


def validate_travel_mode(mode: str) -> str:
    text = (mode or "").strip().lower()
    if text not in TRAVEL_MODES:
        allowed = ", ".join(TRAVEL_MODES)
        raise GeoapifyError(f"Travel mode must be one of: {allowed}.")
    return text


def validate_isoline_minutes(minutes: float) -> int:
    """Return the Isoline ``range`` in seconds (``type=time``)."""
    try:
        value = float(minutes)
    except (TypeError, ValueError) as exc:
        raise GeoapifyError("Isoline minutes must be a number.") from exc
    if value < _ISOLINE_MIN_MINUTES or value > _ISOLINE_MAX_MINUTES:
        raise GeoapifyError(
            f"Isoline range must be between {_ISOLINE_MIN_MINUTES:.0f} and "
            f"{_ISOLINE_MAX_MINUTES:.0f} minutes."
        )
    return int(round(value * 60.0))


def parse_categories(raw: str | None) -> list[str]:
    text = raw if raw is not None and str(raw).strip() else ",".join(DEFAULT_PLACE_CATEGORIES)
    cats = [part.strip() for part in str(text).split(",") if part.strip()]
    if not cats:
        raise GeoapifyError("Places categories are empty.")
    if len(cats) > 20:
        raise GeoapifyError("At most 20 place categories.")
    for cat in cats:
        ok = all(ch.isalnum() or ch in "._" for ch in cat) and ".." not in cat
        if not ok or cat.startswith(".") or cat.endswith("."):
            raise GeoapifyError(f"Invalid Geoapify category key: {cat}")
    return cats


def _normalize_bbox(raw: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        a, b, c, d = (float(x) for x in raw)
    except (TypeError, ValueError):
        return None
    west, east = min(a, c), max(a, c)
    south, north = min(b, d), max(b, d)
    if west >= east or south >= north:
        return None
    if west < -180 or east > 180 or south < -90 or north > 90:
        return None
    return west, south, east, north


def _feature_bbox(feature: dict) -> tuple[float, float, float, float] | None:
    bbox = _normalize_bbox(feature.get("bbox"))
    if bbox:
        return bbox
    props = feature.get("properties") or {}
    return _normalize_bbox(props.get("bbox"))


def _point_lon_lat(feature: dict) -> tuple[float, float] | None:
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") if isinstance(geom, dict) else None
    if geom.get("type") == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
        try:
            return float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            return None
    props = feature.get("properties") or {}
    if "lon" in props and "lat" in props:
        try:
            return float(props["lon"]), float(props["lat"])
        except (TypeError, ValueError):
            return None
    return None


def _geocode_features(payload: dict) -> list[dict]:
    if payload.get("type") == "FeatureCollection":
        feats = payload.get("features") or []
        return [f for f in feats if isinstance(f, dict)]
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    features = []
    for row in results:
        if not isinstance(row, dict):
            continue
        lon, lat = row.get("lon"), row.get("lat")
        if lon is None or lat is None:
            continue
        features.append(
            {
                "type": "Feature",
                "bbox": row.get("bbox"),
                "properties": row,
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            }
        )
    return features


def _attribution(props: dict) -> str | None:
    src = props.get("datasource")
    if not isinstance(src, dict):
        return None
    text = src.get("attribution")
    if text:
        return str(text)
    return None


def _error_text(status: int, body: dict, api_key: str) -> str:
    msg = ""
    if isinstance(body, dict):
        raw = body.get("message") or body.get("error") or ""
        msg = _redact(str(raw), api_key).strip()
    if msg:
        return f"Geoapify HTTP {status}: {msg}"
    return f"Geoapify HTTP {status}."


def _cell_name(props: dict) -> tuple[str, str]:
    for key in ("name", "address_line1", "formatted"):
        value = props.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), key
    return "(unnamed)", "missing"


def _iter_positions(geometry: dict):
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        for ring in coords or []:
            yield from ring
    elif gtype == "MultiPolygon":
        for poly in coords or []:
            for ring in poly:
                yield from ring


def geometry_stats(geometry: dict) -> tuple[int, list[float] | None]:
    count = 0
    minx = miny = maxx = maxy = None
    for pos in _iter_positions(geometry):
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            continue
        try:
            x, y = float(pos[0]), float(pos[1])
        except (TypeError, ValueError):
            continue
        count += 1
        minx = x if minx is None else min(minx, x)
        maxx = x if maxx is None else max(maxx, x)
        miny = y if miny is None else min(miny, y)
        maxy = y if maxy is None else max(maxy, y)
    if count == 0 or minx is None or miny is None or maxx is None or maxy is None:
        return 0, None
    return count, [minx, miny, maxx, maxy]


def build_screening_aoi(geocode: dict, buffer_m: float) -> tuple[AOI, dict]:
    """Choose an AOI from a geocode payload.

    Use the geocoder bbox when both sides fall between 400 m and 8 km.
    Otherwise buffer the returned point. The choice is written onto a
    copy of ``geocode`` so the report can say which rule fired.
    """
    buffer_m = validate_buffer_m(buffer_m)
    lon = float(geocode["lon"])
    lat = float(geocode["lat"])
    raw_bbox = geocode.get("geocoder_bbox")
    bbox = tuple(raw_bbox) if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4 else None
    method = "point_buffer"
    used: tuple[float, float, float, float]
    if bbox is not None:
        west, south, east, north = (float(x) for x in bbox)
        width_m, height_m = bbox_side_meters(west, south, east, north)
        if _BBOX_MIN_SIDE_M <= width_m <= _BBOX_MAX_SIDE_M and _BBOX_MIN_SIDE_M <= height_m <= _BBOX_MAX_SIDE_M:
            method = "geocoder_bbox"
            used = (west, south, east, north)
        else:
            used = point_buffer_bbox(lon, lat, buffer_m)
    else:
        used = point_buffer_bbox(lon, lat, buffer_m)
    aoi = aoi_from_bbox(*used)
    label = geocode.get("formatted") or geocode.get("query") or "place"
    aoi.source = f"geoapify-geocode:{label}"[:240]
    payload = dict(geocode)
    payload["aoi_method"] = method
    payload["aoi_buffer_m"] = None if method == "geocoder_bbox" else buffer_m
    payload["aoi_bounds"] = [float(x) for x in aoi.bounds]
    if method == "geocoder_bbox":
        payload["note_en"] = (
            "AOI is the Geoapify geocoder bbox (both sides between 400 m and 8 km). "
            "The screening polygon is that rectangle, not a flood extent."
        )
        payload["note_zh"] = (
            "兴趣区采用 Geoapify 地理编码返回的包围盒（两边都在 400 m 到 8 km 之间）。"
            "该矩形不是洪水范围。"
        )
    else:
        payload["note_en"] = (
            f"AOI is a {buffer_m:.0f} m equirectangular square around the geocoded point. "
            "The geocoder bbox was missing or outside 400 m–8 km, so it was not used. "
            "The square is not a flood extent."
        )
        payload["note_zh"] = (
            f"兴趣区是地理编码点周围 {buffer_m:.0f} m 的等距矩形。"
            "地理编码包围盒缺失，或不在 400 m–8 km 范围内，因此未采用。"
            "该矩形不是洪水范围。"
        )
    return aoi, payload


def geocode_point_feature(geocode: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [float(geocode["lon"]), float(geocode["lat"])],
        },
        "properties": {
            "role": "geoapify_geocode",
            "kind": "LIVE",
            "provider": "Geoapify",
            "api": GEOCODE_URL,
            "query": geocode.get("query"),
            "formatted": geocode.get("formatted"),
            "name": geocode.get("name"),
            "result_type": geocode.get("result_type"),
            "confidence": geocode.get("confidence"),
            "place_id": geocode.get("place_id"),
            "note": "Geocoded point used to build the screening AOI. Not a flood depth.",
        },
    }


def place_point_feature(place: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [float(place["lon"]), float(place["lat"])],
        },
        "properties": {
            "role": "geoapify_place",
            "kind": "LIVE",
            "provider": "Geoapify",
            "api": PLACES_URL,
            "name": place.get("name"),
            "name_from": place.get("name_from"),
            "categories": place.get("categories") or [],
            "distance_m": place.get("distance_m"),
            "formatted": place.get("formatted"),
            "place_id": place.get("place_id"),
            "note": "Nearby place returned by the Geoapify Places API. Not a flood observation.",
        },
    }


def assemble_geo_context(geocode: dict, places: dict, isoline: dict) -> dict:
    features: list[dict] = [geocode_point_feature(geocode)]
    if places.get("kind") == "LIVE":
        for place in places.get("places") or []:
            features.append(place_point_feature(place))
    features.extend(isoline.get("features") or [])
    return {
        "provider": "Geoapify",
        "geocode": geocode,
        "places": places,
        "isoline": isoline,
        "features": features,
    }


def public_geo_context(ctx: dict) -> dict:
    """Drop polygons. Summary and tool traces keep metadata and place rows."""
    out: dict[str, Any] = {"provider": ctx.get("provider", "Geoapify")}
    for key in ("geocode", "places", "isoline"):
        block = dict(ctx.get(key) or {})
        block.pop("features", None)
        out[key] = block
    return out


def skipped_isoline(lon: float, lat: float) -> dict:
    return {
        "kind": "SKIPPED",
        "endpoint": ISOLINE_URL,
        "origin_lon": lon,
        "origin_lat": lat,
        "mode": None,
        "isoline_type": "time",
        "range_seconds": None,
        "traffic": None,
        "http_status": None,
        "isoline_id": None,
        "geometry_types": [],
        "vertex_count": 0,
        "bbox": None,
        "error": None,
        "follow_up": False,
        "features": [],
        "note_en": (
            "Travel-time isoline was not requested (--no-isoline). "
            "No reachable area was created."
        ),
        "note_zh": "未请求出行时间等时圈（--no-isoline）。没有生成可达范围。",
    }


def _unavailable_places(
    *,
    categories: list[str],
    radius_m: float,
    limit: int,
    lon: float,
    lat: float,
    error: str,
) -> dict:
    filt = f"circle:{_coord(lon)},{_coord(lat)},{int(round(radius_m))}"
    return {
        "kind": "UNAVAILABLE",
        "endpoint": PLACES_URL,
        "categories": categories,
        "filter": filt,
        "bias": f"proximity:{_coord(lon)},{_coord(lat)}",
        "limit": limit,
        "count": 0,
        "places": [],
        "error": error,
        "http_status": None,
        "skipped_without_coordinates": 0,
        "attributions": [],
        "note_en": "Places were not listed because the Geoapify request failed. No names were filled in.",
        "note_zh": "Geoapify 地点请求失败，因此没有列出地点，也没有补写名称。",
    }


def _unavailable_isoline(
    *,
    lon: float,
    lat: float,
    mode: str,
    range_seconds: int,
    error: str,
    http_status: int | None = None,
    isoline_id: str | None = None,
    follow_up: bool = False,
) -> dict:
    return {
        "kind": "UNAVAILABLE",
        "endpoint": ISOLINE_URL,
        "origin_lon": lon,
        "origin_lat": lat,
        "mode": mode,
        "isoline_type": "time",
        "range_seconds": range_seconds,
        "traffic": "free_flow",
        "http_status": http_status,
        "isoline_id": isoline_id,
        "geometry_types": [],
        "vertex_count": 0,
        "bbox": None,
        "error": error,
        "follow_up": follow_up,
        "features": [],
        "note_en": "No travel-time polygon is shown. A circle was not substituted.",
        "note_zh": "没有出行时间多边形。没有用圆来代替。",
    }


class GeoapifyClient:
    """Small REST client. Pass ``http_get_fn`` in tests to avoid the network."""

    def __init__(
        self,
        api_key: str | None = None,
        http_get_fn: HttpGet | None = None,
        timeout_s: float = 40.0,
    ):
        self.api_key = resolve_api_key(api_key)
        self._http_get_fn = http_get_fn
        self.timeout_s = timeout_s
        self.calls: list[dict[str, Any]] = []

    def _fetch(self, url: str) -> tuple[int, dict]:
        getter = self._http_get_fn if self._http_get_fn is not None else http_get
        return getter(url, self.timeout_s)

    def _get(self, base: str, params: dict[str, Any]) -> tuple[int, dict]:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{base}?{query}"
        path = urllib.parse.urlparse(base).path
        try:
            status, body = self._fetch(url)
        except GeoapifyRequestError as exc:
            self.calls.append({"path": path, "status": None})
            raise GeoapifyRequestError(_redact(str(exc), self.api_key)) from None
        self.calls.append({"path": path, "status": status})
        if status >= 400:
            raise GeoapifyRequestError(_error_text(status, body, self.api_key))
        return status, body

    def geocode(self, text: str) -> dict:
        query = (text or "").strip()
        if not query:
            raise GeoapifyError("Place text is empty.")
        status, body = self._get(
            GEOCODE_URL,
            {
                "text": query,
                "format": "geojson",
                "limit": 1,
                "lang": "en",
                # Avoid the default IP-country bias so the hit does not depend
                # on the machine that sends the request.
                "bias": "countrycode:none",
                "apiKey": self.api_key,
            },
        )
        features = _geocode_features(body)
        if not features:
            raise GeoapifyNoResult(
                f"Geoapify Geocoding returned no results for {query!r}. No AOI was built."
            )
        feature = features[0]
        point = _point_lon_lat(feature)
        if point is None:
            raise GeoapifyNoResult(
                f"Geoapify Geocoding returned no coordinates for {query!r}. No AOI was built."
            )
        lon, lat = point
        props = feature.get("properties") or {}
        rank = props.get("rank") if isinstance(props.get("rank"), dict) else {}
        confidence = rank.get("confidence")
        try:
            confidence_f = None if confidence is None else float(confidence)
        except (TypeError, ValueError):
            confidence_f = None
        formatted = props.get("formatted") or props.get("address_line1") or query
        return {
            "kind": "LIVE",
            "endpoint": GEOCODE_URL,
            "query": query,
            "formatted": str(formatted),
            "name": props.get("name") if isinstance(props.get("name"), str) else None,
            "lon": lon,
            "lat": lat,
            "result_type": props.get("result_type"),
            "confidence": confidence_f,
            "place_id": props.get("place_id"),
            "geocoder_bbox": list(_feature_bbox(feature)) if _feature_bbox(feature) else None,
            "attribution": _attribution(props),
            "http_status": status,
            "data_source": "LIVE Geoapify Geocoding API",
        }

    def nearby_places(
        self,
        lon: float,
        lat: float,
        *,
        radius_m: float,
        limit: int = 15,
        categories: list[str] | None = None,
    ) -> dict:
        radius_m = validate_radius_m(radius_m)
        limit = validate_places_limit(limit)
        cats = list(categories) if categories is not None else list(DEFAULT_PLACE_CATEGORIES)
        if not cats:
            raise GeoapifyError("Places categories are empty.")
        radius_i = int(round(radius_m))
        filt = f"circle:{_coord(lon)},{_coord(lat)},{radius_i}"
        bias = f"proximity:{_coord(lon)},{_coord(lat)}"
        try:
            status, body = self._get(
                PLACES_URL,
                {
                    "categories": ",".join(cats),
                    "filter": filt,
                    "bias": bias,
                    "limit": limit,
                    "apiKey": self.api_key,
                },
            )
        except GeoapifyRequestError as exc:
            return _unavailable_places(
                categories=cats,
                radius_m=radius_m,
                limit=limit,
                lon=lon,
                lat=lat,
                error=str(exc),
            )
        places: list[dict] = []
        skipped = 0
        attributions: list[str] = []
        for feature in body.get("features") or []:
            if not isinstance(feature, dict):
                skipped += 1
                continue
            point = _point_lon_lat(feature)
            if point is None:
                skipped += 1
                continue
            props = feature.get("properties") or {}
            name, name_from = _cell_name(props)
            raw_cats = props.get("categories") or []
            if not isinstance(raw_cats, list):
                raw_cats = [str(raw_cats)]
            distance = props.get("distance")
            try:
                distance_m = None if distance is None else float(distance)
            except (TypeError, ValueError):
                distance_m = None
            formatted = props.get("formatted")
            places.append(
                {
                    "name": name,
                    "name_from": name_from,
                    "categories": [str(c) for c in raw_cats],
                    "lon": point[0],
                    "lat": point[1],
                    "distance_m": distance_m,
                    "formatted": None if formatted is None else str(formatted),
                    "place_id": props.get("place_id"),
                }
            )
            attr = _attribution(props)
            if attr and attr not in attributions:
                attributions.append(attr)
        return {
            "kind": "LIVE",
            "endpoint": PLACES_URL,
            "categories": cats,
            "filter": filt,
            "bias": bias,
            "limit": limit,
            "count": len(places),
            "places": places,
            "error": None,
            "http_status": status,
            "skipped_without_coordinates": skipped,
            "attributions": attributions,
            "data_source": "LIVE Geoapify Places API",
            "note_en": (
                "Names and coordinates are copied from the Geoapify Places response. "
                "They are not translated and not completed from any other source."
            ),
            "note_zh": "名称和坐标抄自 Geoapify Places 响应。未翻译，也未用其他来源补全。",
        }

    def isoline(
        self,
        lon: float,
        lat: float,
        *,
        mode: str = "drive",
        range_seconds: int = 600,
    ) -> dict:
        mode = validate_travel_mode(mode)
        try:
            seconds = int(range_seconds)
        except (TypeError, ValueError) as exc:
            raise GeoapifyError("Isoline range must be seconds.") from exc
        if seconds < 60 or seconds > 3600:
            raise GeoapifyError("Isoline range must be between 60 and 3600 seconds.")
        try:
            status, body = self._get(
                ISOLINE_URL,
                {
                    "lat": _coord(lat),
                    "lon": _coord(lon),
                    "type": "time",
                    "mode": mode,
                    "range": seconds,
                    "traffic": "free_flow",
                    "apiKey": self.api_key,
                },
            )
        except GeoapifyRequestError as exc:
            return _unavailable_isoline(
                lon=lon,
                lat=lat,
                mode=mode,
                range_seconds=seconds,
                error=str(exc),
            )
        polygons, iso_id = _isoline_polygons(body)
        followed = False
        # One follow-up when the router has not finished. Do not loop.
        if not polygons and iso_id and status in (200, 202):
            try:
                status, body = self._get(ISOLINE_URL, {"id": iso_id, "apiKey": self.api_key})
            except GeoapifyRequestError as exc:
                return _unavailable_isoline(
                    lon=lon,
                    lat=lat,
                    mode=mode,
                    range_seconds=seconds,
                    error=str(exc),
                    http_status=status,
                    isoline_id=iso_id,
                    follow_up=True,
                )
            followed = True
            polygons, iso_id2 = _isoline_polygons(body)
            iso_id = iso_id2 or iso_id
        if polygons and status == 200:
            return _live_isoline(
                lon=lon,
                lat=lat,
                mode=mode,
                range_seconds=seconds,
                http_status=status,
                isoline_id=iso_id,
                polygons=polygons,
                follow_up=followed,
            )
        if status == 202:
            return {
                "kind": "PENDING",
                "endpoint": ISOLINE_URL,
                "origin_lon": lon,
                "origin_lat": lat,
                "mode": mode,
                "isoline_type": "time",
                "range_seconds": seconds,
                "traffic": "free_flow",
                "http_status": status,
                "isoline_id": iso_id,
                "geometry_types": [],
                "vertex_count": 0,
                "bbox": None,
                "error": None,
                "follow_up": followed,
                "features": [],
                "data_source": "Geoapify Isoline API (geometry not ready)",
                "note_en": (
                    "Geoapify accepted the isoline (HTTP 202) but did not return a polygon "
                    "after one follow-up by id. No circle was substituted."
                ),
                "note_zh": (
                    "Geoapify 已接受等时圈请求（HTTP 202），但按 id 再取一次后仍无多边形。"
                    "没有用圆来代替。"
                ),
            }
        return _unavailable_isoline(
            lon=lon,
            lat=lat,
            mode=mode,
            range_seconds=seconds,
            error=(
                f"Geoapify Isoline HTTP {status} returned no Polygon or MultiPolygon. "
                "No circle was substituted."
            ),
            http_status=status,
            isoline_id=iso_id,
            follow_up=followed,
        )


def _isoline_polygons(payload: dict) -> tuple[list[dict], str | None]:
    iso_id = _isoline_id(payload)
    polygons: list[dict] = []
    for feature in payload.get("features") or []:
        if not isinstance(feature, dict):
            continue
        geom = feature.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        if not iso_id and props.get("id"):
            iso_id = str(props["id"])
        polygons.append({"geometry": geom, "properties": props})
    return polygons, iso_id


def _isoline_id(payload: dict) -> str | None:
    raw = payload.get("id")
    if isinstance(raw, str) and raw:
        return raw
    props = payload.get("properties")
    if isinstance(props, dict) and props.get("id"):
        return str(props["id"])
    for feature in payload.get("features") or []:
        if not isinstance(feature, dict):
            continue
        fp = feature.get("properties") or {}
        if isinstance(fp, dict) and fp.get("id"):
            return str(fp["id"])
    return None


def _live_isoline(
    *,
    lon: float,
    lat: float,
    mode: str,
    range_seconds: int,
    http_status: int,
    isoline_id: str | None,
    polygons: list[dict],
    follow_up: bool,
) -> dict:
    features = []
    types: list[str] = []
    vertices = 0
    bbox: list[float] | None = None
    for item in polygons:
        geom = item["geometry"]
        gtype = str(geom.get("type"))
        types.append(gtype)
        count, gb = geometry_stats(geom)
        vertices += count
        if gb:
            if bbox is None:
                bbox = list(gb)
            else:
                bbox = [
                    min(bbox[0], gb[0]),
                    min(bbox[1], gb[1]),
                    max(bbox[2], gb[2]),
                    max(bbox[3], gb[3]),
                ]
        api_props = item.get("properties") or {}
        features.append(
            {
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "role": "geoapify_isoline",
                    "kind": "LIVE",
                    "provider": "Geoapify",
                    "api": ISOLINE_URL,
                    "isoline_type": api_props.get("type") or "time",
                    "mode": api_props.get("mode") or mode,
                    "range_seconds": api_props.get("range")
                    if api_props.get("range") is not None
                    else range_seconds,
                    "isoline_id": api_props.get("id") or isoline_id,
                    "origin_lon": api_props.get("lon", lon),
                    "origin_lat": api_props.get("lat", lat),
                    "traffic": api_props.get("traffic") or "free_flow",
                    "note": (
                        "Travel-time reachable area from the Geoapify Isoline API. "
                        "Not a flood extent and not a flood depth."
                    ),
                },
            }
        )
    return {
        "kind": "LIVE",
        "endpoint": ISOLINE_URL,
        "origin_lon": lon,
        "origin_lat": lat,
        "mode": mode,
        "isoline_type": "time",
        "range_seconds": range_seconds,
        "traffic": "free_flow",
        "http_status": http_status,
        "isoline_id": isoline_id,
        "geometry_types": types,
        "vertex_count": vertices,
        "bbox": bbox,
        "error": None,
        "follow_up": follow_up,
        "features": features,
        "data_source": "LIVE Geoapify Isoline API",
        "note_en": (
            "The polygon is the area the Geoapify router returned for this travel time "
            f"({mode}, {range_seconds} s, traffic=free_flow). "
            "It is not a flood extent and not a flood depth."
        ),
        "note_zh": (
            f"多边形是 Geoapify 路由返回的可达范围（{mode}，{range_seconds} 秒，"
            "traffic=free_flow）。它不是洪水范围，也不是洪水水深。"
        ),
    }
