"""Decision-level TF-vs-SeanBot divergence (PR 2, read-only edge-finding tool).

Joins TF's own per-eval decisions (``strategy_decisions``, persisting again after
PR C) to SeanBot entries (``seanbot_signals`` ``type='entry'``) on the bar, within
a 120s at-or-before tolerance (mirrors the SeanBot reconciler), and classifies:

  * agree-enter   — SeanBot entered AND TF fired a long_signal on the same bar
  * SeanBot-only  — SeanBot entered but TF skipped (or had no decision) → the edge:
                    broken down by TF's blocking ``failed_gate``
  * TF-only       — TF fired a long_signal with no SeanBot entry on the bar
  * agree-skip    — TF saw the bar and skipped, SeanBot also did not enter

Read-only via the orchestrator's service-role ``SupabaseClient``; SELECT only, no
secret rendered. ``strategy_decisions`` only holds touch-band-passed bars (entries
+ near-misses), so TF skips here are gate failures DOWNSTREAM of touch — touch /
regime never appear as failed_gate (those bars aren't journaled).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.orchestrator import Orchestrator

LOGGER = logging.getLogger(__name__)

TOLERANCE_SEC = 120.0  # mirrors src.comparison.seanbot_reconciler._TOLERANCE_SEC
_MAX_EXAMPLES = 20

DIVERGENCE_CAVEAT = (
    "Sparse until forward decisions accumulate — pre-fix strategy_decisions rows "
    "were lost to the flush bug (fixed PR C). Different books; small sample; TF newly live."
)


@dataclass
class GateCount:
    gate: str
    count: int


@dataclass
class DivergenceExample:
    ts: str
    classification: str
    tf_decision: str | None
    tf_failed_gate: str | None


@dataclass
class Divergence:
    agree_enter: int = 0
    agree_skip: int = 0
    tf_only: int = 0
    seanbot_only: int = 0
    seanbot_only_no_tf_record: int = 0
    skip_gate_breakdown: list[GateCount] = field(default_factory=list)
    examples: list[DivergenceExample] = field(default_factory=list)
    tolerance_sec: int = int(TOLERANCE_SEC)
    caveat: str = DIVERGENCE_CAVEAT
    error: str | None = None


class DivergenceAggregator:
    """Read-only decision-level divergence classification."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orch = orchestrator

    async def divergence(self) -> Divergence:
        try:
            tf_rows = await self._orch._db.select(
                "strategy_decisions",
                filters={"order": "decision_ts.asc"},
                columns="decision_ts,decision,failed_gate",
            )
            sb_rows = await self._orch._db.select(
                "seanbot_signals",
                filters={"type": "eq.entry", "order": "ts.asc"},
                columns="ts,type",
            )
        except Exception as exc:  # noqa: BLE001 — surface as a view error, never 500
            LOGGER.warning("[DASH] divergence: read_failed — %r", exc)
            return Divergence(error=f"{type(exc).__name__}: {exc}")
        return _classify(tf_rows, sb_rows, TOLERANCE_SEC)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _nearest_at_or_before(
    tf: list[tuple[datetime, dict]], when: datetime, tolerance_sec: float
) -> dict | None:
    """The TF decision at-or-before ``when`` within tolerance (mirrors the recon)."""
    best: dict | None = None
    best_gap: float | None = None
    for dts, row in tf:
        gap = (when - dts).total_seconds()
        if 0 <= gap <= tolerance_sec and (best_gap is None or gap < best_gap):
            best, best_gap = row, gap
    return best


def _classify(tf_rows: list[dict], sb_rows: list[dict], tolerance_sec: float) -> Divergence:
    tf = [(_parse_ts(r.get("decision_ts")), r) for r in tf_rows]
    tf = [(dts, r) for dts, r in tf if dts is not None]
    out = Divergence()
    gate_counts: Counter[str] = Counter()
    matched_keys: set[int] = set()

    for sb in sb_rows:
        when = _parse_ts(sb.get("ts"))
        if when is None:
            continue
        tf_row = _nearest_at_or_before(tf, when, tolerance_sec)
        ts_str = str(sb.get("ts"))
        if tf_row is None:
            out.seanbot_only += 1
            out.seanbot_only_no_tf_record += 1
            _add_example(out, ts_str, "SeanBot-only (no TF decision)", None, None)
            continue
        matched_keys.add(id(tf_row))
        if tf_row.get("decision") == "long_signal":
            out.agree_enter += 1
            _add_example(out, ts_str, "agree-enter", "long_signal", None)
        else:
            out.seanbot_only += 1
            gate = tf_row.get("failed_gate") or "unknown"
            gate_counts[gate] += 1
            _add_example(out, ts_str, "SeanBot-only (TF skipped)", tf_row.get("decision"), gate)

    # TF decisions with no SeanBot entry on the bar.
    for _dts, row in tf:
        if id(row) in matched_keys:
            continue
        if row.get("decision") == "long_signal":
            out.tf_only += 1
            _add_example(out, str(row.get("decision_ts")), "TF-only", "long_signal", None)
        else:
            out.agree_skip += 1

    out.skip_gate_breakdown = [GateCount(gate=g, count=c) for g, c in gate_counts.most_common()]
    # Most-recent examples first, capped.
    out.examples.sort(key=lambda e: e.ts, reverse=True)
    out.examples = out.examples[:_MAX_EXAMPLES]
    return out


def _add_example(
    out: Divergence, ts: str, classification: str, decision: str | None, gate: str | None
) -> None:
    out.examples.append(
        DivergenceExample(
            ts=ts, classification=classification, tf_decision=decision, tf_failed_gate=gate
        )
    )
