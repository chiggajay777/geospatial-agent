"""Offline tests: HAND on a synthetic valley, metrics, mock scores, LLM tool loop."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from geo_flood_agent.aoi import aoi_from_bbox
from geo_flood_agent.hydrology import slope_degrees, terrain_concern, terrain_derivatives
from geo_flood_agent.llm import audit_numbers, run_tool_loop
from geo_flood_agent.metrics import auroc, average_precision
from geo_flood_agent.scoring import summarize_aoi
from geo_flood_agent.screening import run_screening


class HydrologyTests(unittest.TestCase):
    def test_valley_hand_increases_upslope(self):
        rows, cols = 61, 80
        r = np.arange(rows)[:, None]
        c = np.arange(cols)[None, :]
        # Channel along the center row, draining toward the right. Walls rise 3 m per row.
        dem = 0.05 * (cols - 1 - c) + 3.0 * np.abs(r - 30)
        dem = dem.astype(np.float64)
        # A cell beside the channel, dug below the thalweg. HAND must clip at 0
        # (it cannot be a negative depth just because the raw DEM is below the outlet).
        dem[31, 40] = -20.0
        out = terrain_derivatives(dem, cell_m=30.0, stream_area_km2=0.05)
        hand = out["hand"]
        self.assertGreater(out["accumulation"].max(), 100)
        channel = hand[30, 20:70]
        ridge = hand[0:3, 20:70]
        self.assertTrue(np.isfinite(channel).mean() > 0.8)
        # One row off the thalweg is 3 m up, so the channel row itself should sit near 0.
        self.assertLess(float(np.nanmedian(channel)), 1.0)
        self.assertGreater(float(np.nanmedian(ridge)), 50.0)
        self.assertGreaterEqual(float(hand[31, 40]), 0.0)
        self.assertLess(float(hand[31, 40]), 1.0)

    def test_slope_of_a_plane(self):
        cell = 30.0
        theta = 10.0  # degrees
        rise = cell * np.tan(np.radians(theta))
        rows = np.arange(40)[:, None]
        dem = rows * rise
        dem = np.broadcast_to(dem, (40, 40)).copy()
        slope = slope_degrees(dem, np.ones_like(dem, dtype=bool), cell)
        # Interior pixels avoid the one-sided gradient at the border.
        interior = slope[5:-5, 5:-5]
        self.assertAlmostEqual(float(np.mean(interior)), theta, delta=0.5)

    def test_permanent_water_excluded_from_mean(self):
        dem = np.zeros((6, 6))
        hand = np.zeros((6, 6))
        slope = np.zeros((6, 6))
        terrain = terrain_concern(hand, slope)
        inside = np.ones((6, 6), dtype=bool)
        occurrence = np.zeros((6, 6))
        permanent = np.zeros((6, 6), dtype=bool)
        permanent[:, 0] = True
        occurrence[:, 0] = 100
        # Land is dry and flat at HAND 0, so terrain concern is 100.
        # Permanent column must not change that land mean, and must be counted apart.
        summary = summarize_aoi(
            dem=dem,
            hand=hand,
            slope=slope,
            terrain=terrain,
            inside=inside,
            occurrence=occurrence,
            permanent=permanent,
            res_m=30.0,
        )
        self.assertEqual(summary["permanent_water_pixels"], 6)
        self.assertEqual(summary["land_pixels_scored"], 30)
        self.assertAlmostEqual(summary["terrain_score_land"]["mean"], 100.0, places=3)
        self.assertEqual(summary["hand_lt_2m_permanent_pixels"], 6)


class MetricTests(unittest.TestCase):
    def test_auroc_extremes(self):
        y = np.array([0, 0, 1, 1])
        self.assertAlmostEqual(auroc(y, np.array([0.1, 0.2, 0.8, 0.9])), 1.0)
        self.assertAlmostEqual(auroc(y, np.array([0.9, 0.8, 0.2, 0.1])), 0.0)
        self.assertAlmostEqual(auroc(y, np.array([0.5, 0.5, 0.5, 0.5])), 0.5)
        self.assertIsNone(auroc(np.array([1, 1]), np.array([0.2, 0.3])))

    def test_average_precision_perfect(self):
        y = np.array([0, 1, 0, 1])
        s = np.array([0.1, 0.9, 0.2, 0.8])
        self.assertAlmostEqual(average_precision(y, s), 1.0)


class MockScreeningTests(unittest.TestCase):
    def test_demo_sample_scores_stable(self):
        aoi = aoi_from_bbox(120.10, 30.22, 120.18, 30.28)
        result = run_screening(aoi, mode="demo")
        scores = {ly.name: ly.score_0_100 for ly in result.layers}
        self.assertEqual(scores["hydrology_proxy"], 39.1)
        self.assertEqual(scores["terrain_flatness_proxy"], 54.6)
        self.assertEqual(scores["inundation_susceptibility"], 45.1)
        self.assertTrue(all(ly.is_mock for ly in result.layers))
        self.assertIsNone(result.rasters)


class NumberAuditTests(unittest.TestCase):
    def test_rounding_allowed_invention_rejected(self):
        payload = {"hand_m": 12.04, "score": 39.1}
        self.assertEqual(audit_numbers("HAND is 12.0 m and the score is 39.1.", payload), [])
        bad = audit_numbers("The secret depth is 77.7 m.", payload)
        self.assertIn("77.7", bad)


class LLMLoopTests(unittest.TestCase):
    def test_tool_loop_against_local_server(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode())
                messages = body["messages"]
                # Count tool results already in the thread.
                n_tools = sum(1 for m in messages if m.get("role") == "tool")
                if n_tools == 0:
                    response = {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {"name": "analyze_terrain", "arguments": "{}"},
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                else:
                    # Echo the score the tool just returned. No invented numbers.
                    last = json.loads(messages[-1]["content"])
                    score = last["score_0_100"]
                    response = {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": f"Terrain score is {score}.",
                                    "tool_calls": [],
                                }
                            }
                        ]
                    }
                raw = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]

            def execute(args):
                self.assertEqual(args["__name__"], "analyze_terrain")
                return {"score_0_100": 41.5, "is_mock": False}

            out = run_tool_loop(
                base_url=f"http://127.0.0.1:{port}/v1",
                api_key="test",
                model="fake",
                messages=[{"role": "user", "content": "screen"}],
                tools_schema=[
                    {
                        "type": "function",
                        "function": {
                            "name": "analyze_terrain",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                execute=execute,
                timeout=10,
            )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(len(out["steps"]), 1)
        self.assertEqual(out["steps"][0]["result"]["score_0_100"], 41.5)
        self.assertIn("41.5", out["final"])
        self.assertEqual(audit_numbers(out["final"], out["steps"][0]["result"]), [])


class DemoCLITests(unittest.TestCase):
    def test_demo_writes_trace(self):
        from geo_flood_agent.cli import main

        out = Path("output/test_demo")
        code = main(["--demo", "--out", str(out)])
        self.assertEqual(code, 0)
        summary = json.loads((out / "summary.json").read_text())
        self.assertEqual(summary["version"], "0.3.0")
        self.assertEqual(summary["planner"], "deterministic")
        self.assertFalse(summary["llm_enabled"])
        trace = json.loads((out / "tool_trace.json").read_text())
        tools = [s["tool"] for s in trace["steps"]]
        self.assertEqual(tools, ["analyze_terrain", "analyze_surface_water", "score_susceptibility"])


if __name__ == "__main__":
    unittest.main()
