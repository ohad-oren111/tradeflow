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
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from config.risk_params import RISK, RiskParams
from src.indicators import add_all_indicators

LOGGER = logging.getLogger(__name__)

STRATEGY_NAME = "sma100_bounce"

_ET = ZoneInfo("America/New_York")
# Rolling buffer big enough for MA100 + ADX(14) warmup with headroom.
_BAR_BUFFER_MAX = 150


def _parse_hhmm(s: str) -> time:
    """Parse an "HH:MM" wall-clock string into a stdlib ``time``."""
    h, m = s.split(":")
    return time(int(h), int(m))


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


def _in_session_edge_window(
    ts_utc: datetime,
    edge_minutes: int,
    *,
    daily_break: tuple[time, time] = (time(17, 0), time(18, 0)),
    gateway_restart: tuple[time, time] = (time(23, 45), time(0, 15)),
    weekend_cutoff: tuple[int, time] = (4, time(16, 30)),
    sunday_open: time = time(18, 0),
) -> bool:
    """True if ``ts_utc`` falls inside any no-trade window of the 24/5 MNQ session.

    America/New_York wall-clock is used for all comparisons so DST is handled
    transparently. The no-trade windows are: Saturday, Sunday before the weekly
    open, the first ``edge_minutes`` after the Sunday open, an ``edge_minutes``
    pad on each side of the daily CME maintenance break ``daily_break``, an
    ``edge_minutes`` pad before the Friday ``weekend_cutoff`` and everything
    after it, and the ``gateway_restart`` band (may span midnight).
    """
    et = ts_utc.astimezone(_ET)
    weekday = et.weekday()

    # Saturday: full day no-trade.
    if weekday == 5:
        return True

    # Sunday before the weekly open: no-trade.
    if weekday == 6 and et.time() < sunday_open:
        return True

    # Sunday inside the post-open edge buffer: no-trade.
    if weekday == 6:
        open_dt = et.replace(
            hour=sunday_open.hour, minute=sunday_open.minute, second=0, microsecond=0
        )
        if (et - open_dt).total_seconds() / 60.0 < edge_minutes:
            return True

    # Friday weekend cutoff: pre-cutoff edge AND everything after the cutoff.
    cutoff_weekday, cutoff_time = weekend_cutoff
    if weekday == cutoff_weekday:
        cutoff_dt = et.replace(
            hour=cutoff_time.hour, minute=cutoff_time.minute, second=0, microsecond=0
        )
        if et >= cutoff_dt:
            return True
        if (cutoff_dt - et).total_seconds() / 60.0 <= edge_minutes:
            return True

    # Daily CME maintenance break (Mon–Fri): pad ``edge_minutes`` on each side.
    if weekday in (0, 1, 2, 3, 4):
        break_start, break_end = daily_break
        anchor = datetime.combine(et.date(), break_start)
        pre_break_start = (anchor - timedelta(minutes=edge_minutes)).time()
        post_break_end = (
            datetime.combine(et.date(), break_end) + timedelta(minutes=edge_minutes)
        ).time()
        if pre_break_start <= et.time() < post_break_end:
            return True

    # IB Gateway restart band (may span midnight).
    gw_start, gw_end = gateway_restart
    et_time = et.time()
    if gw_start <= gw_end:
        if gw_start <= et_time < gw_end:
            return True
    else:
        if et_time >= gw_start or et_time < gw_end:
            return True

    return False


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
        # Parse "HH:MM" strings from risk_params once; the session-edge helper
        # gets ready-to-use ``time`` objects on every bar.
        p = self._params
        self._daily_break: tuple[time, time] = (
            _parse_hhmm(p.daily_break_start_et),
            _parse_hhmm(p.daily_break_end_et),
        )
        self._gateway_restart: tuple[time, time] = (
            _parse_hhmm(p.gateway_restart_start_et),
            _parse_hhmm(p.gateway_restart_end_et),
        )
        self._weekend_cutoff: tuple[int, time] = (
            p.weekend_flat_cutoff_weekday,
            time(p.weekend_flat_cutoff_hour_et, p.weekend_flat_cutoff_minute_et),
        )
        self._sunday_open: time = _parse_hhmm(p.sunday_open_et)

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
        if _in_session_edge_window(
            ts_utc,
            self._params.session_edge_no_trade_minutes,
            daily_break=self._daily_break,
            gateway_restart=self._gateway_restart,
            weekend_cutoff=self._weekend_cutoff,
            sunday_open=self._sunday_open,
        ):
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
