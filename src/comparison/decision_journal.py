"""Durable journal of TradeFlow's own per-eval strategy decisions (PR-D3c).

The strategy emits a structured ``last_decision`` every bar (~1/min); the
orchestrator keeps an in-memory ring of them (``_decision_journal``) that is lost
on every container recreate. This module mirrors the analytically-useful subset
of those decisions to the durable Supabase ``strategy_decisions`` table so the
**TF→SeanBot** direction (TF's *own* entries + near-misses) survives recreates,
the same way ``signal_reconciliations`` made the **SeanBot→TF** direction durable
(W-S15.2).

Design (PR brief Constraint 5):
  * 5a — flush is decoupled from the eval/order loop: ``capture`` only appends to
    a bounded in-memory buffer (sync, no network), and ``flush`` batch-upserts on
    the existing hourly digest tick. A flush failure can never touch trading.
  * 5b — only decisions that carry divergence signal are buffered:
    ``decision != "noop_warmup"`` AND the touch-band gate passed (``touch_ok`` is
    True). That is exactly TF's entries (``long_signal``) plus the near-misses
    where price touched the band but another gate blocked entry.

Observe-only and fire-and-forget: ``capture`` and ``flush`` NEVER raise into the
orchestrator loop (same contract as ``seanbot_reconciler._persist_reconciliation``
and ``reconciler._resolve_exit_price``). This module reads the decision the
strategy already produced and persists a copy — it changes no decision, eval, or
order behavior.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

LOGGER = logging.getLogger(__name__)

# Durable mirror of TF's per-eval decisions. Idempotent on the bar identity
# (symbol, decision_ts) so a re-flush/backfill updates the same row instead of
# duplicating. RLS deny-all; written with the service-role client.
_DECISIONS_TABLE = "strategy_decisions"
_DECISIONS_ON_CONFLICT = "symbol,decision_ts"

# Bound the in-memory buffer so an extended Supabase outage cannot grow it
# without limit; drop-oldest (a forward-measurement scorecard tolerates losing
# the stalest un-flushed rows).
_BUFFER_MAX = 5000


def decision_to_row(decision: dict, *, symbol: str, bar_count: int | None) -> dict[str, Any]:
    """Map a strategy ``last_decision`` dict to a ``strategy_decisions`` row.

    Pure — no IO. ``symbol`` and ``bar_count`` come from the orchestrator (the
    decision dict carries neither; the strategy is single-instrument). The full
    decision is dumped to ``raw`` (jsonb) so the row stays forward-proof if the
    strategy grows new decision fields before this writer is updated.
    """
    return {
        "decision_ts": decision.get("ts"),
        "symbol": symbol,
        "bar_count": bar_count,
        "decision": decision.get("decision"),
        "failed_gate": decision.get("failed"),
        "close": decision.get("close"),
        "sma100": decision.get("sma100"),
        "regime_ok": decision.get("regime_ok"),
        "touch_ok": decision.get("touch_ok"),
        "ma_order_ok": decision.get("ma_order_ok"),
        "bullish_ok": decision.get("bullish_ok"),
        "gap_ok": decision.get("gap_ok"),
        "raw": decision,
    }


class DecisionJournal:
    """Bounded buffer of TF's analytically-useful decisions + a durable flush.

    ``capture`` is sync and runs in the bar callback path; ``flush`` is async and
    runs on the hourly digest tick. Neither raises.
    """

    def __init__(self, *, buffer_max: int = _BUFFER_MAX) -> None:
        self._buffer: deque[dict] = deque(maxlen=buffer_max)
        # Rows dropped (drop-oldest) since the last successful/attempted flush —
        # surfaced in the flush log so silent loss is visible.
        self._dropped_since_flush = 0

    @property
    def buffered(self) -> int:
        """Number of rows currently waiting to be flushed."""
        return len(self._buffer)

    def _is_useful(self, decision: dict) -> bool:
        """PR brief 5b gate: keep entries + touch-band near-misses only.

        ``touch_ok`` is True only on a post-warmup bar whose price touched the
        band (so ``noop_warmup`` / cooldown / session-edge / regime-blocked bars,
        which leave ``touch_ok`` None, are excluded). The explicit warmup check is
        belt-and-suspenders against a future label that sets ``touch_ok`` early.
        """
        if decision.get("decision") == "noop_warmup":
            return False
        return decision.get("touch_ok") is True

    def capture(self, decision: dict | None, *, symbol: str, bar_count: int | None = None) -> None:
        """Buffer one decision if it carries divergence signal. Never raises."""
        try:
            if not decision:
                return
            if not self._is_useful(decision):
                return
            if len(self._buffer) == self._buffer.maxlen:
                # deque drops the oldest on append; count it so flush can report.
                self._dropped_since_flush += 1
            row = decision_to_row(decision, symbol=symbol, bar_count=bar_count)
            self._buffer.append(row)
            LOGGER.debug(
                "[DECJRNL] %s: buffered decision=%s gates=touch:%s/ma_order:%s/"
                "bullish:%s/gap:%s buffered=%d",
                symbol,
                row.get("decision"),
                row.get("touch_ok"),
                row.get("ma_order_ok"),
                row.get("bullish_ok"),
                row.get("gap_ok"),
                len(self._buffer),
            )
        except Exception as exc:  # noqa: BLE001 — observe-only, never break the bar path
            LOGGER.warning(
                "[DECJRNL] capture failed — %s: %s (swallowed)",
                type(exc).__name__,
                exc,
            )

    async def flush(self, db: Any) -> int:
        """Batch-upsert the buffered rows to ``strategy_decisions``. Never raises.

        Returns the number of rows flushed (0 on empty buffer or on a swallowed
        failure). On success the flushed rows are removed and the dropped counter
        reset; on failure the buffer is preserved for the next tick.
        """
        n = len(self._buffer)
        if n == 0:
            return 0
        pending = list(self._buffer)
        dropped = self._dropped_since_flush
        try:
            await db.upsert(_DECISIONS_TABLE, pending, on_conflict=_DECISIONS_ON_CONFLICT)
        except Exception as exc:  # noqa: BLE001 — observability must never break the loop
            LOGGER.warning(
                "[DECJRNL] flush failed — %s: %d buffered, %d dropped (swallowed)",
                type(exc).__name__,
                n,
                dropped,
            )
            return 0
        # Remove exactly the rows we flushed; any captured during the await stay.
        for _ in range(n):
            try:
                self._buffer.popleft()
            except IndexError:
                break
        self._dropped_since_flush = 0
        LOGGER.info(
            "[DECJRNL] flushed %d rows → %s (dropped=%d)",
            n,
            _DECISIONS_TABLE,
            dropped,
        )
        return n
