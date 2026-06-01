"""Live-only shadow SMA + warmup-backfill helpers (warmup-enable PR).

After warmup-enable the STRATEGY trades on a backfilled SMA — its real bar buffer
is seeded from historical bars at boot so SMA100/MA50 are warm immediately (no
~100-min live-warmup dead zone). This module:

  * keeps an independent LIVE-ONLY SMA — accumulating from live bars ONLY, never
    seeded — and logs it against the strategy's backfilled SMA each bar, so the
    operator can watch them converge (or catch a bad backfill);
  * holds the pure helpers that build + sanity-check the historical seed
    (``hist_bars_to_dicts`` / ``validate_seed``) so a junk SMA can never gate a
    trade — the caller rejects and falls back to live warmup.

Observe-only and fire-and-forget: nothing here gates a trade and nothing raises
into boot or the bar loop.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger(__name__)

# Match the strategy: ma_slow = sma_100, ma_fast = sma_50 (src/indicators.py).
_SMA_PERIOD = 100
_MA_PERIOD = 50
_BUFFER_MAX = 150
# Log the live-only-vs-backfill diff until the live-only SMA has had a chance to
# warm + a few bars past, then quiet down (avoids an endless 1/min log).
_LOG_UNTIL_LIVE_BAR = _SMA_PERIOD + 5
# A backfilled SMA100 must sit within this many points of the latest close, else
# the backfill is junk (notional contamination / wrong contract / stale) — reject
# and fall back to live warmup. MNQ trades ~30k; a 100-bar SMA-vs-price gap is
# normally tens of points, so 500 is a generous-but-safe ceiling.
_MAX_SANE_SMA_DISTANCE_PTS = 500.0


class WarmupShadow:
    """LIVE-ONLY SMA tracker (never seeded), logged against the strategy's
    backfilled SMA. No gate; never raises."""

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

    @property
    def live_bars(self) -> int:
        return len(self._closes)

    def observe(self, bar: dict | None, strategy_decision: dict | None) -> dict | None:
        """Append the live bar's close to the LIVE-ONLY buffer and log the
        live-only SMA against the strategy's backfilled SMA. Never raises.

        Returns the measurement dict (for tests) or None — measurements only,
        never a signal/decision, so it cannot affect trading.
        """
        if not self.enabled:
            return None
        try:
            close = bar.get("close") if isinstance(bar, dict) else None
            if close is None:
                return None
            self._closes.append(float(close))
            live_sma100 = self._mean(self._sma_period)  # LIVE-ONLY (not seeded)
            raw_backfill = (strategy_decision or {}).get("sma100")
            backfill_sma100 = float(raw_backfill) if isinstance(raw_backfill, int | float) else None
            diff = (
                backfill_sma100 - live_sma100
                if live_sma100 is not None and backfill_sma100 is not None
                else None
            )
            live_bar = len(self._closes)
            if live_bar <= _LOG_UNTIL_LIVE_BAR:
                LOGGER.info(
                    "[WARMUP-SHADOW] bar=%s live_sma100=%s backfill_sma100=%s diff=%s",
                    live_bar,
                    _fmt(live_sma100),
                    _fmt(backfill_sma100),
                    _fmt(diff),
                )
            return {
                "live_bar": live_bar,
                "live_sma100": live_sma100,
                "backfill_sma100": backfill_sma100,
                "diff": diff,
            }
        except Exception as exc:  # noqa: BLE001 — observe-only, never break the bar loop
            LOGGER.warning("[WARMUP-SHADOW] observe failed — %s (skipped)", exc)
            return None

    def _mean(self, n: int) -> float | None:
        if len(self._closes) < n:
            return None
        return sum(list(self._closes)[-n:]) / n


def hist_bars_to_dicts(bars: list[Any]) -> list[dict]:
    """Convert ib_async ``BarData`` (``.date/.open/.high/.low/.close/.volume``) to
    the strategy's bar-dict shape. Skips bars with no usable close."""
    out: list[dict] = []
    for b in bars:
        close = getattr(b, "close", None)
        if close is None:
            continue
        try:
            c = float(close)
        except (TypeError, ValueError):
            continue
        out.append(
            {
                "time": _coerce_bar_time(getattr(b, "date", None)),
                "open": _f(getattr(b, "open", None), c),
                "high": _f(getattr(b, "high", None), c),
                "low": _f(getattr(b, "low", None), c),
                "close": c,
                "volume": _f(getattr(b, "volume", None), 0.0),
            }
        )
    return out


def validate_seed(
    seed: list[dict], *, needed: int, max_distance: float = _MAX_SANE_SMA_DISTANCE_PTS
) -> tuple[bool, str, float | None]:
    """Sanity-gate a historical seed. Returns ``(ok, reason, sma100)``.

    Rejects (so the caller falls back to live warmup, never trading on junk):
      * fewer than ``needed`` bars (can't form SMA{needed});
      * a NaN SMA;
      * an SMA more than ``max_distance`` pts from the latest close.
    """
    if len(seed) < needed:
        return False, f"only {len(seed)} bars (<{needed} for SMA{needed})", None
    closes = [float(b["close"]) for b in seed[-needed:]]
    sma = sum(closes) / needed
    if math.isnan(sma):
        return False, "SMA is NaN", None
    last = float(seed[-1]["close"])
    if abs(sma - last) > max_distance:
        return False, f"SMA {sma:.1f} is >{max_distance:.0f}pt from last price {last:.1f}", sma
    return True, "ok", sma


def _coerce_bar_time(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, int | float):
        try:
            return datetime.fromtimestamp(float(raw), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(str(raw))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _f(value: Any, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _fmt(value: Any) -> str:
    return f"{value:.2f}" if isinstance(value, int | float) else "None"
