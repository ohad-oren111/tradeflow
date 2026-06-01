"""Kill switch — halt-on-loss/drawdown circuit breaker.

Wires the long-dormant ``risk_params`` thresholds (``max_consecutive_losses`` /
``max_daily_dd_pct`` / ``max_weekly_dd_pct``) into a real halt. When any trigger
fires it raises the existing global halt (blocks new entries), flattens any open
position via the existing safe cancel+market-exit path, and alerts. It stays
halted until a MANUAL operator reset (the existing Supabase ``halt_acks`` row /
``/tmp/halt_clear`` flag that the reconciler polls) — never auto-resumes.

Design invariants:
  * A kill switch can ONLY stop trading — there is no path here that opens, sizes
    up, or re-enables a position. It calls ``raise_halt`` + ``flatten`` only.
  * §0.5.98 — the loss count and realized P&L come from ``lifecycles`` (broker/DB
    truth), never an in-memory counter that drifts across the frequent restarts.
  * Fail-safe — if the evaluator itself errors, it errs toward NOT trading
    (raises the halt) and never raises into the poll/eval loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from config.risk_params import RISK, RiskParams

LOGGER = logging.getLogger(__name__)


@dataclass
class KillVerdict:
    tripped: bool
    reason: str | None  # consecutive_losses | daily_dd | weekly_dd | error | None
    detail: str


def evaluate_triggers(
    closed_pnls_newest_first: list[float],
    realized_today: float,
    realized_week: float,
    equity_base: float,
    *,
    max_consecutive_losses: int,
    daily_dd_pct: float,
    weekly_dd_pct: float,
) -> KillVerdict:
    """Pure trigger logic from broker/DB truth. ``closed_pnls_newest_first`` is the
    ``pnl_net`` of CLOSED lifecycles, newest first. Drawdown triggers are skipped
    when ``equity_base`` is unknown (<= 0) — the consecutive-loss trigger still
    applies. Threshold is inclusive (the Nth consecutive loss / DD AT the bound
    trips)."""
    n = max_consecutive_losses
    if (
        n > 0
        and len(closed_pnls_newest_first) >= n
        and all(p < 0 for p in closed_pnls_newest_first[:n])
    ):
        return KillVerdict(True, "consecutive_losses", f"last {n} closed trades all losing")
    if equity_base > 0:
        daily_limit = -(daily_dd_pct * equity_base)
        if realized_today <= daily_limit:
            return KillVerdict(
                True,
                "daily_dd",
                f"realized_today={realized_today:.2f} <= {daily_limit:.2f} "
                f"({daily_dd_pct:.0%} of {equity_base:.0f})",
            )
        weekly_limit = -(weekly_dd_pct * equity_base)
        if realized_week <= weekly_limit:
            return KillVerdict(
                True,
                "weekly_dd",
                f"realized_week={realized_week:.2f} <= {weekly_limit:.2f} "
                f"({weekly_dd_pct:.0%} of {equity_base:.0f})",
            )
    return KillVerdict(False, None, "ok")


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _sum_pnl_since(rows: list[dict], since: datetime) -> float:
    total = 0.0
    for r in rows:
        ts = _parse_ts(r.get("exit_filled_at"))
        if ts is None or ts < since:
            continue
        pnl = r.get("pnl_net")
        if pnl is not None:
            total += float(pnl)
    return total


def _utc_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _utc_week_start(now: datetime) -> datetime:
    midnight = _utc_day_start(now)
    return midnight - timedelta(days=now.weekday())  # Monday 00:00 UTC


class KillSwitch:
    """Polls the triggers and, on a trip, halts + flattens via injected callables.

    Callables are injected (not the orchestrator) so the breaker is unit-testable
    at the DB/broker boundary and has a deliberately tiny, stop-only surface:
      * ``is_halted()`` — skip if already halted (don't re-flatten / double-act);
      * ``raise_halt(reason)`` — the ONLY state change this class makes;
      * ``flatten()`` — the existing safe cancel+market-exit path;
      * ``equity_base()`` — broker net-liq (or configured base) for the DD %s.
    """

    def __init__(
        self,
        *,
        db: Any,
        is_halted: Callable[[], bool],
        raise_halt: Callable[[str], None],
        flatten: Callable[[], Awaitable[Any]],
        equity_base: Callable[[], Awaitable[float]],
        params: RiskParams = RISK,
    ) -> None:
        self._db = db
        self._is_halted = is_halted
        self._raise_halt = raise_halt
        self._flatten = flatten
        self._equity_base = equity_base
        self._p = params

    async def poll_once(self) -> KillVerdict:
        """Evaluate once; halt + flatten on a trip. Never raises."""
        if not self._p.kill_switch_enabled:
            return KillVerdict(False, "disabled", "kill switch disabled")
        if self._is_halted():
            # Already halted (by us earlier, the reconciler, or an operator) — do
            # not re-evaluate or re-flatten; wait for the manual reset.
            return KillVerdict(False, "already_halted", "halt already raised")
        try:
            verdict = await self._evaluate()
        except Exception as exc:  # noqa: BLE001 — fail-safe: cannot confirm safe → STOP
            LOGGER.error(
                "[KILL] evaluator_error — %s: %s (FAIL-SAFE: raising halt, blocking entries)",
                type(exc).__name__,
                exc,
            )
            self._raise_halt("kill_switch:evaluator_error")
            LOGGER.info("[ALERT] kill_switch_tripped: reason=evaluator_error")
            return KillVerdict(True, "error", str(exc))

        if not verdict.tripped:
            return verdict

        LOGGER.warning("[KILL] TRIPPED — reason=%s detail=%s", verdict.reason, verdict.detail)
        LOGGER.info(
            "[ALERT] kill_switch_tripped: reason=%s detail=%s", verdict.reason, verdict.detail
        )
        self._raise_halt(f"kill_switch:{verdict.reason}")
        try:
            await self._flatten()
        except Exception as exc:  # noqa: BLE001 — halt already raised; flatten is best-effort
            LOGGER.error(
                "[KILL] flatten_after_trip_failed — %s: %s (HALT stays raised; flatten manually)",
                type(exc).__name__,
                exc,
            )
        return verdict

    async def _evaluate(self) -> KillVerdict:
        rows = await self._db.select(
            "lifecycles",
            filters={"state": "eq.CLOSED", "order": "exit_filled_at.desc"},
            columns="pnl_net,exit_filled_at",
        )
        pnls = [float(r["pnl_net"]) for r in rows if r.get("pnl_net") is not None]
        now = datetime.now(UTC)
        realized_today = _sum_pnl_since(rows, _utc_day_start(now))
        realized_week = _sum_pnl_since(rows, _utc_week_start(now))
        equity_base = await self._safe_equity_base()
        return evaluate_triggers(
            pnls,
            realized_today,
            realized_week,
            equity_base,
            max_consecutive_losses=self._p.max_consecutive_losses,
            daily_dd_pct=self._p.max_daily_dd_pct,
            weekly_dd_pct=self._p.max_weekly_dd_pct,
        )

    async def _safe_equity_base(self) -> float:
        # A net-liq blip must not crash the evaluator (which would fail-safe halt);
        # treat an unavailable base as 0 → DD triggers skipped this poll, consec-loss
        # still active.
        try:
            return float(await self._equity_base())
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "[KILL] equity_base unavailable — %r (DD triggers skipped this poll)", exc
            )
            return 0.0

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        interval = max(1, int(self._p.kill_poll_interval_sec))
        LOGGER.info(
            "[KILL] poll loop started — interval=%ss enabled=%s consec=%s "
            "daily_dd=%.0f%% weekly_dd=%.0f%%",
            interval,
            self._p.kill_switch_enabled,
            self._p.max_consecutive_losses,
            self._p.max_daily_dd_pct * 100,
            self._p.max_weekly_dd_pct * 100,
        )
        while not stop_event.is_set():
            await self.poll_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
