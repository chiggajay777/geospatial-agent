"""Runtime configuration from env / CLI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    mode: str  # demo | mock | live
    openai_api_key: str | None
    openai_base_url: str
    openai_model: str
    open_elevation_url: str | None
    use_llm: bool
    llm_timeout_s: float
    cache_dir: Path


def get_settings(mode_override: str | None = None, *, use_llm: bool | None = None) -> Settings:
    mode = (mode_override or os.getenv("GEO_FLOOD_MODE") or "demo").strip().lower()
    if mode not in {"demo", "mock", "live"}:
        mode = "demo"
    if use_llm is None:
        use_llm = _env_flag("GEO_FLOOD_LLM")
    cache = Path(os.getenv("GEO_FLOOD_CACHE") or (Path.cwd() / ".cache" / "geo_flood_agent"))
    try:
        timeout = float(os.getenv("GEO_FLOOD_LLM_TIMEOUT") or "90")
    except ValueError:
        timeout = 90.0
    return Settings(
        mode=mode,
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        open_elevation_url=os.getenv("OPEN_ELEVATION_URL") or None,
        use_llm=bool(use_llm),
        llm_timeout_s=timeout,
        cache_dir=cache,
    )


def default_output_dir() -> Path:
    return Path.cwd() / "output"
