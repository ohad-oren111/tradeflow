"""Kill switch — tiered halt-on-loss / drawdown circuit breaker (PR A).

Default = KEEP TRADING. A 6–9 consecutive-loss streak NOTIFIES once (Telegram)
without pausing; only a hard threshold PAUSES — ``KILL_SWITCH_HALT_CONSEC_LOSSES``
(default 10) consecutive losses, or a realized drawdown reaching
``KILL_SWITCH_MAX_DRAWDOWN_PCT`` (default 33) % of ``KILL_SWITCH_ALLOCATION_USD``
measured from ``KILL_SWITCH_PNL_EPOCH`` (default = deploy time, so pre-deploy
losses don't count). The drawdown brake is INERT while allocation is unset.

A PAUSE raises the existing global halt (blocks new entries) + flattens any open
position via the existing safe cancel+market-exit path. It stays halted until a
MANUAL operator reset (the Supabase ``halt_acks`` row / ``/tmp/halt_clear`` flag
the reconciler polls) — never auto-resumes. All thresholds are env-tunable
without a code change.

Design invariants:
  * A kill switch can ONLY stop trading — there is no path here that opens, sizes
    up, or re-enables a position. It calls ``raise_halt`` + ``flatten`` only.
  * §0.5.98 — the loss count and realized P&L come from ``lifecycles`` (broker/DB
    truth), never an in-memory counter that drifts across the frequent restarts.
  * Fail-safe — if the evaluator itself errors, it errs toward NOT trading
    (raises the halt) and never raises into the poll/eval loop.
  * Notifications are idempotent — fire once per crossing, reset on recovery — so
    the 30s poll never spams.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from config.risk_params import RISK, RiskParams

LOGGER = logging.getLogger(__name__)

# Transient/network faults that a brief retry-window can clear. The evaluator
# tolerates up to ``KILL_SWITCH_MAX_CONSEC_EVAL_ERRORS`` of these IN A ROW before
# fail-safe halting (a single Supabase ReadTimeout used to spuriously halt a
# healthy flat bot — handoff v17). Deliberately NOT a bare ``Exception``: a real
# logic bug (KeyError/TypeError/AttributeError/…) is excluded here and so halts
# on the FIRST occurrence — tolerance must never let a code bug ride for N polls.
#   * httpx.TransportError — the Supabase REST client (src/clients/supabase_client.py)
#     is httpx-based; covers ReadTimeout/ConnectTimeout/WriteTimeout/PoolTimeout
#     (TimeoutException) and ConnectError/ReadError/WriteError/NetworkError.
#     httpx.HTTPStatusError is intentionally EXCLUDED — a 4xx (bad query/auth) is a
#     bug, not a transient blip, and should halt immediately.
#   * TimeoutError — asyncio.TimeoutError is an alias of this in 3.11 (broker awaits).
#   * ConnectionError — socket-level reset/abort/refused (subclass of OSError).
_TRANSIENT_EVAL_ERRORS: tuple[type[Exception], ...] = (
    httpx.TransportError,
    TimeoutError,
    ConnectionError,
)


@dataclass
class KillVerdict:
    # action: "pause" (raise halt + flatten) | "notify" (alert only, keep trading)
    # | "ok". reason: consecutive_losses | drawdown | consecutive_losses_warning |
    # error | None.
    action: str
    reason: str | None
    detail: str

    @property
    def tripped(self) -> bool:
        """Back-compat alias — a 'pause' is the only action that halts trading."""
        return self.action == "pause"


def _consecutive_loss_streak(pnls_newest_first: list[float]) -> int:
    """Leading run of losses in a newest-first pnl list (a win/scratch breaks it)."""
    streak = 0
    for p in pnls_newest_first:
        if p < 0:
            streak += 1
        else:
            break
    return streak


def _collapse_loss_clusters(
    pnls_newest_first: list[float],
    entry_bars_newest_first: list[float | None],
    window_bars: int,
) -> list[float]:
    """PR #126 — collapse correlated stop-clusters into ONE loss event each.

    A run of consecutive LOSING closes whose ENTRIES fall within ``window_bars`` of
    each other (chained adjacent member to member) is replaced by a single
    synthetic loss equal to the run's summed pnl — so a concurrent book's N
    correlated stop-outs count as ONE loss toward the consecutive-loss streak, not
    N. Wins/scratches pass through unchanged and break a cluster. Pure; logs once
    per actual collapse.

    Misaligned inputs (length mismatch, or a ``None`` entry bar inside a run) are
    treated conservatively: the affected losses are NOT merged (they stay as
    separate loss events), so clustering can only ever RELAX the halt where the
    entry data is trustworthy — it never hides a loss it cannot prove is correlated.
    """
    if len(entry_bars_newest_first) != len(pnls_newest_first):
        return list(pnls_newest_first)
    collapsed: list[float] = []
    i = 0
    n = len(pnls_newest_first)
    while i < n:
        pnl = pnls_newest_first[i]
        if pnl >= 0:
            collapsed.append(pnl)
            i += 1
            continue
        cluster_sum = pnl
        members = 1
        prev_bar = entry_bars_newest_first[i]
        j = i + 1
        while (
            j < n
            and pnls_newest_first[j] < 0
            and prev_bar is not None
            and entry_bars_newest_first[j] is not None
            and abs(entry_bars_newest_first[j] - prev_bar) <= window_bars
        ):
            cluster_sum += pnls_newest_first[j]
            prev_bar = entry_bars_newest_first[j]
            members += 1
            j += 1
        if members > 1:
            LOGGER.info(
                "[KILL] cluster mode — collapsed %d correlated stops "
                "(entry window <= %d bars) into 1 loss event (sum=%.2f)",
                members,
                window_bars,
                cluster_sum,
            )
        collapsed.append(cluster_sum)
        i = j
    return collapsed


def evaluate_triggers(
    closed_pnls_newest_first: list[float],
    realized_since_epoch: float,
    allocation_usd: float | None,
    *,
    warn_consec_losses: int,
    halt_consec_losses: int,
    max_drawdown_pct: float,
    cluster_mode: bool = False,
    cluster_window_bars: int = 1,
    entry_bars_newest_first: list[float | None] | None = None,
) -> KillVerdict:
    """Pure tiered trigger logic from broker/DB truth (§0.5.98).

    ``closed_pnls_newest_first`` is the ``pnl_net`` of CLOSED lifecycles, newest
    first. ``realized_since_epoch`` is the cumulative realized pnl since the
    drawdown epoch (so pre-deploy losses don't count). Tiers:

      * PAUSE — ``streak >= halt_consec_losses`` (default 10), OR realized loss
        since the epoch reaches ``max_drawdown_pct`` % of ``allocation_usd``.
      * NOTIFY (no pause) — ``warn_consec_losses`` (6) ≤ streak < halt.
      * OK — otherwise.

    The drawdown brake is INERT when ``allocation_usd`` is None / ≤ 0 (the
    operator hasn't set allocation) — only the consecutive-loss tiers apply.
    Thresholds are inclusive (the Nth loss / the exact % trips).

    PR #126 — when ``cluster_mode`` is True AND ``entry_bars_newest_first`` (entry
    bar index / epoch-seconds, aligned 1:1 with the pnls) is supplied, correlated
    stop-clusters collapse into one loss event before the streak is computed (only
    the consecutive-loss streak is affected; the realized-drawdown brake still uses
    the true summed pnl). Default OFF ⇒ this whole branch is skipped and the streak
    is byte-identical to per-trade counting. ``cluster_mode`` True with no entry-bar
    info falls back to per-trade counting (a loud warning) — the live plumbing to
    supply entry bars is a separate change (PR Task E)."""
    pnls_for_streak = closed_pnls_newest_first
    if cluster_mode:
        if entry_bars_newest_first is None:
            LOGGER.warning(
                "[KILL] cluster mode set but no entry-bar info supplied — falling back "
                "to per-trade consecutive-loss counting (entry-bar plumbing not built)"
            )
        else:
            pnls_for_streak = _collapse_loss_clusters(
                closed_pnls_newest_first, entry_bars_newest_first, cluster_window_bars
            )
    streak = _consecutive_loss_streak(pnls_for_streak)
    # PAUSE tier 1 — consecutive-loss hard halt.
    if halt_consec_losses > 0 and streak >= halt_consec_losses:
        return KillVerdict(
            "pause",
            "consecutive_losses",
            f"{streak} consecutive losing trades (>= halt {halt_consec_losses})",
        )
    # PAUSE tier 2 — cumulative realized drawdown since the epoch (INERT if unset).
    if allocation_usd is not None and allocation_usd > 0:
        dd_limit = -(max_drawdown_pct / 100.0 * allocation_usd)
        if realized_since_epoch <= dd_limit:
            return KillVerdict(
                "pause",
                "drawdown",
                f"realized_since_epoch={realized_since_epoch:.2f} <= {dd_limit:.2f} "
                f"({max_drawdown_pct:.0f}% of {allocation_usd:.0f})",
            )
    # NOTIFY tier — a warn..halt-1 losing streak (alert once, keep trading).
    if warn_consec_losses > 0 and streak >= warn_consec_losses:
        return KillVerdict(
            "notify",
            "consecutive_losses_warning",
            f"{streak} consecutive losing trades "
            f"(warn {warn_consec_losses}, halt {halt_consec_losses})",
        )
    return KillVerdict("ok", None, "ok")


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _entry_bar_minutes(value: Any) -> float | None:
    """Entry timestamp → epoch MINUTES (float), the bar unit the cluster window uses.

    MNQ trades 1-min bars, so ``cluster_window_bars`` is in MINUTES and ``window=1``
    means two entries opened within one 1-min bar are "the same bar" for clustering.
    Returns None when the timestamp is missing/unparseable so the collapse logic
    (which only ever uses entry-bar DIFFERENCES) treats that loss conservatively —
    a None bar is never merged into a cluster (§ #129 contract)."""
    ts = _parse_ts(value)
    if ts is None:
        return None
    return ts.timestamp() / 60.0


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


def _resolve_pnl_epoch(raw: str, *, fallback: datetime) -> datetime:
    """Drawdown epoch from ``KILL_SWITCH_PNL_EPOCH`` (ISO), else ``fallback``.

    Default fallback is the kill switch's construction time (≈ deploy time), so a
    fresh deploy's drawdown counter starts at 0 and pre-deploy / bug-contaminated
    losses never count toward the % drawdown brake."""
    parsed = _parse_ts(raw)
    if parsed is None:
        return fallback
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class KillSwitch:
    """Polls the triggers and, on a trip, halts + flattens via injected callables.

    Callables are injected (not the orchestrator) so the breaker is unit-testable
    at the DB/broker boundary and has a deliberately tiny, stop-only surface:
      * ``is_halted()`` — skip if already halted (don't re-flatten / double-act);
      * ``raise_halt(reason)`` — the ONLY state change this class makes;
      * ``flatten()`` — the existing safe cancel+market-exit path;
      * ``equity_base()`` — broker net-liq (retained for back-compat; the tiered
        drawdown brake now measures against the configured ALLOCATION, not net-liq).

    PR A — tiered: a 6–9 losing streak NOTIFIES once (Telegram) without pausing;
    only ≥10 consecutive losses or ≥33% realized drawdown of the configured
    allocation (since the epoch) PAUSES. Notifications are idempotent — fire once
    per crossing, reset on recovery (a win) — so there's no 30s poll spam.
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
        self._equity_base = equity_base  # retained for back-compat (unused by tiers)
        self._p = params
        # Drawdown epoch: env override, else construction time (≈ deploy time).
        self._pnl_epoch = _resolve_pnl_epoch(
            getattr(params, "kill_switch_pnl_epoch", "") or "",
            fallback=datetime.now(UTC),
        )
        # Idempotency: the warn-tier alert fires once per losing streak crossing.
        self._warn_notified = False
        # Consecutive transient (network) evaluator faults — reset on any clean
        # poll; a fail-safe halt fires only when it reaches the configured max.
        self._consec_eval_errors = 0

    async def poll_once(self) -> KillVerdict:
        """Evaluate once; pause+flatten on a PAUSE, alert-once on a NOTIFY. Never raises."""
        if not self._p.kill_switch_enabled:
            return KillVerdict("ok", "disabled", "kill switch disabled")
        if self._is_halted():
            # Already halted (by us earlier, the reconciler, or an operator) — do
            # not re-evaluate or re-flatten; wait for the manual reset.
            return KillVerdict("ok", "already_halted", "halt already raised")
        try:
            verdict = await self._evaluate()
        except _TRANSIENT_EVAL_ERRORS as exc:
            # TRANSIENT/network fault — tolerate a few in a row (a single Supabase
            # blip should not halt a healthy flat bot). Only halt at the ceiling.
            self._consec_eval_errors += 1
            limit = max(1, int(self._p.kill_switch_max_consec_eval_errors))
            if self._consec_eval_errors < limit:
                LOGGER.warning(
                    "[KILL] eval: transient poll error %d/%d — %s: %s (tolerating, NOT halting)",
                    self._consec_eval_errors,
                    limit,
                    type(exc).__name__,
                    exc,
                )
                return KillVerdict("ok", "transient_error", f"{type(exc).__name__}: {exc}")
            LOGGER.error(
                "[KILL] eval: halting after %d consecutive transient errors — %s: %s "
                "(FAIL-SAFE: raising halt, blocking entries)",
                self._consec_eval_errors,
                type(exc).__name__,
                exc,
            )
            return self._raise_eval_halt(exc)
        except Exception as exc:  # noqa: BLE001 — NON-transient (logic) error: halt FIRST hit
            LOGGER.error(
                "[KILL] evaluator_error — %s: %s (FAIL-SAFE: raising halt, blocking entries)",
                type(exc).__name__,
                exc,
            )
            return self._raise_eval_halt(exc)

        # Clean poll — clear any transient-error streak (log once when it had accrued).
        if self._consec_eval_errors:
            LOGGER.info(
                "[KILL] eval: transient error counter reset — clean poll (was %d)",
                self._consec_eval_errors,
            )
            self._consec_eval_errors = 0

        if verdict.action == "notify":
            # NOTIFY tier — alert once per crossing, keep trading (no halt/flatten).
            if not self._warn_notified:
                self._warn_notified = True
                LOGGER.warning(
                    "[KILL] WARNING — reason=%s detail=%s", verdict.reason, verdict.detail
                )
                LOGGER.info(
                    "[ALERT] kill_switch_warning: reason=%s detail=%s",
                    verdict.reason,
                    verdict.detail,
                )
            return verdict

        if verdict.action == "ok":
            # Streak broke / recovered → re-arm the one-shot warn notification.
            self._warn_notified = False
            return verdict

        # action == "pause" — hard halt + flatten.
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

    def _raise_eval_halt(self, exc: Exception) -> KillVerdict:
        """Raise the global halt for an evaluator failure + emit the trip alert.

        Shared by both halt paths (the transient-error ceiling and a first-hit
        non-transient logic error); the distinguishing log line is emitted by the
        caller before this runs. Returns the PAUSE verdict (never raises)."""
        self._raise_halt("kill_switch:evaluator_error")
        LOGGER.info("[ALERT] kill_switch_tripped: reason=evaluator_error")
        return KillVerdict("pause", "error", str(exc))

    async def _evaluate(self) -> KillVerdict:
        rows = await self._db.select(
            "lifecycles",
            filters={"state": "eq.CLOSED", "order": "exit_filled_at.desc"},
            columns="pnl_net,exit_filled_at,entry_filled_at",
        )
        # Build the pnl + entry-bar lists from the SAME filtered rows in one pass so
        # entry_bars stays index-aligned 1:1 with pnls (the cluster collapse relies
        # on that alignment — a drifted index would mis-group losses).
        closed = [r for r in rows if r.get("pnl_net") is not None]
        pnls = [float(r["pnl_net"]) for r in closed]
        entry_bars = [_entry_bar_minutes(r.get("entry_filled_at")) for r in closed]
        realized_since_epoch = _sum_pnl_since(rows, self._pnl_epoch)
        cluster_mode = getattr(self._p, "kill_switch_cluster_mode", False)
        if cluster_mode:
            # ON path is deploy-gated (operator decision); log the plumbing so the
            # cluster accounting is visible. OFF (today) stays silent — no 30s spam.
            LOGGER.info(
                "[KILL] : entry-bar plumbing — supplied %d entry bars (cluster_mode=%s)",
                len(entry_bars),
                cluster_mode,
            )
        # PR #126 + Task E — feed real entry bars (epoch-minutes, aligned to pnls) so
        # cluster collapse operates on live data WHEN later enabled. With the flag OFF
        # (today's default) evaluate_triggers ignores entry_bars entirely ⇒ supplying
        # them vs None is byte-identical; this changes NO live behavior.
        return evaluate_triggers(
            pnls,
            realized_since_epoch,
            self._p.kill_switch_allocation_usd,
            warn_consec_losses=self._p.kill_switch_warn_consec_losses,
            halt_consec_losses=self._p.kill_switch_halt_consec_losses,
            max_drawdown_pct=self._p.kill_switch_max_drawdown_pct,
            cluster_mode=cluster_mode,
            cluster_window_bars=getattr(self._p, "cluster_window_bars", 1),
            entry_bars_newest_first=entry_bars,
        )

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        interval = max(1, int(self._p.kill_poll_interval_sec))
        LOGGER.info(
            "[KILL] poll loop started — interval=%ss enabled=%s warn_consec=%s halt_consec=%s "
            "max_drawdown=%.0f%% allocation=%s pnl_epoch=%s",
            interval,
            self._p.kill_switch_enabled,
            self._p.kill_switch_warn_consec_losses,
            self._p.kill_switch_halt_consec_losses,
            self._p.kill_switch_max_drawdown_pct,
            (
                f"${self._p.kill_switch_allocation_usd:.0f}"
                if self._p.kill_switch_allocation_usd is not None
                else "UNSET"
            ),
            self._pnl_epoch.isoformat(),
        )
        if self._p.kill_switch_allocation_usd is None:
            # Loud startup warning — the % drawdown brake can't compute without an
            # allocation, so it is INERT until the operator sets KILL_SWITCH_ALLOCATION_USD.
            LOGGER.warning(
                "[KILL] ALLOCATION_USD UNSET — the %.0f%% drawdown brake is INERT; "
                "the >=%d consecutive-loss halt is the active hard brake",
                self._p.kill_switch_max_drawdown_pct,
                self._p.kill_switch_halt_consec_losses,
            )
        while not stop_event.is_set():
            await self.poll_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
