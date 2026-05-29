"""SeanBot V3-aligned signal detection — MA100 pullback with candle confirmation.

Strategy LONG: ``MA100 > MA50`` (recent pullback inside a larger uptrend per
SeanBot ``strategy/ma_bounce.py:122``), price touches MA100 inside a windowed
band ``low ∈ [MA100-15, MA100+5]``, bullish candle close
``close >= open - ma_bullish_tolerance_pts`` (W-S14.2 Track 2: tolerance widens
the strict ``close > open`` to admit flat/near-doji touch bars), and
``abs(MA100-MA50) >= MA_MIN_GAP`` (SeanBot V3 default 0.5). Entry at candle
close. SL = 75 points from entry. TP = 150 points from entry. No ADX filter
and no regime gate (the operator deferred SeanBot's C1 regime gate
deliberately — see PR #33 description §E.1 for the risk callout).

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


@dataclass
class GateEval:
    """Full per-bar gate breakdown — the single source of truth for both the
    entry decision and the granular [STRAT] eval decision label (PR Track 3).

    ``detect_signal`` delegates to :func:`evaluate_gates` and returns only
    ``.signal``, so its public behavior is unchanged. ``on_new_bar`` reads the
    booleans to split the old collapsed filter-or-regime label into
    ``noop_regime`` / ``noop_warmup`` (indicators not ready) / ``noop_filter``.

    Filter booleans are ``None`` when the bar short-circuited before they were
    evaluated (regime blocked, or indicators not yet warm).
    """

    signal: Signal | None
    regime_ok: bool
    indicators_ready: bool
    ma_order_ok: bool | None = None
    touch_ok: bool | None = None
    bullish_ok: bool | None = None
    gap_ok: bool | None = None
    ma_fast: float = float("nan")
    ma_slow: float = float("nan")
    ma_gap: float = float("nan")


_TOUCH_LOWER_BAND_PTS = 15.0  # SeanBot ma_bounce.py:123 hardcodes -15 lower bound


def _regime_ok(df: pd.DataFrame, params: RiskParams) -> bool:
    """C1 regime gate (SeanBot ma_bounce.py:55-91, shipped 2026-05-18).

    Block LONG entries when current price is at or below the 30-min EMA200.
    Fail-open on warmup (<202 30-min bars), missing timestamps, or exception —
    mirrors SeanBot's defensive semantics so a misconfigured frame never
    silently disables every other strategy gate.
    """
    if not params.regime_gate_enabled:
        return True
    try:
        close = df["close"]
        if not isinstance(close.index, pd.DatetimeIndex):
            if "time" in df.columns:
                # TradeFlow uses 'time' (UTC datetime); SeanBot uses 'date'.
                close = df.set_index("time")["close"]
            else:
                return True  # no timestamps — fail open
        bars_30m = close.resample("30min").last().dropna()
        if len(bars_30m) < 202:
            return True  # warmup — fail open
        ema = bars_30m.ewm(span=200, adjust=False).mean()
        ema200_level = float(ema.iloc[-1])
        last_price = float(close.iloc[-1])
        if last_price <= ema200_level:
            LOGGER.info(
                "[STRAT] regime gate BLOCKED entry: price=%.2f <= 30m EMA200=%.2f",
                last_price,
                ema200_level,
            )
            return False
        return True
    except Exception as e:
        LOGGER.warning("[STRAT] regime gate error (fail-open): %s", e)
        return True


def evaluate_gates(
    df: pd.DataFrame,
    instrument: str,
    buffer_pts: float | None = None,
    min_gap_pts: float | None = None,
    *,
    params: RiskParams | None = None,
) -> GateEval:
    """Run every entry gate on the latest bar and return the full breakdown.

    This holds the logic that used to live in :func:`detect_signal`; the gate
    order, thresholds, fail-open semantics, DECISION_TRACE debug line, and the
    ``long_signal`` INFO line are all unchanged — only the return type widened
    from ``Signal | None`` to :class:`GateEval` so callers can see *why* a bar
    did or didn't fire. :func:`detect_signal` delegates here and returns
    ``.signal`` so its public behavior is byte-for-byte identical.

    ``buffer_pts`` / ``min_gap_pts`` override the corresponding risk_params
    values when provided (tests vary thresholds without monkeypatching RISK).
    """
    if len(df) < 2:
        return GateEval(signal=None, regime_ok=True, indicators_ready=False)

    rp = params if params is not None else RISK

    # SeanBot C1 regime gate — checked first (mirrors ma_bounce.py:101) so the
    # blocking decision precedes any indicator math on a downtrend bar.
    if not _regime_ok(df, rp):
        return GateEval(signal=None, regime_ok=False, indicators_ready=False)

    buf = buffer_pts if buffer_pts is not None else rp.ma_touch_buffer_pts
    gap_min = min_gap_pts if min_gap_pts is not None else rp.ma_min_gap_pts
    sl_pts = rp.stop_loss_pts
    tp_pts = rp.take_profit_pts

    bar = df.iloc[-1]

    if pd.isna(bar["ma_fast"]) or pd.isna(bar["ma_slow"]):
        # Regime passed (or fail-open) but the SMA buffer is not warm yet.
        return GateEval(signal=None, regime_ok=True, indicators_ready=False)

    ma_fast = float(bar["ma_fast"])
    ma_slow = float(bar["ma_slow"])
    ma_gap = abs(ma_slow - ma_fast)

    o = float(bar["open"])
    l_ = float(bar["low"])
    c = float(bar["close"])

    touch_lower = ma_slow - _TOUCH_LOWER_BAND_PTS
    touch_upper = ma_slow + buf

    # SeanBot ma_bounce.py:122-125 reference. ``ma_order_ok`` is INVERTED vs
    # the pre-PR-33 port (was ``ma_fast > ma_slow``) — SeanBot treats
    # ``MA100 > MA50`` as a pullback into MA support inside a larger uptrend.
    ma_order_ok = ma_slow > ma_fast
    touch_ok = (l_ >= touch_lower) and (l_ <= touch_upper)
    # W-S14.2 Track 2 (D-1 approved): tolerate flat/near-doji touch candles. The
    # tolerance is a named risk_param (0.0 == prior strict close>open semantics).
    bull_tol = rp.ma_bullish_tolerance_pts
    bullish_ok = c >= o - bull_tol
    gap_ok = ma_gap >= gap_min

    if not (ma_order_ok and touch_ok and bullish_ok and gap_ok):
        LOGGER.debug(
            "[STRAT] DECISION_TRACE: LONG blocked | "
            "ma_order(MA100>MA50)=%s[%.1fvs%.1f] "
            "touch=%s[low=%.1f must be >=%.1f and <=%.1f] "
            "bullish=%s[c=%.1fvs_o=%.1f] "
            "gap=%s[%.1fvs%s]",
            "OK" if ma_order_ok else "FAIL",
            ma_slow,
            ma_fast,
            "OK" if touch_ok else "FAIL",
            l_,
            touch_lower,
            touch_upper,
            "OK" if bullish_ok else "FAIL",
            c,
            o,
            "OK" if gap_ok else "FAIL",
            ma_gap,
            gap_min,
        )
        return GateEval(
            signal=None,
            regime_ok=True,
            indicators_ready=True,
            ma_order_ok=ma_order_ok,
            touch_ok=touch_ok,
            bullish_ok=bullish_ok,
            gap_ok=gap_ok,
            ma_fast=ma_fast,
            ma_slow=ma_slow,
            ma_gap=ma_gap,
        )

    LOGGER.info(
        "[STRAT] %s: long_signal — entry=%.2f stop=%.2f target=%.2f "
        "ma_fast=%.2f ma_slow=%.2f gap=%.2f",
        instrument,
        c,
        c - sl_pts,
        c + tp_pts,
        ma_fast,
        ma_slow,
        ma_gap,
    )
    signal = Signal(
        instrument=instrument,
        direction="LONG",
        entry_price=c,
        stop_price=c - sl_pts,
        target_price=c + tp_pts,
        ma_fast_value=ma_fast,
        ma_slow_value=ma_slow,
        ma_gap=ma_gap,
        adx_value=0.0,
        timestamp=datetime.now(UTC),
    )
    return GateEval(
        signal=signal,
        regime_ok=True,
        indicators_ready=True,
        ma_order_ok=True,
        touch_ok=True,
        bullish_ok=True,
        gap_ok=True,
        ma_fast=ma_fast,
        ma_slow=ma_slow,
        ma_gap=ma_gap,
    )


def detect_signal(
    df: pd.DataFrame,
    instrument: str,
    buffer_pts: float | None = None,
    min_gap_pts: float | None = None,
    *,
    params: RiskParams | None = None,
) -> Signal | None:
    """Inspect the latest fully-formed bar in ``df`` and return a Signal or None.

    Thin wrapper over :func:`evaluate_gates` — public behavior unchanged. Pass
    ``buffer_pts`` / ``min_gap_pts`` to override risk_params thresholds.
    """
    return evaluate_gates(df, instrument, buffer_pts, min_gap_pts, params=params).signal


def _first_failed_filter(ge: GateEval) -> str:
    """Name the first filter gate that failed, in the order they're checked.

    Used only for the ``noop_filter failed=<gate>`` decision label. Assumes the
    caller already established regime_ok and indicators_ready.
    """
    if ge.touch_ok is False:
        return "touch"
    if ge.ma_order_ok is False:
        return "ma_order"
    if ge.bullish_ok is False:
        return "bullish"
    if ge.gap_ok is False:
        return "gap"
    return "unknown"


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
        # Track 3 — structured record of the most recent on_new_bar decision,
        # read by the orchestrator's decision journal after each bar.
        self._last_decision: dict | None = None

    @property
    def instrument(self) -> str:
        return self._instrument

    @property
    def bar_count(self) -> int:
        return len(self._bars)

    @property
    def last_decision(self) -> dict | None:
        """The structured decision record from the most recent ``on_new_bar``."""
        return self._last_decision

    def on_new_bar(self, bar: dict) -> Signal | None:
        """Append a bar, recompute indicators, apply gates, return a Signal or None.

        ``bar`` must contain at least: ``time``, ``open``, ``high``, ``low``,
        ``close``. ``volume`` is optional but propagated.
        """
        self._bars.append(dict(bar))
        ts_utc = _normalise_bar_time(bar)

        # PR-D3a — emit one [STRAT] eval log per bar so we have per-bar truth
        # on what the strategy decided. Fields stay in fixed order so downstream
        # parsing is trivial. ma_*/gap default to NaN for bars that early-exit
        # before indicators are computed (cooldown, session edge, warmup).
        ma_fast = float("nan")
        ma_slow = float("nan")
        ma_gap = float("nan")
        cooldown_active = self._cooldown_bars_remaining > 0
        signal: Signal | None = None
        ge: GateEval | None = None
        failed: str | None = None

        if cooldown_active:
            self._cooldown_bars_remaining -= 1
            LOGGER.debug(
                "[STRAT] %s: cooldown_bar — remaining=%s",
                self._instrument,
                self._cooldown_bars_remaining,
            )
            decision = "noop_cooldown"
        elif _in_session_edge_window(
            ts_utc,
            self._params.session_edge_no_trade_minutes,
            daily_break=self._daily_break,
            gateway_restart=self._gateway_restart,
            weekend_cutoff=self._weekend_cutoff,
            sunday_open=self._sunday_open,
        ):
            LOGGER.debug("[STRAT] %s: session_edge — skip bar at %s", self._instrument, ts_utc)
            decision = "noop_session_edge"
        elif len(self._bars) < 2:
            decision = "noop_warmup"
        else:
            df = pd.DataFrame(list(self._bars))
            df = add_all_indicators(df)
            last_row = df.iloc[-1]
            if not pd.isna(last_row["ma_fast"]):
                ma_fast = float(last_row["ma_fast"])
            if not pd.isna(last_row["ma_slow"]):
                ma_slow = float(last_row["ma_slow"])
            if not (pd.isna(ma_fast) or pd.isna(ma_slow)):
                ma_gap = abs(ma_slow - ma_fast)
            ge = evaluate_gates(df, self._instrument, params=self._params)
            signal = ge.signal
            # Split the old collapsed filter-or-regime label into the actual
            # blocking reason (Track 3). Entry/noop boundary is unchanged — only
            # the label is finer.
            if signal is not None:
                decision = "long_signal"
            elif not ge.regime_ok:
                decision = "noop_regime"
            elif not ge.indicators_ready:
                decision = "noop_warmup"  # SMA buffer not yet warm
            else:
                failed = _first_failed_filter(ge)
                decision = "noop_filter"

        # Enriched eval log — self-explaining: carries every gate value (when a
        # full gate eval ran) plus the failing filter, so no replay is needed to
        # know why a bar didn't fire.
        gate_str = ""
        if ge is not None:
            failed_str = f" failed={failed}" if failed is not None else ""
            gate_str = (
                f"{failed_str} regime_ok={ge.regime_ok} touch_ok={ge.touch_ok} "
                f"ma_order_ok={ge.ma_order_ok} bullish_ok={ge.bullish_ok} gap_ok={ge.gap_ok}"
            )
        LOGGER.info(
            "[STRAT] %s: eval ts=%s close=%.2f ma_fast=%.2f ma_slow=%.2f gap=%.2f "
            "cooldown=%s decision=%s%s",
            self._instrument,
            ts_utc.isoformat(),
            float(bar.get("close", float("nan"))),
            ma_fast,
            ma_slow,
            ma_gap,
            cooldown_active,
            decision,
            gate_str,
        )

        # Track 3 — expose the decision as a structured record for the
        # orchestrator's decision journal + replay tool.
        self._last_decision = {
            "ts": ts_utc.isoformat(),
            "decision": decision,
            "failed": failed,
            "close": float(bar.get("close", float("nan"))),
            "sma100": ma_slow if not pd.isna(ma_slow) else None,
            "regime_ok": ge.regime_ok if ge is not None else None,
            "touch_ok": ge.touch_ok if ge is not None else None,
            "ma_order_ok": ge.ma_order_ok if ge is not None else None,
            "bullish_ok": ge.bullish_ok if ge is not None else None,
            "gap_ok": ge.gap_ok if ge is not None else None,
        }
        return signal

    def mark_trade_closed(self) -> None:
        """Start the cooldown_bars timer; new signals suppressed until elapsed."""
        self._cooldown_bars_remaining = self._params.cooldown_bars
        LOGGER.info(
            "[STRAT] %s: cooldown_armed — bars=%s",
            self._instrument,
            self._cooldown_bars_remaining,
        )
