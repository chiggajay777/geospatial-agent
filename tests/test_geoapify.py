"""Mocked Geoapify tests. No live HTTP.

Default runs must not call api.geoapify.com. The free plan is about 3000
requests per day, and the program demo key is not used here.
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geo_flood_agent.aoi import aoi_from_bbox
from geo_flood_agent.geoapify import (
    GeoapifyClient,
    GeoapifyKeyError,
    GeoapifyNoResult,
    GeoapifyRequestError,
    build_screening_aoi,
    http_get,
    parse_categories,
)
from geo_flood_agent.screening import run_screening

KEY = "sekrit-key"
PLACE = "West Lake, Hangzhou"

GEOCODE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "bbox": [120.13, 30.24, 120.15, 30.26],
            "geometry": {"type": "Point", "coordinates": [120.14, 30.25]},
            "properties": {
                "name": "West Lake",
                "formatted": "West Lake, Hangzhou, China",
                "lon": 120.14,
                "lat": 30.25,
                "result_type": "amenity",
                "place_id": "place-west-lake",
                "rank": {"confidence": 0.9},
                "city": "Hangzhou",
                "datasource": {
                    "sourcename": "openstreetmap",
                    "attribution": "© OpenStreetMap contributors",
                    "license": "Open Database License",
                },
            },
        }
    ],
}

PLACES = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [120.141, 30.251]},
            "properties": {
                "name": "Hangzhou Hospital Fixture",
                "categories": ["healthcare.hospital"],
                "distance": 180,
                "formatted": "Hangzhou Hospital Fixture, Hangzhou",
                "place_id": "place-hospital-fixture",
                "lon": 120.141,
                "lat": 30.251,
                "datasource": {"attribution": "© OpenStreetMap contributors"},
            },
        },
        {
            "type": "Feature",
            "geometry": None,
            "properties": {"name": "Should Not Appear", "categories": ["education.school"]},
        },
    ],
}

ISOLINE_RING = [
    [120.13, 30.24],
    [120.16, 30.24],
    [120.16, 30.27],
    [120.13, 30.27],
    [120.13, 30.24],
]

ISOLINE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ISOLINE_RING]},
            "properties": {
                "lat": 30.25,
                "lon": 120.14,
                "type": "time",
                "mode": "drive",
                "range": 600,
                "id": "iso-fixture-1",
                "traffic": "free_flow",
            },
        }
    ],
}


def _route(url: str, timeout: float = 40.0, *, places_status: int = 200, geocode_body=None, isoline_bodies=None):
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    if qs.get("apiKey") != [KEY]:
        raise AssertionError("apiKey query mismatch")
    path = parsed.path
    if path.endswith("/geocode/search"):
        if qs.get("text") != [PLACE]:
            raise AssertionError(qs.get("text"))
        if qs.get("bias") != ["countrycode:none"]:
            raise AssertionError(qs.get("bias"))
        body = GEOCODE if geocode_body is None else geocode_body
        return 200, body
    if path.endswith("/places"):
        if places_status >= 400:
            return places_status, {"message": f"upstream down {KEY}"}
        return 200, PLACES
    if path.endswith("/isoline"):
        if isoline_bodies is not None:
            return isoline_bodies.pop(0)
        if qs.get("type") != ["time"] or qs.get("mode") != ["drive"] or qs.get("range") != ["600"]:
            raise AssertionError(qs)
        return 200, ISOLINE
    raise AssertionError(path)


class ClientTests(unittest.TestCase):
    def test_missing_key_does_not_call_http(self):
        def boom(url, timeout=40.0):
            raise AssertionError("HTTP was called without a key")

        with patch.dict("os.environ", {"GEOAPIFY_API_KEY": ""}, clear=False):
            with self.assertRaises(GeoapifyKeyError) as caught:
                GeoapifyClient(http_get_fn=boom)
        self.assertIn("GEOAPIFY_API_KEY", str(caught.exception))
        self.assertNotIn("apiKey=", str(caught.exception))

    def test_geocode_bbox_becomes_aoi(self):
        client = GeoapifyClient(api_key=KEY, http_get_fn=lambda url, timeout=40.0: _route(url, timeout))
        payload = client.geocode(PLACE)
        self.assertEqual(payload["kind"], "LIVE")
        self.assertEqual(payload["formatted"], "West Lake, Hangzhou, China")
        self.assertEqual(payload["attribution"], "© OpenStreetMap contributors")
        aoi, built = build_screening_aoi(payload, 2000)
        self.assertEqual(built["aoi_method"], "geocoder_bbox")
        self.assertEqual(tuple(round(v, 6) for v in aoi.bounds), (120.13, 30.24, 120.15, 30.26))

    def test_huge_or_missing_bbox_uses_point_buffer(self):
        huge = {
            "lon": 120.14,
            "lat": 30.25,
            "formatted": "Hangzhou",
            "query": "Hangzhou",
            "geocoder_bbox": [119.0, 29.0, 121.5, 30.8],
        }
        aoi, built = build_screening_aoi(huge, 2000)
        self.assertEqual(built["aoi_method"], "point_buffer")
        west, south, east, north = aoi.bounds
        self.assertLess(east - west, 0.1)
        self.assertLess(north - south, 0.1)
        self.assertGreater(east, 120.14)
        self.assertLess(west, 120.14)

        missing = dict(huge)
        missing["geocoder_bbox"] = None
        _, built_missing = build_screening_aoi(missing, 2000)
        self.assertEqual(built_missing["aoi_method"], "point_buffer")

    def test_no_geocode_hit_builds_no_aoi(self):
        def fake(url, timeout=40.0):
            return 200, {"type": "FeatureCollection", "features": []}

        client = GeoapifyClient(api_key=KEY, http_get_fn=fake)
        with self.assertRaises(GeoapifyNoResult) as caught:
            client.geocode("nowhere-xyz")
        self.assertIn("No AOI was built", str(caught.exception))

    def test_places_skip_features_without_coordinates(self):
        client = GeoapifyClient(api_key=KEY, http_get_fn=lambda url, timeout=40.0: _route(url, timeout))
        # Drive the places parser directly.
        places = client.nearby_places(120.14, 30.25, radius_m=2000, limit=15)
        self.assertEqual(places["kind"], "LIVE")
        self.assertEqual(places["count"], 1)
        self.assertEqual(places["places"][0]["name"], "Hangzhou Hospital Fixture")
        self.assertEqual(places["skipped_without_coordinates"], 1)
        names = [row["name"] for row in places["places"]]
        self.assertNotIn("Should Not Appear", names)
        self.assertNotIn("People's Hospital", names)

    def test_empty_places_stays_live_and_empty(self):
        def fake(url, timeout=40.0):
            return 200, {"type": "FeatureCollection", "features": []}

        client = GeoapifyClient(api_key=KEY, http_get_fn=fake)
        places = client.nearby_places(120.14, 30.25, radius_m=1500, limit=5, categories=["healthcare.hospital"])
        self.assertEqual(places["kind"], "LIVE")
        self.assertEqual(places["places"], [])
        self.assertEqual(places["count"], 0)

    def test_http_error_redacts_key(self):
        def fake(url, timeout=40.0):
            return 401, {"message": f"Invalid apiKey {KEY}"}

        client = GeoapifyClient(api_key=KEY, http_get_fn=fake)
        with self.assertRaises(GeoapifyRequestError) as caught:
            client.geocode(PLACE)
        text = str(caught.exception)
        self.assertNotIn(KEY, text)
        self.assertIn("REDACTED", text)
        self.assertIn("401", text)

    def test_places_failure_is_unavailable_not_invented(self):
        client = GeoapifyClient(
            api_key=KEY,
            http_get_fn=lambda url, timeout=40.0: _route(url, timeout, places_status=500),
        )
        places = client.nearby_places(120.14, 30.25, radius_m=2000, limit=10)
        self.assertEqual(places["kind"], "UNAVAILABLE")
        self.assertEqual(places["places"], [])
        self.assertNotIn(KEY, places["error"])
        self.assertIn("REDACTED", places["error"])

    def test_isoline_polygon_is_the_response_geometry(self):
        client = GeoapifyClient(api_key=KEY, http_get_fn=lambda url, timeout=40.0: _route(url, timeout))
        isoline = client.isoline(120.14, 30.25, mode="drive", range_seconds=600)
        self.assertEqual(isoline["kind"], "LIVE")
        self.assertEqual(isoline["features"][0]["geometry"]["coordinates"], [ISOLINE_RING])
        self.assertEqual(isoline["isoline_id"], "iso-fixture-1")
        self.assertIn("not a flood depth", isoline["note_en"])
        self.assertEqual(len(client.calls), 1)

    def test_isoline_202_follows_id_once(self):
        pending = {
            "type": "FeatureCollection",
            "features": [],
            "properties": {"id": "iso-later"},
        }
        bodies = [(202, pending), (200, ISOLINE)]

        def fake(url, timeout=40.0):
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            self_calls.append(qs)
            return bodies.pop(0)

        self_calls: list = []
        client = GeoapifyClient(api_key=KEY, http_get_fn=fake)
        isoline = client.isoline(120.14, 30.25, mode="walk", range_seconds=600)
        self.assertEqual(isoline["kind"], "LIVE")
        self.assertTrue(isoline["follow_up"])
        self.assertEqual(len(self_calls), 2)
        self.assertEqual(self_calls[1].get("id"), ["iso-later"])
        self.assertNotIn("lat", self_calls[1])

    def test_isoline_still_pending_does_not_invent_geometry(self):
        pending = {"type": "FeatureCollection", "features": [], "properties": {"id": "iso-wait"}}
        bodies = [(202, pending), (202, pending)]

        def fake(url, timeout=40.0):
            return bodies.pop(0)

        client = GeoapifyClient(api_key=KEY, http_get_fn=fake)
        isoline = client.isoline(120.14, 30.25, mode="drive", range_seconds=600)
        self.assertEqual(isoline["kind"], "PENDING")
        self.assertEqual(isoline["features"], [])
        self.assertEqual(isoline["vertex_count"], 0)
        self.assertTrue(isoline["follow_up"])
        self.assertIn("No circle", isoline["note_en"])

    def test_bad_category_rejected_before_http(self):
        def boom(url, timeout=40.0):
            raise AssertionError("HTTP was called")

        with self.assertRaises(Exception) as caught:
            parse_categories("not a key")
        self.assertIn("Invalid", str(caught.exception))
        client = GeoapifyClient(api_key=KEY, http_get_fn=boom)
        with self.assertRaises(Exception):
            client.nearby_places(1, 2, radius_m=10, limit=5)

    def test_http_get_uses_urlopen_and_hides_the_url(self):
        class Resp:
            status = 200

            def read(self):
                return b'{"type":"FeatureCollection","features":[]}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch("geo_flood_agent.geoapify.urllib.request.urlopen", return_value=Resp()) as opened:
            status, body = http_get("https://api.geoapify.com/v1/geocode/search?apiKey=sekrit-key&text=x")
        self.assertEqual(status, 200)
        self.assertEqual(body["type"], "FeatureCollection")
        self.assertEqual(opened.call_count, 1)

        err = urllib.error.HTTPError(
            "https://api.geoapify.com/v1/geocode/search?apiKey=sekrit-key",
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"message":"nope"}'),
        )
        with patch("geo_flood_agent.geoapify.urllib.request.urlopen", side_effect=err):
            status, body = http_get("https://api.geoapify.com/v1/geocode/search?apiKey=sekrit-key")
        self.assertEqual(status, 401)
        self.assertEqual(body["message"], "nope")


class PlaceCLITests(unittest.TestCase):
    def _main(self, argv, handler):
        from geo_flood_agent.cli import main

        out_text = io.StringIO()
        err_text = io.StringIO()
        with patch("geo_flood_agent.geoapify.http_get", handler):
            with redirect_stdout(out_text), redirect_stderr(err_text):
                code = main(argv)
        return code, out_text.getvalue(), err_text.getvalue()

    def test_place_run_tags_context_live_and_flood_layers_mock(self):
        calls = []

        def handler(url, timeout=40.0):
            calls.append(url)
            return _route(url, timeout)

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            with patch.dict("os.environ", {"GEOAPIFY_API_KEY": KEY}, clear=False):
                code, stdout, stderr = self._main(
                    ["--place", PLACE, "--mode", "demo", "--out", str(out)],
                    handler,
                )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(len(calls), 3)
            self.assertIn("geocode", stdout)
            report = (out / "flood_screening_report.md").read_text(encoding="utf-8")
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            geojson = json.loads((out / "aoi_screening.geojson").read_text(encoding="utf-8"))
            trace = json.loads((out / "tool_trace.json").read_text(encoding="utf-8"))
            blob = report + json.dumps(summary) + json.dumps(geojson) + json.dumps(trace)
            self.assertNotIn(KEY, blob)
            self.assertIn("Hangzhou Hospital Fixture", report)
            self.assertNotIn("Should Not Appear", report)
            self.assertNotIn("People's Hospital", blob)
            self.assertIn("not a flood depth", report.lower())
            self.assertIn("**LIVE**", report)
            self.assertIn("West Lake, Hangzhou, China", report)
            self.assertEqual(summary["geoapify"]["geocode"]["kind"], "LIVE")
            self.assertEqual(summary["geoapify"]["places"]["kind"], "LIVE")
            self.assertEqual(summary["geoapify"]["places"]["count"], 1)
            self.assertEqual(summary["geoapify"]["isoline"]["kind"], "LIVE")
            self.assertEqual(summary["mode"], "demo")
            self.assertTrue(all(layer["is_mock"] for layer in summary["layers"]))
            names = [layer["name"] for layer in summary["layers"]]
            self.assertEqual(
                names,
                ["hydrology_proxy", "terrain_flatness_proxy", "inundation_susceptibility"],
            )
            expected = run_screening(aoi_from_bbox(120.13, 30.24, 120.15, 30.26), mode="demo")
            self.assertEqual(summary["composite_score_0_100"], expected.composite_score_0_100)
            roles = [f["properties"].get("role") for f in geojson["features"]]
            self.assertEqual(roles[0], "screening_aoi")
            self.assertIn("geoapify_geocode", roles)
            self.assertIn("geoapify_place", roles)
            self.assertIn("geoapify_isoline", roles)
            isoline = next(f for f in geojson["features"] if f["properties"]["role"] == "geoapify_isoline")
            self.assertEqual(isoline["geometry"]["coordinates"], [ISOLINE_RING])
            self.assertEqual(isoline["properties"]["kind"], "LIVE")
            tools = [step["tool"] for step in trace["steps"]]
            self.assertEqual(
                tools[:3],
                ["geocode_place", "search_nearby_places", "travel_time_isoline"],
            )
            self.assertEqual(
                tools[3:],
                ["analyze_terrain", "analyze_surface_water", "score_susceptibility"],
            )

    def test_missing_key_fails_place_and_skips_http(self):
        def boom(url, timeout=40.0):
            raise AssertionError(url)

        with patch.dict("os.environ", {"GEOAPIFY_API_KEY": ""}, clear=False):
            code, _stdout, stderr = self._main(["--place", PLACE, "--out", "/tmp/geo-flood-no-key"], boom)
        self.assertEqual(code, 1)
        self.assertIn("GEOAPIFY_API_KEY", stderr)

    def test_demo_stays_offline(self):
        def boom(url, timeout=40.0):
            raise AssertionError("demo called Geoapify: " + url)

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            code, _stdout, stderr = self._main(["--demo", "--out", str(out)], boom)
            self.assertEqual(code, 0, stderr)
            for name in (
                "flood_screening_report.md",
                "summary.json",
                "tool_trace.json",
                "aoi_screening.geojson",
            ):
                text = (out / name).read_text(encoding="utf-8")
                self.assertNotIn("geoapify", text.lower())
                self.assertNotIn("Geoapify", text)
            summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["composite_score_0_100"], 45.1)
            self.assertTrue(all(layer["is_mock"] for layer in summary["layers"]))

    def test_list_tools_names_three_geoapify_tools(self):
        def boom(url, timeout=40.0):
            raise AssertionError(url)

        code, stdout, stderr = self._main(["--list-tools"], boom)
        self.assertEqual(code, 0, stderr)
        for name in ("geocode_place", "search_nearby_places", "travel_time_isoline"):
            self.assertIn(name, stdout.splitlines())

    def test_place_conflicts_with_demo(self):
        def boom(url, timeout=40.0):
            raise AssertionError(url)

        code, _stdout, stderr = self._main(["--demo", "--place", PLACE], boom)
        self.assertEqual(code, 2)
        self.assertIn("--place", stderr)

    def test_no_isoline_skips_third_call(self):
        calls = []

        def handler(url, timeout=40.0):
            calls.append(urllib.parse.urlparse(url).path)
            return _route(url, timeout)

        with TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"GEOAPIFY_API_KEY": KEY}, clear=False):
                code, _stdout, stderr = self._main(
                    ["--place", PLACE, "--mode", "demo", "--no-isoline", "--out", tmp],
                    handler,
                )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(len(calls), 2)
            self.assertFalse(any(path.endswith("/isoline") for path in calls))
            summary = json.loads((Path(tmp) / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["geoapify"]["isoline"]["kind"], "SKIPPED")
            geojson = json.loads((Path(tmp) / "aoi_screening.geojson").read_text(encoding="utf-8"))
            roles = [f["properties"].get("role") for f in geojson["features"]]
            self.assertNotIn("geoapify_isoline", roles)


if __name__ == "__main__":
    unittest.main()
