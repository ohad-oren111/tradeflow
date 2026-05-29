"""Long-running orchestrator for TradeFlow.

Owns the lifetime of an ``IBClient`` and a ``SupabaseClient``, runs a periodic
IBKR healthcheck, and exits cleanly on SIGTERM. PR #10 adds the first end-to-end
trading path: bar subscription → strategy → bracket placement → EOD force-close.

§0.5.T1 — single IBKR client id (orchestrator uses ``IBKR_CLIENT_ID``).
§0.5.T4 — kill switch is NOT in this PR; clean exit code is 0.
§0.5.T5 — bracket pair is parent+TP; protective STP is placed by the router
inside the parent fillEvent handler before the lifecycle reaches ACTIVE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import signal
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Any

from ib_async import Contract, Future

from comms.telegram import TelegramAlerter
from config.instruments import MNQ
from src.clients.ib_client import BrokerExtendedOutageError, IBClient
from src.clients.supabase_client import SupabaseClient
from src.comparison.seanbot_reconciler import _RECON_JSONL_PATH, SeanbotReconciler
from src.execution.dirty_set import DirtySet
from src.execution.force_close import EodForceClose
from src.execution.reconciler import Reconciler
from src.execution.router import CloseResult, OrderRouter
from src.journal_rotation import rotate_jsonl_if_large
from src.state_machine import InvariantViolationError, Lifecycle, State, StateMachine

# _in_session_edge_window is reused by the bar-liveness watchdog so that the
# "expected bar window" definition has a single source of truth with the
# strategy. This is the only consumer outside src.strategy itself.
from src.strategy import (
    STRATEGY_NAME,
    Signal,
    Sma100BounceStrategy,
    _in_session_edge_window,
)

# Bar-liveness watchdog tunables.
_WATCHDOG_STALE_THRESHOLD_SEC = 5 * 60
_WATCHDOG_ALERT_COOLDOWN_SEC = 15 * 60
_BAR_TIMESTAMP_RING_MAXLEN = 4096

# Track 3 — decision journal. Bounded in-memory ring of the most recent
# per-bar strategy decisions (mirrors the live_bars_60m ring), plus a local
# JSONL append for the inspect_decisions replay tool. The JSONL dir is created
# on demand; if no logs volume is mounted it is ephemeral (queued follow-up).
_DECISION_JOURNAL_MAXLEN = 240
_DECISION_JSONL_PATH = "/app/logs/decisions.jsonl"

# Track 5a — hourly session-status digest. One [ALERT] line per interval during
# CME session hours, sourced from the decision journal + the SeanBot
# reconciliation journal (both already in-process). Suppressed during edge
# windows so maintenance silence never pages the operator. Interval is tunable.
_HOURLY_DIGEST_INTERVAL_SEC = 60 * 60
# "Feed OK" if a live bar arrived within this window at digest time.
_DIGEST_FEED_OK_SEC = 5 * 60
# W-S15.3 Track E — bars needed before the SMA is warm. ma_slow is SMA100
# (src/indicators.py: sma_100), so 100 1-min bars (~99 min) seeds the slow MA;
# the strategy's own indicators-ready gate is authoritative, this is the ETA.
_WARMUP_BARS_NEEDED = 100

# Transient broker disconnects worth catching mid-loop and triggering a
# resilient reconnect rather than orchestrator exit. Mirrors the tuple in
# ``src.clients.ib_client._TRANSIENT_CONNECT_EXC`` — kept local to avoid a
# back-reference import. ConnectionError covers Refused / Reset / Aborted.
_TRANSIENT_BROKER_EXC: tuple[type[BaseException], ...] = (
    TimeoutError,
    socket.gaierror,
    ConnectionError,
)


@dataclass
class FlattenResult:
    """Aggregate outcome of :meth:`Orchestrator.flatten_all`."""

    requested_symbols: list[str] = field(default_factory=list)
    closed: list[CloseResult] = field(default_factory=list)


@dataclass
class ExitResult:
    """Outcome of :meth:`Orchestrator.exit_symbol`."""

    symbol: str
    result: CloseResult


LOGGER = logging.getLogger(__name__)


class Orchestrator:
    """Owns IB + DB lifetimes, runs a periodic healthcheck, exits on SIGTERM."""

    def __init__(
        self,
        ib: IBClient,
        db: SupabaseClient,
        *,
        paper_account: str,
        healthcheck_interval: float = 60.0,
        state_machine: StateMachine | None = None,
        instrument: str = "MNQM6",
        bar_size: str = "1 min",
        enable_strategy: bool = True,
        reconnect_max_attempts: int = 30,
        reconnect_backoff_initial_sec: float = 2.0,
        reconnect_backoff_max_sec: float = 30.0,
        reconnect_connect_timeout_sec: float = 20.0,
    ) -> None:
        self._ib = ib
        self._db = db
        self._paper_account = paper_account
        self._healthcheck_interval = healthcheck_interval
        self._sm: StateMachine = state_machine or StateMachine(db=db, ib=ib)
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started_at: float | None = None
        self._instrument = instrument
        self._bar_size = bar_size
        self._enable_strategy = enable_strategy
        self._reconnect_max_attempts = reconnect_max_attempts
        self._reconnect_backoff_initial_sec = reconnect_backoff_initial_sec
        self._reconnect_backoff_max_sec = reconnect_backoff_max_sec
        self._reconnect_connect_timeout_sec = reconnect_connect_timeout_sec
        self._contract: Contract = _build_contract(instrument)
        self._strategy = Sma100BounceStrategy(instrument)
        # PR #11 — dirty set + halt flag + reconciler wired before any orders fly.
        # PR #12 — halt flag is now ack-able via raise_halt/clear_halt; the
        # reconciler holds an orchestrator handle so it can poll halt_acks.
        self._dirty_set = DirtySet()
        self._halt_new_entries = False
        self._halt_raised_at: datetime | None = None
        self._router = OrderRouter(
            ib=ib,
            sm=self._sm,
            strategy_name=STRATEGY_NAME,
            dirty_set=self._dirty_set,
        )
        self._eod = EodForceClose(self._router, self._sm, contract=self._contract)
        self._reconciler = Reconciler(
            ib=ib,
            sm=self._sm,
            dirty_set=self._dirty_set,
            db=db,
            orchestrator=self,
        )
        # PR #14 — optional Telegram alerter + command bot. Disabled when env
        # vars are absent so unit tests and pre-activation runs don't crash.
        self._telegram: TelegramAlerter | None = _build_telegram_if_configured(self, db=db)
        self._background_tasks: list[asyncio.Task[Any]] = []
        self._bars: Any = None
        # Bar-liveness watchdog state. Armed at the end of
        # _start_bar_subscription (regardless of subscribe success) so the
        # 5-min staleness clock starts there. None means "not yet armed".
        self._last_bar_at: datetime | None = None
        self._last_bar_alert_at: datetime | None = None
        self._bar_timestamps: deque[datetime] = deque(maxlen=_BAR_TIMESTAMP_RING_MAXLEN)
        self._decision_journal: deque[dict] = deque(maxlen=_DECISION_JOURNAL_MAXLEN)
        # Track 5b — timestamps of long_signals suppressed because a position was
        # already open. Aggregated into the hourly digest (NOT alerted per-bar,
        # which would be ~884/day of noise). Bounded like the decision journal.
        self._suppressed_entry_ts: deque[datetime] = deque(maxlen=_DECISION_JOURNAL_MAXLEN)
        # Track 4 — SeanBot-vs-TradeFlow reconciler. Reads its decision data
        # from this orchestrator's journal (get_recent_decisions); polls the
        # shared Supabase seanbot_signals table for new entries in its task.
        self._seanbot_reconciler = SeanbotReconciler(
            decisions_getter=lambda: self.get_recent_decisions(_DECISION_JOURNAL_MAXLEN)
        )

    async def run(self) -> int:
        """Start orchestrator, loop on healthcheck, return process exit code."""
        self._stop_event = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._started_at = time.monotonic()
        self._install_signal_handlers()

        exit_code = 0
        try:
            await self._startup()
            while not self._stop_event.is_set():
                try:
                    await self._healthcheck_once()
                except _TRANSIENT_BROKER_EXC as exc:
                    LOGGER.warning(
                        "[ORCH] healthcheck: transient_disconnect — type=%s msg=%s",
                        type(exc).__name__,
                        exc,
                    )
                    await self._resilient_reconnect()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._healthcheck_interval,
                    )
                except TimeoutError:
                    continue
        except BrokerExtendedOutageError as exc:
            # Clean exit (code 0) so docker recreates the container as a last
            # resort. Distinct from the catch-all below: this is an EXPECTED
            # escalation path, not a programming error.
            LOGGER.error(
                "[ORCH] shutdown: extended_outage — attempts=%d elapsed=%.1fs last=%s",
                exc.attempts,
                exc.elapsed_sec,
                type(exc.last_exc).__name__,
            )
            LOGGER.info(
                "[ALERT] extended_outage: attempts=%d elapsed_sec=%.1f",
                exc.attempts,
                exc.elapsed_sec,
            )
        except Exception as exc:
            LOGGER.error(
                "[ORCH] shutdown: exception — type=%s msg=%s",
                type(exc).__name__,
                exc,
            )
            exit_code = 1
        finally:
            await self._shutdown(exit_code)

        return exit_code

    async def _resilient_reconnect(self) -> None:
        """Reconnect the broker socket with exponential backoff.

        Emits an ``[ALERT]`` line on success so Telegram surfaces the recovery
        to the operator. Raises ``BrokerExtendedOutageError`` if max_attempts
        is exhausted — caller in ``run()`` clean-exits on that.
        """
        started = time.monotonic()
        await self._ib.connect_with_resilience(
            max_attempts=self._reconnect_max_attempts,
            backoff_initial_sec=self._reconnect_backoff_initial_sec,
            backoff_max_sec=self._reconnect_backoff_max_sec,
            connect_timeout_sec=self._reconnect_connect_timeout_sec,
        )
        elapsed = time.monotonic() - started
        LOGGER.info(
            "[ALERT] reconnect_recovered: elapsed_sec=%.1f",
            elapsed,
        )
        # The keepUpToDate bar subscription is bound to the socket that just
        # dropped; ib_async does not carry it across a reconnect. Re-arm it on
        # the fresh connection so [BAR] resumes — otherwise the feed stays
        # silent until a manual restart even though the socket is healthy (the
        # "Peer closed connection" blind-feed mode, distinct from the §0.5.181
        # farm flap that PR-R1 handles via errorEvent). The errorEvent /
        # execDetails handlers live on the persistent IB object and survive the
        # reconnect, so only the bar subscription needs re-arming.
        if self._enable_strategy:
            await self._start_bar_subscription()
            LOGGER.info("[ORCH] bar_subscription re-armed after socket reconnect")

    async def _startup(self) -> None:
        import os

        pid = os.getpid()
        LOGGER.info(
            "[ORCH] startup: begin — pid=%s healthcheck_interval=%s",
            pid,
            self._healthcheck_interval,
        )
        LOGGER.info(
            "[ORCH] startup: ib_connecting — host=%s port=%s client_id=%s",
            getattr(self._ib, "_host", "?"),
            getattr(self._ib, "_port", "?"),
            getattr(self._ib, "_client_id", "?"),
        )
        await self._ib.connect_with_resilience(
            max_attempts=self._reconnect_max_attempts,
            backoff_initial_sec=self._reconnect_backoff_initial_sec,
            backoff_max_sec=self._reconnect_backoff_max_sec,
            connect_timeout_sec=self._reconnect_connect_timeout_sec,
        )
        server_version = self._safe_server_version()
        LOGGER.info("[ORCH] startup: ib_connected — server_version=%s", server_version)

        summary = await self._ib._ib.accountSummaryAsync(self._paper_account)
        net_liq = self._extract_net_liquidation(summary)
        if net_liq is None:
            raise RuntimeError(
                f"NetLiquidation row missing from accountSummaryAsync for account "
                f"{self._account_prefix(self._paper_account)} — refusing to start"
            )
        LOGGER.info(
            "[ORCH] startup: account_bound — prefix=%s net_liq=%s",
            self._account_prefix(self._paper_account),
            net_liq,
        )
        await self._recover_state()
        if self._enable_strategy:
            self._wire_fill_event()
            await self._start_bar_subscription()
            self._arm_farm_flap_resubscribe()
            self._launch_background_tasks()

    async def _recover_state(self) -> None:
        """Load non-CLOSED lifecycles and reconcile with broker reality.

        One-shot at startup. Uses read-only IB API calls (positions, openTrades)
        — safe under IBC Read-Only API mode (§0.5.123). Applies any transitions
        that the state machine deems necessary based on broker drift, then
        re-registers each lifecycle with the router so subsequent fillEvents
        route correctly.
        """
        lifecycles = await self._sm.load_non_closed()
        LOGGER.info("[ORCH] state: recovery_loaded — count=%s", len(lifecycles))
        transitions_applied = 0
        for lc in lifecycles:
            target = await self._sm.boot_time_broker_check(lc)
            if target is not None:
                field_updates = await self._broker_field_updates_for(lc, target)
                lc = await self._sm.transition(
                    lc,
                    target,
                    reason="boot_time_broker_check",
                    payload={"prior_state": lc.state},
                    **field_updates,
                )
                LOGGER.info(
                    "[ORCH] state: recovery_transition — id=%s to=%s",
                    lc.lifecycle_id,
                    target.value,
                )
                transitions_applied += 1
            if lc.state != State.CLOSED.value:
                self._router.register_recovered(lc, self._contract)
        LOGGER.info(
            "[ORCH] state: recovery_complete — transitions_applied=%s",
            transitions_applied,
        )

    def _wire_fill_event(self) -> None:
        """Register :meth:`OrderRouter.on_fill` as the global execDetails handler.

        Uses the synchronous ``call_soon_threadsafe`` bridge so the async router
        method is scheduled on the orchestrator's loop, not the IB thread.
        """
        raw_ib = getattr(self._ib, "_ib", None)
        if raw_ib is None:
            return
        event = getattr(raw_ib, "execDetailsEvent", None)
        if event is None:
            return
        try:
            event += self._on_exec_details
            LOGGER.info("[ORCH] startup: fill_event_wired")
        except Exception as exc:
            LOGGER.warning(
                "[ORCH] startup: fill_event_wire_failed — type=%s msg=%s",
                type(exc).__name__,
                exc,
            )

    def _on_exec_details(self, trade: Any, fill: Any) -> None:
        if self._loop is None:
            return
        coro = self._router.on_fill(trade, fill)
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _start_bar_subscription(self) -> None:
        # CME futures trade 24/5; the strategy session boundaries (PR #32) span
        # Sun 18:00 ET → Fri 16:25 ET. useRTH=True would silently filter bars
        # to the 09:30–16:00 ET liquid window and starve the scanner overnight.
        use_rth = False
        try:
            self._bars = await self._ib.subscribe_bars(
                self._contract,
                bar_size=self._bar_size,
                use_rth=use_rth,
                on_new_bar=self._on_new_bar,
            )
            LOGGER.info(
                "[STRAT] %s: bar_subscription started — bar_size=%s use_rth=%s",
                self._instrument,
                self._bar_size,
                use_rth,
            )
        except Exception as exc:
            LOGGER.warning(
                "[ORCH] startup: bar_subscription_failed — type=%s msg=%s",
                type(exc).__name__,
                exc,
            )
        # Arm the bar-liveness watchdog clock either way — if the subscribe
        # failed we still want a [WATCHDOG] alert 5 min later instead of
        # silent darkness.
        self._last_bar_at = datetime.now(UTC)

    def _arm_farm_flap_resubscribe(self) -> None:
        """Wire the IB client's farm-flap watcher to re-arm the bar sub.

        §0.5.181 — the keepUpToDate sub dies on the 2103/2105/10182 trio and is
        never auto-rearmed (the socket stays up, so the PR-A socket reconnect
        does not fire). The client watches ``ib.errorEvent`` and calls back into
        ``_start_bar_subscription``, which reuses the retained contract + args.
        Composes with the PR #47 watchdog (still the safety net) — does not
        replace it.
        """
        if self._loop is None:
            return
        try:
            self._ib.arm_farm_flap_watch(self._loop, self._start_bar_subscription)
        except Exception as exc:
            LOGGER.warning(
                "[ORCH] farm_flap_watch_arm_failed — type=%s msg=%s",
                type(exc).__name__,
                exc,
            )

    def _on_new_bar(self, bar: dict) -> None:
        now = datetime.now(UTC)
        self._last_bar_at = now
        self._bar_timestamps.append(now)
        if self._last_bar_alert_at is not None:
            LOGGER.warning("[WATCHDOG] bar feed recovered — first bar after stale window")
            LOGGER.info("[ALERT] watchdog_bar_recovered")
            self._last_bar_alert_at = None
        try:
            signal_or_none = self._strategy.on_new_bar(bar)
        except Exception as exc:
            LOGGER.error(
                "[STRAT] %s: on_new_bar_error — type=%s msg=%s",
                self._instrument,
                type(exc).__name__,
                exc,
            )
            return
        # Track 3 — record every bar's decision (entry or noop) to the journal.
        self._record_decision(self._strategy.last_decision)
        if signal_or_none is None:
            return
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._handle_trade_signal(signal_or_none), self._loop)

    def _watchdog_check_bar_liveness(self) -> None:
        """Alert if no bar arrived in >5 min during an expected CME session.

        Suppressed during edge windows (Saturday, Sunday pre-open, daily CME
        break, Friday cutoff, IB Gateway restart band) so maintenance silence
        never wakes the operator. ALERT-only; does NOT auto-halt. Re-fires
        every ``_WATCHDOG_ALERT_COOLDOWN_SEC`` while stale to avoid log spam.
        """
        if self._last_bar_at is None:
            return
        now = datetime.now(UTC)
        if _in_session_edge_window(now, edge_minutes=0):
            return
        stale_sec = (now - self._last_bar_at).total_seconds()
        if stale_sec <= _WATCHDOG_STALE_THRESHOLD_SEC:
            return
        if (
            self._last_bar_alert_at is not None
            and (now - self._last_bar_alert_at).total_seconds() < _WATCHDOG_ALERT_COOLDOWN_SEC
        ):
            return
        stale_min = int(stale_sec // 60)
        LOGGER.warning(
            "[WATCHDOG] no live bar in %dm during session — feed delayed/dead",
            stale_min,
        )
        LOGGER.info("[ALERT] watchdog_stale_bars: stale_min=%d", stale_min)
        self._last_bar_alert_at = now

    def _count_live_bars_last_60m(self) -> int:
        """Return number of bars whose arrival ts is within the last 60 minutes.

        Prunes the deque in-place to bound memory across long uptimes; the
        deque maxlen also caps worst-case growth.
        """
        if not self._bar_timestamps:
            return 0
        now = datetime.now(UTC)
        window_start_sec = 60 * 60
        while (
            self._bar_timestamps
            and (now - self._bar_timestamps[0]).total_seconds() > window_start_sec
        ):
            self._bar_timestamps.popleft()
        return len(self._bar_timestamps)

    def _record_decision(self, record: dict | None) -> None:
        """Append the strategy's latest decision to the ring buffer + JSONL.

        Best-effort on the file write — a journal IO error (e.g. read-only fs)
        must never break the bar path, so it degrades to a debug log.
        """
        if record is None:
            return
        self._decision_journal.append(record)
        try:
            path = pathlib.Path(_DECISION_JSONL_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Track 6b — bound the unbounded JSONL on long-lived containers.
            rotate_jsonl_if_large(path)
            with path.open("a") as fh:
                fh.write(json.dumps(record) + "\n")
        except Exception as exc:
            LOGGER.debug("[ORCH] decision_journal: write skipped — %s", exc)

    def get_recent_decisions(self, n: int = 50) -> list[dict]:
        """Return up to the last ``n`` decisions, newest first.

        Consumed by the inspect_decisions replay tool, /status, and (Track 4)
        the SeanBot reconciler's nearest-decision lookup.
        """
        items = list(self._decision_journal)
        if n > 0:
            items = items[-n:]
        return list(reversed(items))

    # ----------------------------------------------------- Track 5a hourly digest

    async def _hourly_digest_loop(self) -> None:
        """Emit one session-status digest per ``_HOURLY_DIGEST_INTERVAL_SEC``.

        Waits the interval first (so the first digest covers a full window of
        data rather than firing empty at startup), then emits. Each pass is
        wrapped so a transient error never kills the task.
        """
        LOGGER.info("[ORCH] hourly_digest: started — interval=%ds", _HOURLY_DIGEST_INTERVAL_SEC)
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=_HOURLY_DIGEST_INTERVAL_SEC)
                break  # stop requested
            except TimeoutError:
                pass
            try:
                await self._emit_hourly_digest()
            except Exception as exc:
                LOGGER.warning(
                    "[ORCH] hourly_digest: emit error — type=%s msg=%s",
                    type(exc).__name__,
                    exc,
                )

    async def _emit_hourly_digest(self) -> None:
        """Build + log the [ALERT] hourly session-status line. Edge-suppressed."""
        now = datetime.now(UTC)
        if _in_session_edge_window(now, edge_minutes=0):
            LOGGER.debug("[ORCH] hourly_digest: suppressed — session edge window")
            return
        window_start = now - timedelta(seconds=_HOURLY_DIGEST_INTERVAL_SEC)

        decisions = [
            d
            for d in self.get_recent_decisions(_DECISION_JOURNAL_MAXLEN)
            if _ts_in(d.get("ts"), window_start, now)
        ]
        reconciliations = _read_recent_reconciliations(window_start, now)
        suppressed = sum(1 for ts in self._suppressed_entry_ts if window_start <= ts <= now)

        # Position state — broker is source of truth (§0.5.98). Best-effort.
        pos_str = "?"
        try:
            summary = await self.get_broker_status_summary()
            live = [p for p in summary.get("positions", []) if p.get("qty")]
            if not live:
                pos_str = "FLAT"
            else:
                pos_str = "+".join(f"{p.get('symbol')}x{p.get('qty')}" for p in live)
        except Exception as exc:
            LOGGER.debug("[ORCH] hourly_digest: pos lookup failed — %s", exc)

        feed_ok = (
            self._last_bar_at is not None
            and (now - self._last_bar_at).total_seconds() <= _DIGEST_FEED_OK_SEC
        )
        readiness = self._readiness_fragment(now)
        line = build_hourly_digest(
            window_start=window_start,
            window_end=now,
            decisions=decisions,
            reconciliations=reconciliations,
            pos_str=pos_str,
            feed_ok=feed_ok,
            suppressed_count=suppressed,
            readiness=readiness,
        )
        LOGGER.info("[ALERT] hourly_session_digest: %s", line)

    def _readiness_fragment(self, now: datetime) -> str:
        """W-S15.3 Track E — 'is it ready to trade' at a glance, from in-process
        state only (no broker round-trip). Warmup (bars-seen / needed + ready or
        warming), last-bar age, and the deployed commit. Best-effort: any missing
        field degrades to a placeholder rather than failing the digest."""
        bars = getattr(self._strategy, "bar_count", 0)
        last_decision = getattr(self._strategy, "last_decision", None) or {}
        warming = (
            str(last_decision.get("decision", "")) == "noop_warmup" or bars < _WARMUP_BARS_NEEDED
        )
        if warming:
            eta_min = max(0, _WARMUP_BARS_NEEDED - bars)
            warm_str = f"warming {bars}/{_WARMUP_BARS_NEEDED} bars (~{eta_min}m)"
        else:
            warm_str = f"ready ({bars} bars)"
        if self._last_bar_at is not None:
            bar_age = f"{(now - self._last_bar_at).total_seconds():.0f}s"
        else:
            bar_age = "n/a"
        commit = (os.environ.get("TRADEFLOW_COMMIT") or "unknown")[:8]
        return f"warmup={warm_str} last_bar={bar_age} commit={commit}"

    async def _handle_trade_signal(self, signal: Signal) -> None:
        # PR #12 — drop new entries while halted (foreign-position detection
        # by Reconciler raises the flag; operator clears via Supabase halt_acks
        # row or /tmp/halt_clear file flag).
        if self.is_halted():
            LOGGER.warning(
                "[ORCH] signal: dropped — halt_new_entries=True signal=%s",
                signal.direction,
            )
            return
        try:
            await self._router.place_entry(signal, self._contract)
        except InvariantViolationError:
            # Track 5b — the strategy agreed (long_signal) but a non-CLOSED
            # lifecycle already exists for this symbol/strategy: we're already
            # in a position. create_lifecycle rejects it BEFORE any order is
            # placed, so no broker state changes. Record it for the hourly
            # digest aggregate; do NOT alert per-bar.
            self._suppressed_entry_ts.append(datetime.now(UTC))
            LOGGER.info(
                "[ORCH] %s: long_signal suppressed — position already open "
                "(aggregated into hourly digest)",
                signal.instrument,
            )
        except Exception as exc:
            LOGGER.error(
                "[EXEC] %s: place_entry_dispatch_failed — type=%s msg=%s",
                signal.instrument,
                type(exc).__name__,
                exc,
            )

    # ------------------------------------------------------------- halt API
    # PR #12 — public halt coordinator surface consumed by Reconciler.
    # _halt_new_entries remains the in-memory boolean source of truth;
    # _halt_raised_at is the timestamp the ack poll compares against.

    def raise_halt(self, symbol: str | None = None) -> None:
        """Raise the global halt — block new entries, stamp ``_halt_raised_at``.

        Idempotent: calling twice in a row only logs once at INFO for the second
        call so the operator sees one warning per distinct halt event.
        """
        if self._halt_new_entries:
            LOGGER.info("[ORCH] halt_already_raised: symbol=%s — no-op", symbol or "<unknown>")
            return
        self._halt_new_entries = True
        self._halt_raised_at = datetime.now(UTC)
        LOGGER.warning(
            "[ORCH] halt_raised: symbol=%s — halting new entries",
            symbol or "<unknown>",
        )
        # PR #14 — sibling alert line for Telegram subsystem.
        LOGGER.info("[ALERT] halt_raised: symbol=%s", symbol or "<unknown>")

    def clear_halt(self, reason: str = "") -> None:
        """Clear the global halt — resume new entries, log ``reason`` at INFO.

        No-op if not currently halted. Always resets ``_halt_raised_at`` to None
        so a subsequent ``halt_raised_at()`` returns None.
        """
        if not self._halt_new_entries:
            return
        self._halt_new_entries = False
        self._halt_raised_at = None
        LOGGER.info(
            "[ORCH] halt_cleared: reason=%s — resuming new entries",
            reason or "<no reason>",
        )
        # PR #14 — sibling alert line for Telegram subsystem.
        LOGGER.info("[ALERT] halt_acked: reason=%s", reason or "<no reason>")

    async def get_broker_status_summary(self) -> dict[str, Any]:
        """Read-only broker snapshot for the ``/status`` telegram command.

        Pulls from broker (per §0.5.123 / §0.5.98) — never reads lifecycles
        as the source of truth. Returns small primitives so the alerter can
        format them without further IBKR knowledge.
        """
        positions = await self._ib.get_positions()
        open_trades = await self._ib.get_open_trades()
        summary: dict[str, Any] = {
            "positions": [
                {
                    "symbol": getattr(getattr(p, "contract", None), "localSymbol", "?"),
                    "qty": getattr(p, "position", 0),
                    "avg_cost": getattr(p, "avgCost", 0.0),
                }
                for p in positions
            ],
            "open_trades_count": len(open_trades),
            "account": self._account_prefix(self._paper_account),
            "live_bars_60m": self._count_live_bars_last_60m(),
        }
        try:
            account_summary = await self._ib._ib.accountSummaryAsync(self._paper_account)
            net_liq = self._extract_net_liquidation(account_summary)
            summary["net_liq"] = net_liq or "?"
        except Exception as exc:
            LOGGER.warning("[ORCH] status_net_liq_failed: %r", exc)
            summary["net_liq"] = "?"
        return summary

    async def insert_halt_ack(self, note: str) -> dict[str, Any]:
        """Thin pass-through to :meth:`SupabaseClient.insert_halt_ack`.

        Exists on Orchestrator so the telegram :class:`OperatorCoordinator`
        Protocol stays orchestrator-shaped — comms code never imports
        ``src.clients.*`` directly.
        """
        return await self._db.insert_halt_ack(note=note)

    # ----------------------------------------------------- manual close (PR #16)

    async def flatten_all(self) -> FlattenResult:
        """Close every non-CLOSED lifecycle at MKT. Operator-initiated.

        Lifecycle-driven enumeration mirrors :class:`EodForceClose` rather than
        querying the broker, so a foreign position with no DB lifecycle is NOT
        flattened by this path — the operator must still SSH for that edge case
        (and the reconciler will already have raised a halt). One ``[ALERT]
        flatten_requested`` precedes the per-symbol :meth:`close_position`
        calls; one ``[ALERT] flatten_complete`` follows. Errors per-symbol are
        caught so partial flatten is reported in the result.
        """
        lifecycles = await self._sm.load_non_closed()
        symbols = [lc.symbol for lc in lifecycles]
        LOGGER.info(
            "[ALERT] flatten_requested: symbols=%s count=%d",
            ",".join(symbols) if symbols else "<none>",
            len(symbols),
        )
        results: list[CloseResult] = []
        for symbol in symbols:
            try:
                res = await self._router.close_position(symbol, reason="manual_flatten")
                results.append(res)
            except Exception as exc:
                LOGGER.error(
                    "[ORCH] flatten_all: close_error — symbol=%s type=%s msg=%s",
                    symbol,
                    type(exc).__name__,
                    exc,
                )
                results.append(
                    CloseResult(
                        closed=False,
                        symbol=symbol,
                        status="error",
                        close_reason="manual_flatten",
                    )
                )
        closed_count = sum(1 for r in results if r.closed)
        LOGGER.info(
            "[ALERT] flatten_complete: closed=%d total=%d",
            closed_count,
            len(symbols),
        )
        return FlattenResult(requested_symbols=symbols, closed=results)

    async def exit_symbol(self, symbol: str) -> ExitResult:
        """Close one non-CLOSED lifecycle matching ``symbol`` at MKT.

        Single ``[ALERT] exit_requested`` precedes the close; the
        ``[ALERT] exit_complete`` line is emitted by
        :meth:`OrderRouter.close_position` itself (do not double-log here).
        """
        LOGGER.info("[ALERT] exit_requested: symbol=%s", symbol)
        try:
            result = await self._router.close_position(symbol, reason="manual_exit_symbol")
        except Exception as exc:
            LOGGER.error(
                "[ORCH] exit_symbol: close_error — symbol=%s type=%s msg=%s",
                symbol,
                type(exc).__name__,
                exc,
            )
            result = CloseResult(
                closed=False,
                symbol=symbol,
                status="error",
                close_reason="manual_exit_symbol",
            )
        return ExitResult(symbol=symbol, result=result)

    def is_halted(self) -> bool:
        """Return whether the global halt is currently raised."""
        return self._halt_new_entries

    def halt_raised_at(self) -> datetime | None:
        """Return the timestamp the current halt was raised, or None if clear."""
        return self._halt_raised_at

    def _launch_background_tasks(self) -> None:
        assert self._stop_event is not None
        eod_task = asyncio.create_task(
            self._eod.run_until_stopped(self._stop_event),
            name="tradeflow-eod-force-close",
        )
        self._background_tasks.append(eod_task)
        LOGGER.info("[EOD] task_launched — name=tradeflow-eod-force-close")
        recon_task = asyncio.create_task(
            self._reconciler.run_until_stopped(self._stop_event),
            name="tradeflow-reconciler",
        )
        self._background_tasks.append(recon_task)
        LOGGER.info("[RECON] task_launched — name=tradeflow-reconciler")
        # Track 4 — SeanBot reconciliation poll loop (read-only; never trades).
        seanbot_task = asyncio.create_task(
            self._seanbot_reconciler.run_until_stopped(self._stop_event, self._db),
            name="tradeflow-seanbot-reconciler",
        )
        self._background_tasks.append(seanbot_task)
        LOGGER.info("[RECON] task_launched — name=tradeflow-seanbot-reconciler")
        # Track 5a — hourly session-status digest (read-only; emits one [ALERT]
        # per interval during session hours; the existing alerter relays it).
        digest_task = asyncio.create_task(
            self._hourly_digest_loop(),
            name="tradeflow-hourly-digest",
        )
        self._background_tasks.append(digest_task)
        LOGGER.info("[ORCH] task_launched — name=tradeflow-hourly-digest")
        # PR #14 — telegram subsystem (optional; only when env vars are set).
        if self._telegram is not None and self._loop is not None:
            self._telegram.install_handler(self._loop, logging.getLogger())
            alert_task = asyncio.create_task(
                self._telegram.alert_loop(self._stop_event),
                name="tradeflow-telegram-alert",
            )
            self._background_tasks.append(alert_task)
            cmd_task = asyncio.create_task(
                self._telegram.command_loop(self._stop_event),
                name="tradeflow-telegram-command",
            )
            self._background_tasks.append(cmd_task)
        # PR #18 — dashboard server. Failure here must not bring down the
        # orchestrator (e.g. missing DASHBOARD_USERNAME/PASSWORD env vars).
        # load_credentials() is called synchronously so missing-env-var fails
        # fast here (before scheduling the task), surfacing as launch_failed.
        try:
            from dashboard.server import load_credentials, run_uvicorn

            load_credentials()
            dash_task = asyncio.create_task(run_uvicorn(self), name="tradeflow-dashboard")
            self._background_tasks.append(dash_task)
            LOGGER.info("[ORCH] dashboard: task_launched")
        except Exception:
            LOGGER.exception("[ORCH] dashboard: launch_failed — continuing without dashboard")

    async def _broker_field_updates_for(
        self, lifecycle: Lifecycle, target: State
    ) -> dict[str, Any]:
        # Best-effort re-bind to broker reality on ENTERING → ACTIVE drift.
        # entry_filled_at uses wall-clock; reqExecutions probe for exact fill
        # timestamp lands in PR #10 reconciliation.
        if State(lifecycle.state) is State.ENTERING and target is State.ACTIVE:
            positions = await self._ib.get_positions()
            qty, avg_cost = _broker_qty_and_avg_cost(positions, lifecycle.symbol)
            return {
                "entry_qty": qty,
                "entry_price": avg_cost,
                "entry_filled_at": datetime.now(UTC).isoformat(),
            }
        return {}

    async def _healthcheck_once(self) -> None:
        ib_time = await self._ib._ib.reqCurrentTimeAsync()
        LOGGER.info("[ORCH] healthcheck: ok — ib_time=%s", self._format_ib_time(ib_time))
        self._watchdog_check_bar_liveness()

    async def _shutdown(self, exit_code: int) -> None:
        await self._cancel_background_tasks()
        try:
            self._ib.disconnect()
            LOGGER.info("[ORCH] shutdown: ib_disconnected")
        except Exception as exc:
            LOGGER.warning(
                "[ORCH] shutdown: ib_disconnect_error — type=%s msg=%s",
                type(exc).__name__,
                exc,
            )
        try:
            await self._db.close()
            LOGGER.info("[ORCH] shutdown: db_closed")
        except Exception as exc:
            LOGGER.warning(
                "[ORCH] shutdown: db_close_error — type=%s msg=%s",
                type(exc).__name__,
                exc,
            )
        duration = (
            f"{time.monotonic() - self._started_at:.1f}" if self._started_at is not None else "?"
        )
        LOGGER.info(
            "[ORCH] shutdown: done — exit_code=%s duration_sec=%s",
            exit_code,
            duration,
        )

    async def _cancel_background_tasks(self) -> None:
        if not self._background_tasks:
            return
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        for task in self._background_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                LOGGER.warning(
                    "[ORCH] shutdown: background_task_error — name=%s type=%s msg=%s",
                    task.get_name(),
                    type(exc).__name__,
                    exc,
                )
        self._background_tasks.clear()

    def _install_signal_handlers(self) -> None:
        # asyncio.add_signal_handler does not work in Docker pid=1 / minimal
        # images. Use signal.signal + call_soon_threadsafe to set the event.
        try:
            signal.signal(signal.SIGTERM, self._handle_signal)
            signal.signal(signal.SIGINT, self._handle_signal)
        except ValueError:
            # signal.signal must be called from main thread; tests may bypass.
            LOGGER.debug("[ORCH] startup: signal_handlers_skipped — not_main_thread")

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        name = (
            signal.Signals(signum).name
            if signum in signal.Signals.__members__.values()
            else str(signum)
        )
        LOGGER.info("[ORCH] shutdown: signal_received — signal=%s", name)
        if self._stop_event is None or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._stop_event.set)

    def _safe_server_version(self) -> str:
        try:
            return str(self._ib._ib.client.serverVersion())
        except Exception:
            return "unknown"

    @staticmethod
    def _account_prefix(account: str) -> str:
        return account[:3] if account else "?"

    @staticmethod
    def _extract_net_liquidation(summary: list[Any]) -> str | None:
        for row in summary:
            tag = getattr(row, "tag", None)
            if tag == "NetLiquidation":
                return getattr(row, "value", None)
        return None

    @staticmethod
    def _format_ib_time(ib_time: Any) -> str:
        try:
            return ib_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        except AttributeError:
            return str(ib_time)


def _build_telegram_if_configured(
    orchestrator: Orchestrator, *, db: SupabaseClient
) -> TelegramAlerter | None:
    """Construct a :class:`TelegramAlerter` if both env vars are set; else None.

    PR #14 — operator opts in by adding ``TELEGRAM_BOT_TOKEN`` +
    ``TELEGRAM_OPERATOR_CHAT_ID`` to ``~/.tradeflow-secrets/.env``. Until those
    land, the orchestrator boots without telegram and logs the disable line so
    the absence is visible in ``docker logs``.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id_raw = os.environ.get("TELEGRAM_OPERATOR_CHAT_ID", "").strip()
    if not token or not chat_id_raw:
        LOGGER.info("[ORCH] telegram_disabled: env vars not set")
        return None
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        LOGGER.warning(
            "[ORCH] telegram_disabled: TELEGRAM_OPERATOR_CHAT_ID not int — value=%r",
            chat_id_raw,
        )
        return None
    LOGGER.info("[ORCH] telegram_enabled: chat_id=%s", chat_id)
    # db is unused inside this helper but accepted so future flavors (e.g. a
    # direct SupabaseClient handle for /ack) can wire in without refactoring
    # the caller. Today the alerter goes through the orchestrator coordinator.
    _ = db
    return TelegramAlerter(bot_token=token, operator_chat_id=chat_id, coordinator=orchestrator)


def _ts_in(ts_iso: str | None, start: datetime, end: datetime) -> bool:
    """True if an ISO-8601 timestamp falls within [start, end] (UTC-aware)."""
    if not ts_iso:
        return False
    try:
        dt = datetime.fromisoformat(ts_iso)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return start <= dt <= end


def _read_recent_reconciliations(
    start: datetime, end: datetime, *, path: str = _RECON_JSONL_PATH
) -> list[dict]:
    """Read the SeanBot reconciliation journal, returning records whose
    ``signal_ts`` falls in [start, end]. Best-effort — a missing/locked/corrupt
    journal yields an empty list (the digest still emits its decision stats)."""
    records: list[dict] = []
    try:
        p = pathlib.Path(path)
        if not p.exists():
            return records
        with p.open() as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if _ts_in(rec.get("signal_ts"), start, end):
                    records.append(rec)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("[ORCH] hourly_digest: reconciliation read skipped — %s", exc)
    return records


def build_hourly_digest(
    *,
    window_start: datetime,
    window_end: datetime,
    decisions: list[dict],
    reconciliations: list[dict],
    pos_str: str,
    feed_ok: bool,
    suppressed_count: int,
    readiness: str = "",
) -> str:
    """Format the one-line hourly session-status digest body (pure, testable).

    Mirrors the [STRAT] eval decision labels (long_signal / noop_filter with
    per-gate failed breakdown / noop_regime / noop_warmup / noop_cooldown /
    noop_session_edge) plus the SeanBot AGREE/MISS scorecard for the window.

    ``readiness`` (W-S15.3 Track E) is an optional pre-formatted fragment
    (warmup / last-bar-age / commit); appended when non-empty.
    """
    label = f"{window_start:%H:%M}–{window_end:%H:%M}Z"
    counts: dict[str, int] = {}
    gate_fail: dict[str, int] = {}
    for d in decisions:
        decision = str(d.get("decision", "unknown"))
        counts[decision] = counts.get(decision, 0) + 1
        if decision == "noop_filter":
            failed = str(d.get("failed", "unknown"))
            gate_fail[failed] = gate_fail.get(failed, 0) + 1

    evals = len(decisions)
    long_signal = counts.get("long_signal", 0)
    noop_filter = counts.get("noop_filter", 0)
    gate_str = " ".join(
        f"{g}={gate_fail.get(g, 0)}" for g in ("touch", "bullish", "ma_order", "gap")
    )
    regime = counts.get("noop_regime", 0)
    warmup = counts.get("noop_warmup", 0)
    cooldown = counts.get("noop_cooldown", 0)
    edge = counts.get("noop_session_edge", 0)

    # SeanBot scorecard: AGREE_ENTER vs the MISS-* classes, grouped.
    sb_entries = len(reconciliations)
    agree = sum(1 for r in reconciliations if str(r.get("classification", "")).startswith("AGREE"))
    miss_groups: dict[str, int] = {}
    for r in reconciliations:
        cls = str(r.get("classification", ""))
        if cls.startswith("MISS"):
            miss_groups[cls] = miss_groups.get(cls, 0) + 1
    miss_str = ", ".join(f"{n} {cls}" for cls, n in sorted(miss_groups.items()))
    sb_str = f"{sb_entries} entries"
    if sb_entries:
        sb_str += f" → {agree} AGREE" + (f", {miss_str}" if miss_str else "")

    body = (
        f"\U0001f4ca TradeFlow hourly {label} | pos={pos_str} | "
        f"evals={evals}: long_signal={long_signal} noop_filter={noop_filter} ({gate_str}) "
        f"regime={regime} warmup={warmup} cooldown={cooldown} edge={edge} | "
        f"SeanBot: {sb_str} | suppressed_in_position={suppressed_count} | "
        f"feed {'OK' if feed_ok else 'STALE'}"
    )
    if readiness:
        body += f" | {readiness}"
    return body


def _build_contract(instrument: str) -> Contract:
    """Build an ib_async ``Contract`` for ``instrument`` (e.g. ``"MNQM6"``).

    Resolution rule: a localSymbol that begins with ``MNQ`` is the front-month
    MNQ future. PR #11 will add contract-roll awareness; for PR #10 the
    instrument is supplied externally by the orchestrator's caller.
    """
    if instrument.startswith(MNQ.symbol):
        return Future(
            symbol=MNQ.symbol,
            exchange=MNQ.exchange,
            currency=MNQ.currency,
            localSymbol=instrument,
        )
    c = Contract()
    c.symbol = instrument
    c.secType = "FUT"
    c.exchange = MNQ.exchange
    c.currency = MNQ.currency
    return c


def _broker_qty_and_avg_cost(positions: list[Any], symbol: str) -> tuple[int, float]:
    """Return (qty, avg_cost) for the matching position; (0, 0.0) if missing.

    avgCost is coerced defensively — recovery must not crash on a mock or a
    None value; falls back to 0.0 and lets the next-tick reconciliation probe
    correct the price.
    """
    for pos in positions:
        contract = getattr(pos, "contract", None)
        if contract is None:
            continue
        local = getattr(contract, "localSymbol", None)
        base = getattr(contract, "symbol", None)
        if symbol not in {local, base}:
            continue
        try:
            qty = int(getattr(pos, "position", 0))
        except (TypeError, ValueError):
            qty = 0
        try:
            avg_cost = float(getattr(pos, "avgCost", 0.0))
        except (TypeError, ValueError):
            avg_cost = 0.0
        return qty, avg_cost
    return 0, 0.0
