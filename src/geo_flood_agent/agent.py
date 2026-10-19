"""Flood screening agent.

Default planner: a fixed tool sequence. No API key, no network beyond what
`--mode live` already does. Reproducible.

Optional planner: OpenAI-compatible tool calling (`--llm` or GEO_FLOOD_LLM=1).
The model chooses tools and may draft the narrative. Scores still come only
from tools. If the endpoint fails, or the draft cites numbers that are not in
the tool outputs, the run falls back to the deterministic planner / narrative.

The tool-call trace is written to tool_trace.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from geo_flood_agent.aoi import AOI, aoi_from_bbox, aoi_from_geojson
from geo_flood_agent.cog import TransferStats
from geo_flood_agent.config import Settings, default_output_dir
from geo_flood_agent.figures import write_screening_figures
from geo_flood_agent.llm import audit_numbers, run_tool_loop
from geo_flood_agent.narrative import deterministic_narrative
from geo_flood_agent.report import write_markdown_report
from geo_flood_agent.screening import (
    ScreeningResult,
    __version_product__,
    run_screening,
    screening_to_geojson,
)

ToolFn = Callable[..., Any]

LLM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "analyze_terrain",
            "description": (
                "Compute terrain flood indicators for the current AOI. "
                "Live mode reads Copernicus GLO-30 and returns HAND and slope on non-permanent land. "
                "Demo mode returns the MOCK terrain proxy. Do not invent elevations."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_surface_water",
            "description": (
                "Read JRC Global Surface Water occurrence for the current AOI. "
                "Permanent water is masked and is not flood-prone land. "
                "Demo mode returns the MOCK hydrology proxy."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "score_susceptibility",
            "description": (
                "Return the composite susceptibility and per-layer scores already computed from "
                "terrain and surface-water tools. Does not invent numbers."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


@dataclass
class AgentRunResult:
    aoi: AOI
    screening: ScreeningResult
    report_path: Path
    geojson_path: Path
    summary_json_path: Path
    figure_paths: list[Path]
    trace_path: Path | None = None
    narrative: dict[str, str] = field(default_factory=dict)


def _layer_payload(result: ScreeningResult) -> list[dict]:
    return [
        {
            "name": ly.name,
            "score_0_100": ly.score_0_100,
            "label": ly.label,
            "is_mock": ly.is_mock,
            "data_source": ly.data_source,
            "notes_en": ly.notes_en,
            "extras": ly.extras,
        }
        for ly in result.layers
    ]


class FloodScreeningAgent:
    """Tool registry plus deterministic and LLM planners."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.transfer = TransferStats()
        self._aoi: AOI | None = None
        self._screening: ScreeningResult | None = None
        self.tools: dict[str, ToolFn] = {
            "validate_aoi_geojson": self._tool_validate_geojson,
            "validate_aoi_bbox": self._tool_validate_bbox,
            "run_flood_screening": self._tool_screen,
            "analyze_terrain": self._tool_analyze_terrain,
            "analyze_surface_water": self._tool_analyze_surface_water,
            "score_susceptibility": self._tool_score,
            "write_report": self._tool_write_report,
            "fetch_open_elevation": self._tool_fetch_elevation,
        }

    def list_tools(self) -> list[str]:
        return sorted(self.tools.keys())

    def _tool_validate_geojson(self, path_or_json: str | Path | dict) -> AOI:
        aoi = aoi_from_geojson(path_or_json)
        self._aoi = aoi
        return aoi

    def _tool_validate_bbox(self, west: float, south: float, east: float, north: float) -> AOI:
        aoi = aoi_from_bbox(west, south, east, north)
        self._aoi = aoi
        return aoi

    def _ensure(self) -> ScreeningResult:
        if self._screening is None:
            if self._aoi is None:
                raise RuntimeError("AOI is not validated")
            self._screening = run_screening(
                self._aoi,
                mode=self.settings.mode,
                open_elevation_url=self.settings.open_elevation_url,
                transfer_stats=self.transfer,
            )
        return self._screening

    def _tool_screen(self, aoi: AOI | None = None) -> ScreeningResult:
        if aoi is not None:
            self._aoi = aoi
            self._screening = None
        return self._ensure()

    def _tool_analyze_terrain(self) -> dict:
        result = self._ensure()
        terrain_names = {"terrain_hand", "terrain_dem_samples", "terrain_flatness_proxy"}
        layer = next(ly for ly in result.layers if ly.name in terrain_names)
        return {
            "tool": "analyze_terrain",
            "mode": result.mode,
            "layer": layer.name,
            "score_0_100": layer.score_0_100,
            "label": layer.label,
            "is_mock": layer.is_mock,
            "data_source": layer.data_source,
            "extras": layer.extras,
            "terrain_summary": result.terrain_summary,
            "parameters": result.parameters,
        }

    def _tool_analyze_surface_water(self) -> dict:
        result = self._ensure()
        names = {"surface_water", "hydrology_proxy"}
        layer = next(ly for ly in result.layers if ly.name in names)
        return {
            "tool": "analyze_surface_water",
            "mode": result.mode,
            "layer": layer.name,
            "score_0_100": layer.score_0_100,
            "label": layer.label,
            "is_mock": layer.is_mock,
            "data_source": layer.data_source,
            "extras": layer.extras,
            "water_summary": result.water_summary,
            "parameters": result.parameters,
        }

    def _tool_score(self) -> dict:
        result = self._ensure()
        comp = next(ly for ly in result.layers if ly.name == "inundation_susceptibility")
        return {
            "tool": "score_susceptibility",
            "mode": result.mode,
            "composite_score_0_100": result.composite_score_0_100,
            "composite_label": result.composite_label,
            "composite_is_mock": comp.is_mock,
            "composite_data_source": comp.data_source,
            "aoi_area_km2": result.aoi_area_km2,
            "aoi_bounds": list(result.aoi_bounds),
            "aoi_centroid": list(result.aoi_centroid),
            "layers": [
                {
                    "name": ly.name,
                    "score_0_100": ly.score_0_100,
                    "label": ly.label,
                    "is_mock": ly.is_mock,
                }
                for ly in result.layers
            ],
            "parameters": result.parameters,
        }

    def _tool_fetch_elevation(self, aoi: AOI | None = None) -> dict:
        from geo_flood_agent.elevation import fetch_elevation_stats

        target = aoi or self._aoi
        if target is None:
            raise RuntimeError("AOI is not validated")
        return fetch_elevation_stats(target, open_elevation_url=self.settings.open_elevation_url).to_dict()

    def _execute_llm_tool(self, args: dict) -> dict:
        name = args.get("__name__")
        if name == "analyze_terrain":
            return self._tool_analyze_terrain()
        if name == "analyze_surface_water":
            return self._tool_analyze_surface_water()
        if name == "score_susceptibility":
            return self._tool_score()
        raise KeyError(f"Unknown tool {name}")

    def _deterministic_steps(self) -> list[dict]:
        steps = []
        for name, fn in (
            ("analyze_terrain", self._tool_analyze_terrain),
            ("analyze_surface_water", self._tool_analyze_surface_water),
            ("score_susceptibility", self._tool_score),
        ):
            result = fn()
            steps.append(
                {
                    "round": 0,
                    "tool": name,
                    "arguments": {},
                    "ok": True,
                    "error": None,
                    "result": result,
                    "planner": "deterministic",
                }
            )
        return steps

    def _plan(self) -> tuple[dict[str, str], dict]:
        """Return (narrative, trace). Screening is cached on self."""
        det_steps = self._deterministic_steps()
        narrative = deterministic_narrative(self._ensure())
        trace: dict[str, Any] = {
            "planner": "deterministic",
            "llm_requested": bool(self.settings.use_llm),
            "llm_model": None,
            "llm_base_url": None,
            "fallback_reason": None,
            "steps": det_steps,
            "narrative_source": "deterministic",
            "number_audit_rejected": [],
            "llm_draft": None,
        }
        if not self.settings.use_llm:
            return narrative, trace

        base = self.settings.openai_base_url
        key = self.settings.openai_api_key
        if not key:
            host = base.lower()
            if "api.openai.com" in host:
                trace["fallback_reason"] = "GEO_FLOOD_LLM / --llm set but OPENAI_API_KEY is empty"
                return narrative, trace
            key = "ollama"

        aoi = self._aoi
        assert aoi is not None
        system = (
            "You are the planner for a flood-screening tool. "
            "Call analyze_terrain, analyze_surface_water, and score_susceptibility. "
            "Then write a short bilingual narrative (English, then 中文). "
            "Use ONLY numbers that appear in tool results. Do not invent measurements, "
            "areas, elevations, or scores. If a quantity was not returned, omit it. "
            "The tables are authoritative; your narrative is a reading of those numbers. "
            "State that permanent water is excluded from the land score when the tools say so. "
            "State that this is not a hydraulic flood model."
        )
        user = (
            f"AOI bounds (W,S,E,N)={list(aoi.bounds)} centroid={list(aoi.centroid)} "
            f"area_km2={round(aoi.approx_area_km2(), 4)} mode={self.settings.mode}. "
            "Plan the screening with the tools, then draft the narrative."
        )
        # The deterministic calls already filled the cache. Reset is wrong — the LLM
        # should see the same measurements. Tools hit the cache, so the model still
        # "calls" them and receives the real payloads. That is the trace we want
        # when the LLM is driving the loop. Drop the deterministic steps if the
        # LLM loop succeeds, otherwise keep them.
        try:
            # Re-bind a fresh view: tools return cached screening (already computed).
            loop = run_tool_loop(
                base_url=base,
                api_key=key,
                model=self.settings.openai_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                tools_schema=LLM_TOOLS,
                execute=self._execute_llm_tool,
                timeout=self.settings.llm_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            trace["fallback_reason"] = f"{type(exc).__name__}: {exc}"
            trace["llm_model"] = self.settings.openai_model
            trace["llm_base_url"] = base
            return narrative, trace

        llm_steps = loop["steps"]
        called = {s["tool"] for s in llm_steps if s.get("ok")}
        required = {"analyze_terrain", "analyze_surface_water", "score_susceptibility"}
        missing = sorted(required - called)
        if missing:
            # Fill gaps so the trace shows the measurements even if the model skipped them.
            for step in det_steps:
                if step["tool"] in missing:
                    filled = dict(step)
                    filled["planner"] = "deterministic-fill"
                    llm_steps.append(filled)
        allowed = {
            "aoi_bounds": list(aoi.bounds),
            "aoi_centroid": list(aoi.centroid),
            "aoi_area_km2": round(aoi.approx_area_km2(), 4),
            "tool_results": [s["result"] for s in llm_steps],
            "mode": self.settings.mode,
        }
        draft = (loop.get("final") or "").strip()
        rejected = audit_numbers(draft, allowed) if draft else []
        trace.update(
            {
                "planner": "llm" if not missing else "llm+deterministic-fill",
                "llm_model": self.settings.openai_model,
                "llm_base_url": base,
                "fallback_reason": None if not missing else f"model skipped tools: {missing}",
                "steps": llm_steps,
                "llm_draft": draft,
                "number_audit_rejected": rejected,
            }
        )
        if draft and not rejected:
            narrative = {"en": draft, "zh": "", "source": "llm", "model": self.settings.openai_model}
            trace["narrative_source"] = "llm"
        else:
            trace["narrative_source"] = "deterministic"
            if rejected:
                trace["fallback_reason"] = (
                    "LLM draft rejected by number audit: " + ", ".join(rejected[:12])
                )
            elif not draft:
                trace["fallback_reason"] = "LLM returned no narrative; deterministic text kept"
        return narrative, trace

    def _tool_write_report(
        self,
        aoi: AOI,
        result: ScreeningResult,
        out_dir: Path,
        narrative: dict | None = None,
        trace: dict | None = None,
    ) -> tuple[Path, Path, Path, list[Path], Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        fig_dir = out_dir / "figures"
        figure_paths = write_screening_figures(result, fig_dir)
        report_path = write_markdown_report(
            aoi,
            result,
            out_dir / "flood_screening_report.md",
            figure_paths=figure_paths,
            narrative=narrative,
            trace=trace,
        )
        gj = screening_to_geojson(aoi, result)
        geojson_path = out_dir / "aoi_screening.geojson"
        geojson_path.write_text(json.dumps(gj, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "product": "geo-flood-agent",
            "version": __version_product__,
            "mode": result.mode,
            "planner": (trace or {}).get("planner", "deterministic"),
            "narrative_source": (trace or {}).get("narrative_source", "deterministic"),
            "llm_enabled": (trace or {}).get("narrative_source") == "llm",
            "llm_requested": bool(self.settings.use_llm),
            "composite_score_0_100": result.composite_score_0_100,
            "composite_label": result.composite_label,
            "aoi_bounds": list(result.aoi_bounds),
            "aoi_centroid": list(result.aoi_centroid),
            "aoi_area_km2_approx": result.aoi_area_km2,
            "tools_available": self.list_tools(),
            "layers": _layer_payload(result),
            "terrain_summary": result.terrain_summary,
            "water_summary": result.water_summary,
            "parameters": result.parameters,
            "transfers": result.transfers,
            "figures": [str(p.name) for p in figure_paths],
            "note": "Layer tables are authoritative. MOCK layers are excluded from the live composite.",
        }
        if result.elevation:
            summary["elevation_ok"] = result.elevation.get("ok")
            summary["elevation_provider"] = result.elevation.get("provider")
            summary["elev_mean_m"] = result.elevation.get("elev_mean_m")
            summary["hand_land_median_m"] = result.elevation.get("hand_land_median_m")
        summary_json_path = out_dir / "summary.json"
        summary_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        trace_path = out_dir / "tool_trace.json"
        trace_path.write_text(json.dumps(trace or {}, ensure_ascii=False, indent=2), encoding="utf-8")
        return report_path, geojson_path, summary_json_path, figure_paths, trace_path

    def run(
        self,
        *,
        geojson: str | Path | dict | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        out_dir: Path | None = None,
    ) -> AgentRunResult:
        self._screening = None
        self.transfer = TransferStats()
        if geojson is not None:
            aoi = self.tools["validate_aoi_geojson"](geojson)
        elif bbox is not None:
            aoi = self.tools["validate_aoi_bbox"](*bbox)
        else:
            raise ValueError("Provide geojson= or bbox=(w,s,e,n)")

        narrative, trace = self._plan()
        screening = self._ensure()
        # Transfer counter was passed into run_screening; copy the final tally
        # if the screening object was created with that counter.
        if screening.transfers is not None:
            screening.transfers["http_get_bytes"] = self.transfer.http_get_bytes
            screening.transfers["largest_head_content_length"] = self.transfer.http_head_bytes_reported
        out = Path(out_dir) if out_dir else default_output_dir()
        report_path, geojson_path, summary_json_path, figure_paths, trace_path = self.tools["write_report"](
            aoi, screening, out, narrative, trace
        )
        return AgentRunResult(
            aoi=aoi,
            screening=screening,
            report_path=report_path,
            geojson_path=geojson_path,
            summary_json_path=summary_json_path,
            figure_paths=figure_paths,
            trace_path=trace_path,
            narrative=narrative,
        )

    def maybe_llm_polish(self, report_markdown: str) -> str:
        """Kept for callers. Narrative drafting happens inside run(), not by rewriting the report."""
        return report_markdown
