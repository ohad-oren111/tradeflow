"""Automated SeanBot-vs-TradeFlow reconciliation (Track 4, app-side).

When SeanBot ("Trading NQ Triggers") posts an ENTRY, this reconciles it against
what TradeFlow's *own* strategy decided at the nearest bar and emits a Telegram
``[ALERT]`` plus a durable-ish JSONL record. TradeFlow never trades on SeanBot —
SeanBot is a benchmark; this is read-only observation.

Runs INSIDE the orchestrator process (``tradeflow-app``) — that is where the
Track 3 decision journal lives. The SeanBot signal is read from the shared
Supabase ``seanbot_signals`` table (the listener, a separate container, writes
it there), so no cross-container memory access is needed.

Classifications:
  AGREE_ENTER          TradeFlow also fired long_signal near signal_ts
  MISS-regime          TradeFlow blocked at the C1 regime gate
  MISS-filter:<gate>   a single entry filter failed (touch/ma_order/bullish/gap)
  MISS-session-edge    outside TradeFlow's tradable session window
  MISS-<decision>      any other noop (warmup, cooldown)
  MISS-NO-BAR          TradeFlow had no decision within tolerance — feed
                       stale/dead (the blind-feed detector)
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from src.journal_rotation import rotate_jsonl_if_large

LOGGER = logging.getLogger(__name__)

_TOLERANCE_SEC = 120.0
_POLL_INTERVAL_SEC = 30.0
_RECON_JSONL_PATH = "/app/logs/reconciliations.jsonl"
# W-S15.2 — durable mirror of the (ephemeral) JSONL journal. Idempotent on the
# SeanBot signal identity (channel, message_id), so a re-poll/backfill updates
# the same row instead of duplicating. RLS deny-all; written with service-role.
_RECON_TABLE = "signal_reconciliations"
_RECON_ON_CONFLICT = "channel,message_id"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def nearest_decision(
    decisions: list[dict], signal_ts: datetime, *, tolerance_sec: float = _TOLERANCE_SEC
) -> dict | None:
    """Return the decision at-or-before ``signal_ts`` within ``tolerance_sec``.

    ``decisions`` is the journal (any order). Picks the smallest non-negative
    gap (decision ts <= signal ts). None if every decision is too old, in the
    future, or the journal is empty (→ MISS-NO-BAR).
    """
    best: dict | None = None
    best_gap: float | None = None
    for d in decisions:
        dts = _parse_ts(d.get("ts"))
        if dts is None:
            continue
        gap = (signal_ts - dts).total_seconds()
        if 0 <= gap <= tolerance_sec and (best_gap is None or gap < best_gap):
            best, best_gap = d, gap
    return best


def classify(
    signal_row: dict, decisions: list[dict], *, tolerance_sec: float = _TOLERANCE_SEC
) -> dict:
    """Pure reconciliation: classify a SeanBot entry against TradeFlow's decision.

    Returns a record dict (no side effects). ``tf_decision`` is the matched
    journal entry or None.
    """
    signal_ts = _parse_ts(signal_row.get("ts")) or datetime.now(UTC)
    tf = nearest_decision(decisions, signal_ts, tolerance_sec=tolerance_sec)

    if tf is None:
        classification = "MISS-NO-BAR"
        justification = (
            "TradeFlow had no live bar within "
            f"{int(tolerance_sec)}s of the signal — feed stale/dead"
        )
    else:
        decision = tf.get("decision")
        close = tf.get("close")
        sma100 = tf.get("sma100")
        if decision == "long_signal":
            classification = "AGREE_ENTER"
            justification = f"TradeFlow also fired long_signal (close={close} sma100={sma100})"
        elif decision == "noop_regime":
            classification = "MISS-regime"
            justification = f"blocked at C1 regime gate (close={close} sma100={sma100})"
        elif decision == "noop_filter":
            failed = tf.get("failed")
            classification = f"MISS-filter:{failed}"
            justification = f"filter '{failed}' failed (close={close} sma100={sma100})"
        elif decision == "noop_session_edge":
            classification = "MISS-session-edge"
            justification = "outside TradeFlow's tradable session window"
        else:
            classification = f"MISS-{decision}"
            justification = f"TradeFlow decision was {decision} (close={close} sma100={sma100})"

    return {
        "signal_ts": signal_ts.isoformat(),
        "seanbot": {
            "channel": signal_row.get("channel"),
            "message_id": signal_row.get("message_id"),
            "type": signal_row.get("type"),
            "direction": signal_row.get("direction"),
            "symbol": signal_row.get("symbol"),
            "price": signal_row.get("price"),
        },
        "tf_decision": tf,
        "classification": classification,
        "justification": justification,
        "acknowledged_at": datetime.now(UTC).isoformat(),
    }


def record_to_row(record: dict) -> dict:
    """Flatten a reconciliation ``record`` (from :func:`classify`) into a
    ``signal_reconciliations`` row. Pure — no IO. The natural key
    (``channel``, ``message_id``) mirrors ``seanbot_signals`` and drives the
    upsert's ``on_conflict``."""
    sb = record.get("seanbot") or {}
    return {
        "signal_ts": record.get("signal_ts"),
        "channel": sb.get("channel"),
        "message_id": sb.get("message_id"),
        "seanbot_type": sb.get("type"),
        "direction": sb.get("direction"),
        "symbol": sb.get("symbol"),
        "price": sb.get("price"),
        "classification": record.get("classification"),
        "justification": record.get("justification"),
        "tf_decision": record.get("tf_decision"),
        "acknowledged_at": record.get("acknowledged_at"),
    }


def evaluate_sb_trigger(
    *,
    direction: str | None,
    symbol: str | None,
    sb_price: float | None,
    current_price: float | None,
    sma100: float | None,
    near_below_pts: float,
    near_above_pts: float,
    no_chase_max_pts: float,
) -> tuple[bool, str]:
    """Pure validity check for the REPLICATE SeanBot-triggered entry path.

    Returns ``(ok, reason)``. ``ok`` is True only if ALL hold: the signal is a
    LONG MNQ entry, indicators are warm (``sma100`` present), the *current* price
    is inside the near-MA window ``[sma100 - near_below, sma100 + near_above]``,
    and it is NOT a stale chase (current price is not more than ``no_chase_max``
    points above SeanBot's signal price). ``current_price`` is TradeFlow's
    freshest SETTLED-bar truth (no look-ahead) supplied by the caller.

    Halt and FLAT/no-stack are NOT checked here — they are enforced downstream by
    the orchestrator entry path (``is_halted`` drop + ``create_lifecycle`` reject).
    Pure: no IO, no side effects, fully unit-testable.
    """
    if (direction or "").strip().lower() != "long":
        return False, f"not-long:{direction!r}"
    if not (symbol or "").strip().upper().startswith("MNQ"):
        return False, f"not-mnq:{symbol!r}"
    if sb_price is None or current_price is None:
        return False, "no-price"
    if sma100 is None:
        return False, "warmup-no-sma"
    delta_ma = current_price - sma100
    if delta_ma < -near_below_pts or delta_ma > near_above_pts:
        return False, f"far-from-ma:{delta_ma:+.2f}"
    drift = current_price - sb_price
    if drift > no_chase_max_pts:
        return False, f"stale-chase:{drift:+.2f}"
    return True, f"ok:px-sma={delta_ma:+.2f},px-sb={drift:+.2f}"


class SeanbotReconciler:
    """Polls Supabase ``seanbot_signals`` for new entries and reconciles each.

    Decisions come from ``decisions_getter`` (the orchestrator's
    ``get_recent_decisions``). Idempotent per ``(channel, message_id)`` so a
    listener backfill / restart never double-alerts.
    """

    def __init__(
        self,
        *,
        decisions_getter: Callable[[], list[dict]],
        tolerance_sec: float = _TOLERANCE_SEC,
        poll_interval_sec: float = _POLL_INTERVAL_SEC,
        journal_path: str = _RECON_JSONL_PATH,
        entry_handler: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        self._decisions_getter = decisions_getter
        self._tolerance_sec = tolerance_sec
        self._poll_interval_sec = poll_interval_sec
        self._journal_path = journal_path
        # REPLICATE — optional async callback invoked once per freshly-reconciled
        # (deduped) entry. The orchestrator wires this to its validity-checked
        # SeanBot-triggered entry path. None keeps the reconciler observe-only.
        self._entry_handler = entry_handler
        self._seen: set[tuple[Any, Any]] = set()
        # Only reconcile signals captured after startup — set on first poll.
        self._cursor_iso: str | None = None

    def reconcile_row(self, signal_row: dict) -> dict | None:
        """Classify one ``seanbot_signals`` row, alert + journal it.

        Returns the reconciliation record, or None if the row is not an entry
        or was already reconciled (idempotency).
        """
        if signal_row.get("type") != "entry":
            return None
        key = (signal_row.get("channel"), signal_row.get("message_id"))
        if key in self._seen:
            return None
        self._seen.add(key)

        record = classify(signal_row, self._decisions_getter(), tolerance_sec=self._tolerance_sec)
        self._emit_alert(record)
        self._append_journal(record)
        return record

    def _emit_alert(self, record: dict) -> None:
        sb = record["seanbot"]
        tf = record["tf_decision"]
        if tf is not None:
            failed = tf.get("failed")
            tf_str = f"{tf.get('decision')}" + (f" failed={failed}" if failed else "")
            tf_str += f" (close={tf.get('close')} sma100={tf.get('sma100')})"
        else:
            tf_str = "no-bar"
        LOGGER.info(
            "[ALERT] seanbot_reconciliation: SeanBot ENTRY %s %s @%s ts=%s | TF=%s | class=%s",
            sb.get("direction"),
            sb.get("symbol"),
            sb.get("price"),
            record["signal_ts"],
            tf_str,
            record["classification"],
        )

    async def _persist_reconciliation(self, db: Any, record: dict) -> None:
        """Upsert ``record`` to Supabase ``signal_reconciliations`` (durable
        store that survives container recreates, unlike the JSONL). Keyed on
        ``(channel, message_id)`` so a re-poll updates the same row.

        Observability must never break the eval loop: a write failure is logged
        as a warning and swallowed — the JSONL append is the local fallback.
        """
        row = record_to_row(record)
        try:
            await db.upsert(_RECON_TABLE, row, on_conflict=_RECON_ON_CONFLICT)
            LOGGER.info(
                "[RECON-CMP] %s: persisted reconciliation %s — seanbot_ts=%s",
                row.get("symbol"),
                row.get("classification"),
                row.get("signal_ts"),
            )
        except Exception as exc:
            LOGGER.warning(
                "[RECON-CMP] %s: reconciliation persist failed — type=%s msg=%s",
                row.get("symbol"),
                type(exc).__name__,
                exc,
            )

    def _append_journal(self, record: dict) -> None:
        """Best-effort durable record — IO failure must not break the loop."""
        try:
            path = pathlib.Path(self._journal_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Track 6b — bound the unbounded JSONL on long-lived containers.
            rotate_jsonl_if_large(path)
            with path.open("a") as fh:
                fh.write(json.dumps(record) + "\n")
        except Exception as exc:
            LOGGER.debug("[RECON] reconciliation journal write skipped — %s", exc)

    async def poll_once(self, db: Any) -> int:
        """Fetch new entry signals since the cursor and reconcile each.

        Returns the number of new entries reconciled. On the first call the
        cursor is seeded to "now" so historical rows are not re-alerted.
        """
        if self._cursor_iso is None:
            self._cursor_iso = datetime.now(UTC).isoformat()
            return 0
        filters = {
            "type": "eq.entry",
            "created_at": f"gt.{self._cursor_iso}",
            "order": "created_at.asc",
        }
        rows = await db.select("seanbot_signals", filters=filters)
        count = 0
        for row in rows:
            created = row.get("created_at")
            if created and (self._cursor_iso is None or created > self._cursor_iso):
                self._cursor_iso = created
            record = self.reconcile_row(row)
            if record is not None:
                # Durable mirror of the JSONL append (W-S15.2). Best-effort —
                # _persist_reconciliation never raises.
                await self._persist_reconciliation(db, record)
                count += 1
                # REPLICATE — fire the SeanBot-triggered entry path on this fresh
                # entry. reconcile_row deduped already (returns None on a re-poll),
                # so this fires at most once per (channel, message_id). Best-effort:
                # an entry-path error must never break the observe/eval loop.
                if self._entry_handler is not None:
                    try:
                        await self._entry_handler(row)
                    except Exception as exc:
                        LOGGER.warning(
                            "[RECON] seanbot entry_handler error — type=%s msg=%s",
                            type(exc).__name__,
                            exc,
                        )
        return count

    async def run_until_stopped(self, stop_event: asyncio.Event, db: Any) -> None:
        """Poll loop — one reconcile pass every ``poll_interval_sec`` until stop.

        Each pass is wrapped so a transient Supabase error never kills the task;
        the watchdog/PR-A paths handle broker health separately.
        """
        LOGGER.info(
            "[RECON] seanbot_reconciler: started — interval=%.0fs tolerance=%.0fs",
            self._poll_interval_sec,
            self._tolerance_sec,
        )
        while not stop_event.is_set():
            try:
                n = await self.poll_once(db)
                if n:
                    LOGGER.info("[RECON] seanbot_reconciler: reconciled %d new entry signal(s)", n)
            except Exception as exc:
                LOGGER.warning(
                    "[RECON] seanbot_reconciler: poll error — type=%s msg=%s",
                    type(exc).__name__,
                    exc,
                )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval_sec)
            except TimeoutError:
                continue
