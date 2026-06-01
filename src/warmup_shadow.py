"""Shadow SMA warmup backfill (PR 1 — instrumentation only, NO trading change).

On boot the strategy's SMA buffer starts empty and fills one live 1-min bar at a
time, so SMA100 isn't ready for ~100 minutes after every restart — the bot sits
out that window. This module proves we *could* warm the SMA from history WITHOUT
changing any trading behavior: it keeps a parallel "shadow" close buffer seeded
from historical bars, and each warmup bar logs the shadow-backfilled SMA100/MA50
next to the live (buffer-warmed) SMA100 so the future enable can be de-risked
against real convergence numbers.

Observe-only and fire-and-forget — ``seed_from`` and ``observe`` NEVER raise into
boot or the bar loop, and NOTHING here gates a trade. The trade gate still waits
for the live buffer to warm. Flipping to *trade* on the backfilled SMA is a
separate, gated (AUDIT) change.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

LOGGER = logging.getLogger(__name__)

# Match the strategy: ma_slow = sma_100, ma_fast = sma_50 (src/indicators.py).
_SMA_PERIOD = 100
_MA_PERIOD = 50
_BUFFER_MAX = 150
# Stop logging a few bars past live warmup — long enough to show convergence once
# the live SMA appears (~bar 100), then quiet to avoid an endless 1/min log.
_LOG_UNTIL_BAR = _SMA_PERIOD + 5


class WarmupShadow:
    """Parallel close buffer + shadow SMA, logged against the live SMA. No gate."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        sma_period: int = _SMA_PERIOD,
        ma_period: int = _MA_PERIOD,
        buffer_max: int = _BUFFER_MAX,
    ) -> None:
        self.enabled = enabled
        self._sma_period = sma_period
        self._ma_period = ma_period
        self._closes: deque[float] = deque(maxlen=buffer_max)
        self._seeded = 0

    @property
    def seeded(self) -> int:
        return self._seeded

    async def seed_from(self, fetch: Callable[[], Awaitable[list[Any]]]) -> None:
        """Backfill the shadow buffer from ``fetch()`` (historical bars). Never raises.

        ``fetch`` returns objects with a ``.close`` attribute (ib_async ``BarData``).
        A fetch failure (or no entitlement) is logged and skipped — boot/trading
        are never blocked.
        """
        if not self.enabled:
            return
        try:
            bars = await fetch()
            closes = [float(b.close) for b in bars if getattr(b, "close", None) is not None]
            for c in closes:
                self._closes.append(c)
            self._seeded = len(closes)
            LOGGER.info(
                "[WARMUP-SHADOW] seeded backfill_closes=%d buffered=%d sma100_ready=%s",
                self._seeded,
                len(self._closes),
                len(self._closes) >= self._sma_period,
            )
        except Exception as exc:  # noqa: BLE001 — backfill must never block boot/trading
            LOGGER.warning(
                "[WARMUP-SHADOW] seed failed — %s: %s (skipped, no behavior change)",
                type(exc).__name__,
                exc,
            )

    def observe(
        self,
        bar: dict | None,
        live_decision: dict | None,
        bar_count: int | None,
    ) -> dict | None:
        """Append the live bar's close, log shadow-vs-live SMA. Never raises.

        Returns the shadow measurement dict (for tests) or None. The return value
        carries ONLY measurements — never a signal/decision — so it cannot affect
        trading even if a caller inspected it.
        """
        if not self.enabled:
            return None
        try:
            close = bar.get("close") if isinstance(bar, dict) else None
            if close is None:
                return None
            self._closes.append(float(close))
            backfill_sma100 = self._mean(self._sma_period)
            backfill_ma50 = self._mean(self._ma_period)
            live_sma100 = (live_decision or {}).get("sma100")
            diff = (
                backfill_sma100 - live_sma100
                if backfill_sma100 is not None and isinstance(live_sma100, int | float)
                else None
            )
            if bar_count is None or bar_count <= _LOG_UNTIL_BAR:
                LOGGER.info(
                    "[WARMUP-SHADOW] bar=%s live_sma100=%s backfill_sma100=%s diff=%s "
                    "backfill_ma50=%s",
                    bar_count,
                    _fmt(live_sma100),
                    _fmt(backfill_sma100),
                    _fmt(diff),
                    _fmt(backfill_ma50),
                )
            return {
                "bar_count": bar_count,
                "live_sma100": live_sma100,
                "backfill_sma100": backfill_sma100,
                "backfill_ma50": backfill_ma50,
                "diff": diff,
            }
        except Exception as exc:  # noqa: BLE001 — observe-only, never break the bar loop
            LOGGER.warning("[WARMUP-SHADOW] observe failed — %s (skipped)", exc)
            return None

    def _mean(self, n: int) -> float | None:
        if len(self._closes) < n:
            return None
        window = list(self._closes)[-n:]
        return sum(window) / n


def _fmt(value: Any) -> str:
    return f"{value:.2f}" if isinstance(value, int | float) else "None"
