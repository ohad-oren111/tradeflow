"""SeanBot V2 signal detection — MA100 bounce with candle confirmation + ADX filter.

Strategy LONG: MA50 > MA100, ADX >= 20, price touches MA100, bullish candle close.
Entry at candle close. SL = 75 points from entry. TP = 150 points from entry.

SHORT branch deferred to a post-paper-graduation PR.

The schema column ``strategy`` keeps the stable identifier ``"sma100_bounce"``;
the algorithm name is documented here per §0.5.133. Renaming the schema
identifier would invalidate prior lifecycle rows.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from config.risk_params import RISK, RiskParams
from src.indicators import add_all_indicators

LOGGER = logging.getLogger(__name__)

STRATEGY_NAME = "sma100_bounce"

_ET = ZoneInfo("America/New_York")
# Regular trading hours boundaries for MNQ (mirror config.risk_params).
_RTH_OPEN_HOUR = 9
_RTH_OPEN_MIN = 30
_RTH_CLOSE_HOUR = 16
_RTH_CLOSE_MIN = 0
# Rolling buffer big enough for MA100 + ADX(14) warmup with headroom.
_BAR_BUFFER_MAX = 150


@dataclass
class Signal:
    instrument: str
    direction: str  # "LONG" (SHORT deferred)
    entry_price: float
    stop_price: float
    target_price: float
    ma_fast_value: float
    ma_slow_value: float
    ma_gap: float
    adx_value: float = 0.0
    timestamp: datetime | None = None


def detect_signal(
    df: pd.DataFrame,
    instrument: str,
    buffer_pts: float | None = None,
    min_gap_pts: float | None = None,
    min_adx: float | None = None,
    *,
    params: RiskParams | None = None,
) -> Signal | None:
    """Inspect the latest fully-formed bar in ``df`` and return a Signal or None.

    Caller is expected to pre-populate ``ma_fast``, ``ma_slow``, ``adx`` columns
    via :func:`src.indicators.add_all_indicators`. ``buffer_pts``, ``min_gap_pts``,
    ``min_adx`` override the corresponding risk_params values when provided —
    used by tests to vary thresholds without monkeypatching the global RISK.
    """
    if len(df) < 2:
        return None

    rp = params if params is not None else RISK
    buf = buffer_pts if buffer_pts is not None else rp.ma_touch_buffer_pts
    gap_min = min_gap_pts if min_gap_pts is not None else rp.ma_min_gap_pts
    adx_threshold = min_adx if min_adx is not None else rp.adx_min_threshold
    sl_pts = rp.stop_loss_pts
    tp_pts = rp.take_profit_pts

    bar = df.iloc[-1]

    if pd.isna(bar["ma_fast"]) or pd.isna(bar["ma_slow"]):
        return None

    ma_fast = float(bar["ma_fast"])
    ma_slow = float(bar["ma_slow"])
    ma_gap = abs(ma_fast - ma_slow)

    if ma_gap < gap_min:
        return None

    adx_val = bar.get("adx", 0.0)
    if pd.isna(adx_val):
        adx_val = 0.0
    adx_val = float(adx_val)
    if adx_threshold > 0 and adx_val < adx_threshold:
        return None

    o = float(bar["open"])
    h = float(bar["high"])  # noqa: F841 — kept to mirror SeanBot port for readability
    l_ = float(bar["low"])
    c = float(bar["close"])

    # LONG: MA50 > MA100 (uptrend), price touches MA100, bullish candle close.
    if ma_fast > ma_slow and l_ <= ma_slow + buf and c > o:
        LOGGER.info(
            "[STRAT] %s: long_signal — entry=%.2f stop=%.2f target=%.2f "
            "ma_fast=%.2f ma_slow=%.2f adx=%.2f",
            instrument,
            c,
            c - sl_pts,
            c + tp_pts,
            ma_fast,
            ma_slow,
            adx_val,
        )
        return Signal(
            instrument=instrument,
            direction="LONG",
            entry_price=c,
            stop_price=c - sl_pts,
            target_price=c + tp_pts,
            ma_fast_value=ma_fast,
            ma_slow_value=ma_slow,
            ma_gap=ma_gap,
            adx_value=adx_val,
            timestamp=datetime.now(UTC),
        )

    # SHORT branch deferred — do not implement in this PR.
    return None


def _normalise_bar_time(bar: dict) -> datetime:
    """Coerce ``bar['time']`` to a timezone-aware UTC datetime.

    Accepts a pandas Timestamp, a stdlib datetime, or an ISO 8601 string. The
    bar feed should always supply one of these; we default to ``datetime.now(UTC)``
    only if the field is missing (live realtime bars sometimes omit time on the
    initial snapshot).
    """
    raw = bar.get("time")
    if raw is None:
        return datetime.now(UTC)
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, pd.Timestamp):
        dt = raw.to_pydatetime()
    else:
        dt = datetime.fromisoformat(str(raw))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _in_session_edge_window(ts_utc: datetime, edge_minutes: int) -> bool:
    """True if ``ts_utc`` falls within ``edge_minutes`` of the RTH open or close.

    Uses America/New_York wall-clock so DST is handled transparently. Trades
    outside RTH (e.g. overnight bars) are also gated as "edge" — the strategy
    only fires during RTH.
    """
    et = ts_utc.astimezone(_ET)
    weekday = et.weekday()
    if weekday >= 5:
        return True  # weekend
    open_dt = et.replace(hour=_RTH_OPEN_HOUR, minute=_RTH_OPEN_MIN, second=0, microsecond=0)
    close_dt = et.replace(hour=_RTH_CLOSE_HOUR, minute=_RTH_CLOSE_MIN, second=0, microsecond=0)
    if et < open_dt or et >= close_dt:
        return True
    minutes_from_open = (et - open_dt).total_seconds() / 60.0
    minutes_to_close = (close_dt - et).total_seconds() / 60.0
    if minutes_from_open < edge_minutes:
        return True
    return minutes_to_close <= edge_minutes


class Sma100BounceStrategy:
    """Stateful wrapper around :func:`detect_signal`.

    Owns the rolling bar buffer (DataFrame populated with indicators), tracks
    session-edge no-trade windows and post-trade cooldown bars. Methods are
    deliberately small so the orchestrator can drive them per bar.
    """

    def __init__(
        self,
        instrument: str,
        *,
        params: RiskParams | None = None,
        buffer_size: int = _BAR_BUFFER_MAX,
    ) -> None:
        self._instrument = instrument
        self._params: RiskParams = params if params is not None else RISK
        self._bars: deque[dict] = deque(maxlen=buffer_size)
        self._cooldown_bars_remaining = 0

    @property
    def instrument(self) -> str:
        return self._instrument

    @property
    def bar_count(self) -> int:
        return len(self._bars)

    def on_new_bar(self, bar: dict) -> Signal | None:
        """Append a bar, recompute indicators, apply gates, return a Signal or None.

        ``bar`` must contain at least: ``time``, ``open``, ``high``, ``low``,
        ``close``. ``volume`` is optional but propagated.
        """
        self._bars.append(dict(bar))

        if self._cooldown_bars_remaining > 0:
            self._cooldown_bars_remaining -= 1
            LOGGER.debug(
                "[STRAT] %s: cooldown_bar — remaining=%s",
                self._instrument,
                self._cooldown_bars_remaining,
            )
            return None

        ts_utc = _normalise_bar_time(bar)
        if _in_session_edge_window(ts_utc, self._params.session_edge_no_trade_minutes):
            LOGGER.debug("[STRAT] %s: session_edge — skip bar at %s", self._instrument, ts_utc)
            return None

        if len(self._bars) < 2:
            return None

        df = pd.DataFrame(list(self._bars))
        df = add_all_indicators(df)
        return detect_signal(df, self._instrument, params=self._params)

    def mark_trade_closed(self) -> None:
        """Start the cooldown_bars timer; new signals suppressed until elapsed."""
        self._cooldown_bars_remaining = self._params.cooldown_bars
        LOGGER.info(
            "[STRAT] %s: cooldown_armed — bars=%s",
            self._instrument,
            self._cooldown_bars_remaining,
        )
