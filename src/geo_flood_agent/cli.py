"""CLI entry: python -m geo_flood_agent ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from geo_flood_agent import __version__
from geo_flood_agent.agent import FloodScreeningAgent
from geo_flood_agent.config import get_settings


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="geo-flood-agent",
        description=(
            "Flood / water inundation screening for a bbox or GeoJSON AOI. "
            "--demo = offline MOCK. --mode live = Copernicus GLO-30 HAND + JRC occurrence "
            "(no paid keys). --validate = Sen1Floods11 test-split skill of the terrain score."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--demo",
        action="store_true",
        help="Run built-in sample AOI with MOCK layers (no network).",
    )
    p.add_argument(
        "--aoi",
        type=str,
        default=None,
        help="Path to AOI GeoJSON (Feature / FeatureCollection / Polygon).",
    )
    p.add_argument(
        "--bbox",
        type=str,
        default=None,
        help="Bounding box as west,south,east,north (WGS84 degrees).",
    )
    p.add_argument(
        "--mode",
        choices=["demo", "mock", "live"],
        default=None,
        help="demo/mock = MOCK (offline). live = Copernicus HAND + JRC surface water.",
    )
    p.add_argument(
        "--llm",
        action="store_true",
        help=(
            "Use an OpenAI-compatible tool-calling planner "
            "(OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL). "
            "Default is the deterministic planner. Falls back if the endpoint fails "
            "or the draft cites numbers that are not in the tool outputs."
        ),
    )
    p.add_argument(
        "--validate",
        action="store_true",
        help="Score terrain susceptibility against the Sen1Floods11 hand-labeled test split.",
    )
    p.add_argument(
        "--validate-max-per-event",
        type=int,
        default=0,
        help=(
            "If >0, use only the first N test-split chips of each event (CSV order, not by score). "
            "Default 0 = every chip in the official test split."
        ),
    )
    p.add_argument(
        "--out",
        type=str,
        default=None,
        help="Output directory (default: ./output).",
    )
    p.add_argument(
        "--list-tools",
        action="store_true",
        help="Print agent tool names and exit.",
    )
    return p


def _builtin_sample_aoi() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "examples" / "sample_aoi.geojson",
        Path.cwd() / "examples" / "sample_aoi.geojson",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        "sample_aoi.geojson not found. Pass --aoi or run from the repo root."
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = "demo" if args.demo else args.mode
    settings = get_settings(mode_override=mode, use_llm=True if args.llm else None)
    agent = FloodScreeningAgent(settings)

    if args.validate:
        from geo_flood_agent.validation import run_validation

        out_dir = Path(args.out) if args.out else Path.cwd() / "output" / "validation"
        try:
            run_validation(
                out_dir,
                settings.cache_dir,
                max_per_event=args.validate_max_per_event or None,
            )
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"geo-flood-agent validation written to {out_dir}")
        return 0

    if args.list_tools:
        for name in agent.list_tools():
            print(name)
        return 0

    out_dir = Path(args.out) if args.out else Path.cwd() / "output"

    try:
        if args.demo:
            aoi_path = _builtin_sample_aoi()
            settings = get_settings(mode_override="demo", use_llm=True if args.llm else None)
            agent = FloodScreeningAgent(settings)
            result = agent.run(geojson=aoi_path, out_dir=out_dir)
        elif args.aoi:
            result = agent.run(geojson=args.aoi, out_dir=out_dir)
        elif args.bbox:
            parts = [float(x.strip()) for x in args.bbox.split(",")]
            if len(parts) != 4:
                print("ERROR: --bbox needs west,south,east,north", file=sys.stderr)
                return 2
            result = agent.run(bbox=tuple(parts), out_dir=out_dir)  # type: ignore[arg-type]
        else:
            print(
                "ERROR: provide --demo, --aoi PATH, or --bbox w,s,e,n",
                file=sys.stderr,
            )
            return 2
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    s = result.screening
    print("geo-flood-agent OK")
    print(f"  version:           {__version__}")
    print(f"  mode:              {s.mode}")
    print(f"  composite:         {s.composite_score_0_100} ({s.composite_label})")
    print(f"  area_km2_approx:   {s.aoi_area_km2}")
    print(f"  report:            {result.report_path}")
    print(f"  geojson:           {result.geojson_path}")
    print(f"  summary:           {result.summary_json_path}")
    for fp in result.figure_paths:
        print(f"  figure:            {fp}")
    live_n = sum(1 for ly in s.layers if not ly.is_mock)
    mock_n = sum(1 for ly in s.layers if ly.is_mock)
    print(f"  layers:            {live_n} LIVE / {mock_n} MOCK")
    if any(ly.is_mock for ly in s.layers):
        print("  note:              MOCK layers present — see report disclaimer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
