"""Dashboard TF-vs-SeanBot daily P&L scoreboard (PR #71, read-only).

Compares each bot's OWN realized P&L on its OWN book — TF from ``lifecycles``
(``pnl_net``), SeanBot from ``seanbot_signals`` ``type='exit'`` rows
(``pnl_points × MNQ.multiplier × contracts``). This is "who made more money
running their own strategy", NOT a same-trade comparison: TF and SeanBot trade
different entries (TF is more selective and only went live 2026-06-01). Reads via
the orchestrator's service-role ``SupabaseClient``; SELECT only, no secret rendered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from config.instruments import MNQ
from dashboard.trades import DB_VIEW_CAVEAT, TradesAggregator

if TYPE_CHECKING:
    from src.orchestrator import Orchestrator

LOGGER = logging.getLogger(__name__)

SCOREBOARD_CAVEAT = (
    "Different books — each bot's own realized P&L, not same-trade. "
    "Small sample; TF newly live (2026-06-01). "
    "SeanBot P&L is reconstructed from captured exit alerts (same-price "
    "re-announcements deduped) — an estimate that may diverge from SeanBot's own "
    "daily summary; treat the leader as indicative, not authoritative. " + DB_VIEW_CAVEAT
)


@dataclass
class ScoreRow:
    day: str
    tf_pnl: float
    sb_pnl: float
    delta: float  # tf - sb (positive → TF made more / lost less that day)
    winner: str  # "TF" | "SeanBot" | "tie"
    tf_cum: float
    sb_cum: float
    delta_cum: float


@dataclass
class Scoreboard:
    rows: list[ScoreRow]  # newest day first
    tf_total: float
    sb_total: float
    delta_total: float
    leader: str  # "TF" | "SeanBot" | "tie"
    headline: str
    caveat: str = SCOREBOARD_CAVEAT
    error: str | None = None


class ScoreboardAggregator:
    """TF-vs-SeanBot daily realized-P&L comparison, read-only."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orch = orchestrator
        self._trades = TradesAggregator(orchestrator)

    async def scoreboard(self) -> Scoreboard:
        tf = await self._trades.daily_pnl()
        if tf.error:
            return _error_board(f"TF read failed: {tf.error}")
        try:
            sb_rows = await self._orch._db.select(
                "seanbot_signals",
                filters={"type": "eq.exit", "order": "ts.desc"},
                columns="pnl_points,contracts,ts,type,price,message_id",
            )
        except Exception as exc:  # noqa: BLE001 — surface as a view error, never 500
            LOGGER.warning("[DASH] scoreboard: seanbot read_failed — %r", exc)
            return _error_board(f"SeanBot read failed: {type(exc).__name__}: {exc}")

        tf_by_day = {d.day: d.pnl_net for d in tf.days}
        sb_by_day = _aggregate_seanbot_daily(sb_rows)
        return _build_scoreboard(tf_by_day, sb_by_day)


# SeanBot's book is a fixed 2-contract position: it can close at most ONCE at a
# given price. Multiple ``exit`` rows at the SAME price within this window are the
# same close re-announced by SeanBot's reconciler (each post recomputes P&L, so
# the points differ — e.g. the 2026-06-01 13:22:18/13:22:53/13:22:53 @30365.25
# trio, or the 06-02 19:57:45/19:58:05 @30694.25 exact +49/+49 pair). Summing all
# of them over-captured realized P&L ~3.3× and produced the bogus "TF leads"
# headline (§0.5.197 / §7.3). We collapse each same-price burst to one event.
_DEDUP_WINDOW_SECONDS = 120.0


def _parse_ts(value: Any) -> datetime | None:
    """Parse a ``seanbot_signals.ts`` ISO string (``...+00:00``) → aware datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _dedup_seanbot_exits(
    rows: list[dict[str, Any]],
    window_seconds: float = _DEDUP_WINDOW_SECONDS,
) -> list[dict[str, Any]]:
    """Collapse same-close re-announcements: ``exit`` rows sharing a price whose
    timestamps fall within ``window_seconds`` of the prior kept row at that price
    are the SAME close. Keep the LAST (latest-ts) row — SeanBot's settled recompute.

    Rows with no parseable price/ts are passed through unchanged (they can't be
    proven duplicates and the caller's value-guard drops malformed ones anyway).
    Pure + deterministic; logs which row won each collapse (handoff §11).
    """
    exits = [r for r in rows if str(r.get("type") or "") == "exit"]
    exits.sort(key=lambda r: str(r.get("ts") or ""))  # oldest first

    kept: list[dict[str, Any]] = []
    last_idx_by_price: dict[float, int] = {}
    for row in exits:
        price = _f(row.get("price"))
        ts = _parse_ts(row.get("ts"))
        if price is None or ts is None:
            kept.append(row)
            continue
        prev_idx = last_idx_by_price.get(price)
        if prev_idx is not None:
            prev_ts = _parse_ts(kept[prev_idx].get("ts"))
            if prev_ts is not None and (ts - prev_ts).total_seconds() <= window_seconds:
                LOGGER.debug(
                    "[DASH] scoreboard dedup: collapsed re-announced exit — "
                    "price=%s dropped_msg=%s dropped_pnl_pts=%s kept_msg=%s kept_pnl_pts=%s",
                    price,
                    kept[prev_idx].get("message_id"),
                    kept[prev_idx].get("pnl_points"),
                    row.get("message_id"),
                    row.get("pnl_points"),
                )
                kept[prev_idx] = row  # keep the later (settled) recompute
                continue
        last_idx_by_price[price] = len(kept)
        kept.append(row)
    return kept


def _aggregate_seanbot_daily(rows: list[dict[str, Any]]) -> dict[str, float]:
    """SeanBot realized $ per UTC day: sum(pnl_points × multiplier × contracts),
    after collapsing same-close re-announcements so each exit counts ONCE."""
    by_day: dict[str, float] = {}
    for row in _dedup_seanbot_exits(rows):
        ts = row.get("ts")
        pts = _f(row.get("pnl_points"))
        contracts = _i(row.get("contracts"))
        if not ts or pts is None or not contracts:
            continue
        day = str(ts)[:10]  # YYYY-MM-DD; seanbot_signals.ts is UTC (+00:00)
        by_day[day] = by_day.get(day, 0.0) + pts * MNQ.multiplier * contracts
    return by_day


def _build_scoreboard(tf_by_day: dict[str, float], sb_by_day: dict[str, float]) -> Scoreboard:
    rows: list[ScoreRow] = []
    tf_cum = 0.0
    sb_cum = 0.0
    for day in sorted(set(tf_by_day) | set(sb_by_day)):  # oldest first for cumulatives
        tf_p = round(tf_by_day.get(day, 0.0), 2)
        sb_p = round(sb_by_day.get(day, 0.0), 2)
        tf_cum = round(tf_cum + tf_p, 2)
        sb_cum = round(sb_cum + sb_p, 2)
        delta = round(tf_p - sb_p, 2)
        rows.append(
            ScoreRow(
                day=day,
                tf_pnl=tf_p,
                sb_pnl=sb_p,
                delta=delta,
                winner=_winner(delta),
                tf_cum=tf_cum,
                sb_cum=sb_cum,
                delta_cum=round(tf_cum - sb_cum, 2),
            )
        )
    rows.reverse()  # newest day first for display
    delta_total = round(tf_cum - sb_cum, 2)
    leader = _winner(delta_total)
    return Scoreboard(
        rows=rows,
        tf_total=tf_cum,
        sb_total=sb_cum,
        delta_total=delta_total,
        leader=leader,
        headline=_headline(leader, delta_total),
    )


def _winner(delta: float) -> str:
    if delta > 0:
        return "TF"
    if delta < 0:
        return "SeanBot"
    return "tie"


def _headline(leader: str, delta_total: float) -> str:
    if leader == "tie":
        return "TF and SeanBot are even"
    return f"{leader} leads by ${abs(delta_total):,.2f}"


def _error_board(msg: str) -> Scoreboard:
    return Scoreboard(
        rows=[], tf_total=0.0, sb_total=0.0, delta_total=0.0, leader="tie", headline="—", error=msg
    )


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
