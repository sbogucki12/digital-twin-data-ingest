"""Local, gitignored cost observability for Anthropic API calls.

Every Anthropic response's `usage` is recorded to a local SQLite ledger with an
estimated USD cost, so spend is visible per call and per run.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, Field

MTOK = 1_000_000


class Pricing(BaseModel):
    """USD per million tokens. Cache rates are multipliers of the input rate."""

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    cache_write_5m_multiplier: float = 1.25
    cache_write_1h_multiplier: float = 2.0
    cache_read_multiplier: float = 0.1
    # Optional second rate card for long prompts (e.g. Haiku 5.5 above 100K tokens).
    long_context_threshold_tokens: int | None = None
    long_input_per_mtok: float | None = None
    long_output_per_mtok: float | None = None


# Published rates as of 2026-11. Override in config if Anthropic's pricing changes.
DEFAULT_PRICING: dict[str, Pricing] = {
    "claude-haiku-5-5": Pricing(
        input_per_mtok=0.10,
        output_per_mtok=0.50,
        long_context_threshold_tokens=100_000,
        long_input_per_mtok=0.50,
        long_output_per_mtok=2.50,
    ),
}


@dataclass(frozen=True)
class CallCost:
    input_tokens: int
    output_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    cache_read_tokens: int
    long_context: bool
    cost_usd: float


def _int(obj: Any, name: str) -> int:
    value = getattr(obj, name, None) if not isinstance(obj, dict) else obj.get(name)
    return int(value or 0)


def compute_cost(usage: Any, pricing: Pricing) -> CallCost:
    """Estimate cost from an Anthropic `usage` object (or an equivalent dict).

    Thinking tokens are already included in `output_tokens` by the API.
    """
    input_tokens = _int(usage, "input_tokens")
    output_tokens = _int(usage, "output_tokens")
    cache_read = _int(usage, "cache_read_input_tokens")

    breakdown = (
        usage.get("cache_creation") if isinstance(usage, dict) else getattr(usage, "cache_creation", None)
    )
    if breakdown:
        w5 = _int(breakdown, "ephemeral_5m_input_tokens")
        w1h = _int(breakdown, "ephemeral_1h_input_tokens")
    else:  # older shape: one total, assume 5-minute TTL
        w5, w1h = _int(usage, "cache_creation_input_tokens"), 0

    prompt_tokens = input_tokens + w5 + w1h + cache_read
    long_ctx = bool(
        pricing.long_context_threshold_tokens
        and prompt_tokens > pricing.long_context_threshold_tokens
    )
    in_rate = (pricing.long_input_per_mtok if long_ctx else None) or pricing.input_per_mtok
    out_rate = (pricing.long_output_per_mtok if long_ctx else None) or pricing.output_per_mtok

    cost = (
        input_tokens * in_rate
        + w5 * in_rate * pricing.cache_write_5m_multiplier
        + w1h * in_rate * pricing.cache_write_1h_multiplier
        + cache_read * in_rate * pricing.cache_read_multiplier
        + output_tokens * out_rate
    ) / MTOK
    return CallCost(input_tokens, output_tokens, w5, w1h, cache_read, long_ctx, round(cost, 8))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc           TEXT    NOT NULL,
    run_id           TEXT    NOT NULL,
    doc_id           TEXT,
    model            TEXT    NOT NULL,
    input_tokens     INTEGER NOT NULL,
    output_tokens    INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_read_tokens  INTEGER NOT NULL,
    long_context     INTEGER NOT NULL,
    cost_usd         REAL    NOT NULL,
    stop_reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_calls_run ON api_calls(run_id);
"""


def _restrict(path: Path, mode: int) -> None:
    """Owner-only permissions on POSIX. On Windows, files under the user profile
    already inherit an owner-only ACL; chmod there only toggles read-only."""
    if os.name == "posix":
        os.chmod(path, mode)


class CostLedger:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _restrict(db_path.parent, 0o700)
        self._conn = sqlite3.connect(db_path)
        _restrict(db_path, 0o600)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def record(
        self,
        *,
        run_id: str,
        model: str,
        usage: Any,
        pricing: Pricing,
        doc_id: str | None = None,
        stop_reason: str | None = None,
    ) -> CallCost:
        c = compute_cost(usage, pricing)
        self._conn.execute(
            "INSERT INTO api_calls (ts_utc, run_id, doc_id, model, input_tokens, output_tokens,"
            " cache_write_tokens, cache_read_tokens, long_context, cost_usd, stop_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.now(UTC).isoformat(timespec="seconds"),
                run_id, doc_id, model, c.input_tokens, c.output_tokens,
                c.cache_write_5m_tokens + c.cache_write_1h_tokens, c.cache_read_tokens,
                int(c.long_context), c.cost_usd, stop_reason,
            ),
        )
        self._conn.commit()
        return c

    def run_summaries(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT run_id, MIN(ts_utc), COUNT(*), SUM(input_tokens + cache_write_tokens"
            " + cache_read_tokens), SUM(output_tokens), SUM(cost_usd)"
            " FROM api_calls GROUP BY run_id ORDER BY MIN(ts_utc) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        keys = ("run_id", "started_utc", "calls", "input_tokens", "output_tokens", "cost_usd")
        return [dict(zip(keys, r)) for r in rows]

    def run_total(self, run_id: str) -> float:
        (total,) = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM api_calls WHERE run_id = ?", (run_id,)
        ).fetchone()
        return float(total)

    def total_cost(self) -> float:
        (total,) = self._conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM api_calls").fetchone()
        return float(total)
