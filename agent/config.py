"""Settings, read once from the environment (and `.env`, which is git-ignored)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SEED_WAREHOUSE = ROOT / "seed" / "warehouse_seed.duckdb"
SEED_DBT_TARGET = ROOT / "seed" / "dbt_target"
DEFAULT_WAREHOUSE = ROOT.parent / "telemetry-data-platform" / "data" / "warehouse.duckdb"
DEFAULT_DBT_TARGET = ROOT.parent / "telemetry-data-platform" / "dbt" / "target"

log = logging.getLogger(__name__)

EFFORTS = ("low", "medium", "high")

# USD per million tokens, from the claude-api skill's model table (checked 2026-11-24).
# cache_write is the 5-minute TTL price (1.25x input); cache_read is the model's own rate.
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.10},
    # Haiku 5.5 prices for prompts <= 100K tokens (ours are far below that)
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01},
}

# Models that accept the server-side refusal fallback with fallbacks="default".
FALLBACK_MODELS = {"claude-opus-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    use_fallback: bool = True
    max_tokens: int = 16000
    max_turns: int = 8
    max_sql_errors: int = 3
    schema_retrieval: bool = True  # False = ablation: every schema pasted into the prompt
    warehouse_path: Path = DEFAULT_WAREHOUSE
    dbt_target_dir: Path = DEFAULT_DBT_TARGET
    expose_staging: bool = False
    query_timeout_s: float = 10.0
    row_limit: int = 1000  # LIMIT injected into the outer query when absent
    result_rows: int = 50  # rows returned to the model per run_sql call
    error_chars: int = 1000  # DuckDB error text returned to the model is truncated to this
    extra: dict = field(default_factory=dict)

    @property
    def using_seed(self) -> bool:
        return self.warehouse_path.resolve() == SEED_WAREHOUSE.resolve()

    def with_(self, **kw) -> Settings:
        return replace(self, **kw)


def load_settings(**overrides) -> Settings:
    load_dotenv(ROOT / ".env", override=False)
    effort = os.getenv("AGENT_EFFORT", "medium").strip().lower()
    if effort not in EFFORTS:
        raise ValueError(f"AGENT_EFFORT must be one of {EFFORTS}, got {effort!r}")
    warehouse = Path(os.getenv("WAREHOUSE_PATH") or DEFAULT_WAREHOUSE)
    target = Path(os.getenv("DBT_TARGET_DIR") or DEFAULT_DBT_TARGET)
    if not warehouse.is_absolute():
        warehouse = (ROOT / warehouse).resolve()
    if not target.is_absolute():
        target = (ROOT / target).resolve()
    if not warehouse.exists() and SEED_WAREHOUSE.exists():
        log.warning("warehouse %s not found; using the seed warehouse (make seed)", warehouse)
        warehouse, target = SEED_WAREHOUSE, SEED_DBT_TARGET
    s = Settings(
        model=os.getenv("CLAUDE_MODEL", "claude-opus-5-5").strip(),
        effort=effort,
        use_fallback=_bool("CLAUDE_FALLBACK", True),
        max_tokens=_int("CLAUDE_MAX_TOKENS", 16000),
        max_turns=_int("MAX_TURNS", 8),
        max_sql_errors=_int("MAX_SQL_ERRORS", 3),
        schema_retrieval=_bool("SCHEMA_RETRIEVAL", True),
        warehouse_path=warehouse,
        dbt_target_dir=target,
        expose_staging=_bool("EXPOSE_STAGING", False),
    )
    return s.with_(**overrides) if overrides else s


def seed_settings(**overrides) -> Settings:
    """Settings pointing at the seed warehouse (tests, CI, and work before the platform)."""
    return load_settings().with_(
        warehouse_path=SEED_WAREHOUSE, dbt_target_dir=SEED_DBT_TARGET, **overrides
    )
