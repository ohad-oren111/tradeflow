"""Dashboard trade-log + daily realized-P&L data access (PR #70, Phase 1).

Read-only views over the Supabase ``lifecycles`` table via the orchestrator's
existing service-role ``SupabaseClient`` (the same client ``main.py`` builds with
``SUPABASE_SERVICE_ROLE_KEY``). SELECT only — no writes, no broker calls, no
order/strategy imports.

P&L here is the DB view: forward closes (post-PR #65 fill-price fix and post-PR
#69 entry-price fix) are accurate; some historical rows are NOT restated (e.g.
the ``b39a4def`` notional entry price, an un-restated slipped stop, a pre-#57
commission). The templates label every view ``DB_VIEW_CAVEAT`` — never present
these numbers as broker-true ground truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.orchestrator import Orchestrator

LOGGER = logging.getLogger(__name__)

DB_VIEW_CAVEAT = "DB view — broker-true may differ for historical rows"
_TRADE_LOG_LIMIT = 100


@dataclass
class TradeRow:
    lifecycle_id: str
    entry_time: str | None
    exit_time: str | None
    direction: str
    symbol: str
    entry_price: float | None
    exit_price: float | None
    qty: int | None
    pnl_net: float | None
    exit_reason: str | None
    state: str
    is_open: bool


@dataclass
class TradeLog:
    rows: list[TradeRow]
    caveat: str = DB_VIEW_CAVEAT
    error: str | None = None


@dataclass
class DayPnl:
    day: str
    closed_count: int
    pnl_net: float
    wins: int
    losses: int
    running_total: float


@dataclass
class DailyPnl:
    days: list[DayPnl]
    caveat: str = DB_VIEW_CAVEAT
    error: str | None = None


class TradesAggregator:
    """Read-only ``lifecycles`` views via the orchestrator's service-role client.

    Mirrors the watchdog's query shape (``select`` / ``state=eq.CLOSED`` /
    ``order``); reuses ``orchestrator._db`` so no Supabase key is read or rendered
    here. Each method is independently try/except'd — a read failure surfaces as
    an ``error`` string on the view, not a 500.
    """

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orch = orchestrator

    async def trade_log(self, limit: int = _TRADE_LOG_LIMIT) -> TradeLog:
        try:
            raw = await self._orch._db.select(
                "lifecycles",
                filters={"order": "created_at.desc", "limit": str(limit)},
                columns=(
                    "lifecycle_id,direction,symbol,entry_price,exit_price,entry_qty,"
                    "pnl_net,exit_reason,state,entry_filled_at,exit_filled_at"
                ),
            )
        except Exception as exc:  # noqa: BLE001 — surface as a view error, never 500
            LOGGER.warning("[DASH] trade_log: read_failed — %r", exc)
            return TradeLog(rows=[], error=f"{type(exc).__name__}: {exc}")
        return TradeLog(rows=[_to_trade_row(r) for r in raw])

    async def daily_pnl(self) -> DailyPnl:
        try:
            raw = await self._orch._db.select(
                "lifecycles",
                filters={"state": "eq.CLOSED", "order": "exit_filled_at.desc"},
                columns="pnl_net,exit_filled_at,state",
            )
        except Exception as exc:  # noqa: BLE001 — surface as a view error, never 500
            LOGGER.warning("[DASH] daily_pnl: read_failed — %r", exc)
            return DailyPnl(days=[], error=f"{type(exc).__name__}: {exc}")
        return DailyPnl(days=_aggregate_daily(raw))


def _to_trade_row(row: dict[str, Any]) -> TradeRow:
    state = str(row.get("state") or "?")
    is_open = state != "CLOSED"
    return TradeRow(
        lifecycle_id=str(row.get("lifecycle_id") or ""),
        entry_time=_fmt_ts(row.get("entry_filled_at")),
        exit_time=None if is_open else _fmt_ts(row.get("exit_filled_at")),
        direction=str(row.get("direction") or "?"),
        symbol=str(row.get("symbol") or "?"),
        entry_price=_f(row.get("entry_price")),
        exit_price=None if is_open else _f(row.get("exit_price")),
        qty=_i(row.get("entry_qty")),
        pnl_net=None if is_open else _f(row.get("pnl_net")),
        exit_reason=None if is_open else (row.get("exit_reason") or None),
        state=state,
        is_open=is_open,
    )


def _aggregate_daily(rows: list[dict[str, Any]]) -> list[DayPnl]:
    """Group realized pnl_net by UTC close-day. Only CLOSED rows with an exit
    timestamp contribute; an open lifecycle (no ``exit_filled_at``) adds 0."""
    by_day: dict[str, list[float]] = {}
    for row in rows:
        if str(row.get("state") or "") != "CLOSED":
            continue
        xfa = row.get("exit_filled_at")
        if not xfa:
            continue
        day = str(xfa)[:10]  # YYYY-MM-DD; lifecycles timestamps are UTC (+00:00)
        by_day.setdefault(day, []).append(_f(row.get("pnl_net")) or 0.0)

    out: list[DayPnl] = []
    running = 0.0
    for day in sorted(by_day):  # oldest→newest to accumulate the running total
        pnls = by_day[day]
        running += sum(pnls)
        out.append(
            DayPnl(
                day=day,
                closed_count=len(pnls),
                pnl_net=sum(pnls),
                wins=sum(1 for p in pnls if p > 0),
                losses=sum(1 for p in pnls if p < 0),
                running_total=running,
            )
        )
    out.reverse()  # newest day first for display
    return out


def _fmt_ts(value: Any) -> str | None:
    if not value:
        return None
    return str(value)[:19].replace("T", " ")


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
