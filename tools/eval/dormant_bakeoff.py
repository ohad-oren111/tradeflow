"""Phase-15 (research, OFFLINE) — DORMANT-WINDOW STRATEGY BAKE-OFF.

Two external research reports converged on the same idea — "trade MNQ below the
30-min EMA200 while the gated long bot is DORMANT" — but DIVERGED on direction.
This module backtests BOTH verbatim rule sets over the full 27-month 1-min history
and lets the data pick. It is a VALIDATION pass, not an optimization pass: every
parameter is fixed by the briefs; nothing is tuned (overfit guard).

CANDIDATE 1 — "SHORT-CONTINUATION" (sell the pullback into a falling-EMA downtrend)
  regime  : 30m close < 30m-EMA200 AND EMA200 falling (now < 3 buckets ago)
  setup   : within the prior 20 1-min bars, >=1 close <= SMA100 - 0.75*ATR14(1m)
  pullback: first bar trading into [SMA100-0.10*ATR, SMA100+0.25*ATR] closing < SMA100
  confirm : within the next 3 bars, a close < pullback-bar low AND < SMA100
            -> SHORT the next bar's open. One attempt per sequence.
  stop    : max(pullback high, highest high of confirm bar + 2 prior) + 0.25*ATR
  reject  : R < 0.5*ATR or R > 1.5*ATR  (R = stop - entry, the per-ct risk)
  exit    : target entry-2R; at +1R move stop to entry-1tick and trail at
            lowest_low_since_entry + 1.0*ATR (down-only); also exit on 30m close >
            EMA200, age > 240 bars, or session flat. Max 1 position, 10-bar cooldown.

CANDIDATE 2 — "CAPITULATION MEAN-REVERSION LONG" (buy the washout, fade it back)
  regime  : 1m close < 30m-EMA200. RTH only, 09:45-15:15 ET (signal window).
  signal  : on CLOSED synthetic 15-min bars — close < lower Bollinger(20, 2.5sigma)
            AND RSI(2) < 5 -> BUY the next 1-min bar's open.
  bracket : stop = fill - 1.5*ATR14(15m); target = fill + 2.0*ATR14(15m).
  time    : flatten 15:55 ET. No overnight. Max 1 position.

COST MODEL (both, per SIDE): $0.31 commission + slippage of 1 tick base / 2 ticks
when the FILL bar's 1-min range > 2*ATR14(1m) (capitulation fills worse). Headline
metrics use this model; a 0/1/2-tick sensitivity table is printed alongside, plus a
$0.62/side variant (the project's verified IBKR rate — see config.instruments).

FIDELITY / REUSE (no re-implementation of the gate):
  * The 30m-EMA200 regime uses the gate's EXACT resample/EMA: close.resample("30min")
    .last() + ewm(span=200, adjust=False), the literal in strategy._regime_ok and its
    faithful mirror below_trend_study.regime_at. We apply it over the full roll-handled
    series (globally converged); the live gate uses a trailing ~7000-bar window (lightly
    warmed ~234 buckets) — the two match to <0.01pt over this span (Session-27 gate-health
    probe: -0.001pt mean), so this is faithful. >=202 valid buckets required (warmup).
  * SMA100 + ATR14(1m) come from src.indicators.add_all_indicators (the SAME columns the
    live strategy reads). The 15-min Bollinger/RSI/ATR are candidate-specific and built
    here (no prod helper exists), with the formula visible at the call site.
  * The gated long reference book (for the daily-P&L correlation) is the above-30m-EMA200
    set replayed through the REAL TF exit — i.e. exactly what the live gated strategy is.

CAVEATS (carried into the report, never hidden):
  * MODELED fills — entry at the fill bar's open (cand2) / open (cand1); the protective
    stop fills AT the stop price intrabar; on a same-bar stop+target collision the STOP is
    taken first (pessimistic, §0.5.206). Measures LOGIC expectancy, not live plumbing.
  * NEW research entries — NO prod path trades below-trend; this proves nothing about prod.
  * NQ 1-min bars (point-identical to MNQ; $2 multiplier). Per-CONTRACT P&L reported.
  * 30m regime uses COMPLETED buckets (causal, no partial-bucket lookahead).
  * The forward edge in the LIVE regime is the one thing no backtest can prove (§0.5.220).

  python -m tools.eval.dormant_bakeoff [--limit N] [--out PATH] [--rebuild-gated]

OFFLINE research, READ-ONLY, drives NO prod path. No src/ changes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config.instruments import MNQ
from src.execution.trail_manager import round_to_tick
from src.indicators import add_atr
from tools.eval import data
from tools.eval.backtest import friday_force_flat
from tools.eval.below_trend_study import (
    REGIME_EMA_SPAN,
    REGIME_MIN_BUCKETS,
    cfg_params,
    load_tape,
    mask_above,
    replay_masked,
)
from tools.eval.metrics import Stats, compute_stats

_ET = ZoneInfo("America/New_York")
TICK = MNQ.tick_size  # 0.25
MULT = MNQ.multiplier  # 2.0

# Cost model (brief spec).
COMMISSION_PER_SIDE = 0.31  # brief literal (NOTE: project-verified IBKR = 0.62/side)
COMMISSION_PER_SIDE_VERIFIED = MNQ.commission_per_side_usd  # 0.62 — reported as a variant

# Head-to-head go/no-go bar (brief).
PF_BAR = 1.20
EXPECTANCY_BAR_USD = 4.0  # per contract

# Dormant-stretch depth bands (pts below the 30m EMA200 at entry).
DEPTH_BANDS: tuple[tuple[str, float, float], ...] = (
    ("shallow_0_25", 0.0, 25.0),
    ("mid_25_50", 25.0, 50.0),
    ("band_50_100", 50.0, 100.0),
    ("deep_100_plus", 100.0, float("inf")),
)

GATED_TAPE_CACHE = "/tmp/tf_below_trend_tape.pkl"


# --------------------------------------------------------------------------- #
# 30-min regime precompute (the gate's EXACT resample + EMA, completed buckets)
# --------------------------------------------------------------------------- #
@dataclass
class Regime30m:
    """Per-1-min-bar view of the completed-bucket 30m regime.

    Arrays are aligned to the 1-min frame (length == n bars):
      * ``ema``        — 30m-EMA200 level of the last COMPLETED 30m bucket (NaN in warmup)
      * ``bclose``     — close of the last completed 30m bucket
      * ``falling``    — EMA200[b] < EMA200[b-3] for that completed bucket (bool)
      * ``valid``      — last completed bucket index >= REGIME_MIN_BUCKETS-1 (gate armed)
    """

    ema: np.ndarray
    bclose: np.ndarray
    falling: np.ndarray
    valid: np.ndarray


def build_regime_30m(df: pd.DataFrame) -> Regime30m:
    """Resample the 1-min close to 30m (gate's exact rule), EMA200 it (gate's exact
    span/adjust), then broadcast each completed bucket onto the 1-min bars that fall
    AFTER it closes (causal — bar t sees only buckets that closed at/before t)."""
    s = df.set_index("time")["close"]
    bars_30m = s.resample("30min").last().dropna()
    ema30 = bars_30m.ewm(span=REGIME_EMA_SPAN, adjust=False).mean()
    bvals = bars_30m.to_numpy(dtype=float)
    evals = ema30.to_numpy(dtype=float)
    bidx = np.arange(len(bars_30m))
    falling_b = np.zeros(len(bars_30m), dtype=bool)
    falling_b[3:] = evals[3:] < evals[:-3]
    valid_b = bidx >= (REGIME_MIN_BUCKETS - 1)

    # bucket boundary timestamps (label = bucket START); a bucket is "completed" and
    # usable from its END (start + 30min) onward.
    bucket_start = bars_30m.index
    bucket_end = bucket_start + pd.Timedelta(minutes=30)
    end_ns = bucket_end.asi8
    bar_ns = pd.DatetimeIndex(df["time"]).asi8
    # for each 1-min bar, the last completed bucket = last bucket whose END <= bar time.
    pos = np.searchsorted(end_ns, bar_ns, side="right") - 1  # index into bars_30m, -1 if none

    n = len(df)
    ema = np.full(n, np.nan)
    bclose = np.full(n, np.nan)
    falling = np.zeros(n, dtype=bool)
    valid = np.zeros(n, dtype=bool)
    ok = pos >= 0
    ema[ok] = evals[pos[ok]]
    bclose[ok] = bvals[pos[ok]]
    falling[ok] = falling_b[pos[ok]]
    valid[ok] = valid_b[pos[ok]]
    return Regime30m(ema=ema, bclose=bclose, falling=falling, valid=valid)


# --------------------------------------------------------------------------- #
# Per-trade record + cost model
# --------------------------------------------------------------------------- #
@dataclass
class BakeTrade:
    cand: str
    side: str  # "short" | "long"
    entry_ts: datetime
    exit_ts: datetime
    entry_px: float
    exit_px: float
    gross_pts: float  # signed so >0 == profit (entry-exit for short, exit-entry for long)
    init_risk_pts: float  # the per-ct risk that defines 1R
    exit_reason: str
    depth_pts: float  # EMA200 - entry close (pts below trend at entry)
    entry_range: float  # fill-bar 1-min high-low (for conditional slippage)
    entry_atr1m: float
    exit_range: float
    exit_atr1m: float

    @property
    def year(self) -> int:
        return self.entry_ts.astimezone(_ET).year

    @property
    def realized_r(self) -> float:
        return self.gross_pts / self.init_risk_pts if self.init_risk_pts > 0 else 0.0

    def _slip_ticks(self, rng: float, atr1m: float, fixed: int | None) -> int:
        if fixed is not None:
            return fixed
        return 2 if (atr1m > 0 and rng > 2.0 * atr1m) else 1

    def net_usd(
        self, *, commission_per_side: float = COMMISSION_PER_SIDE, fixed_ticks: int | None = None
    ) -> float:
        """Per-CONTRACT net P&L: gross points, minus per-side slippage (ticks->pts) and
        commission on BOTH sides. ``fixed_ticks`` overrides the conditional model for the
        0/1/2-tick sensitivity columns."""
        ent = self._slip_ticks(self.entry_range, self.entry_atr1m, fixed_ticks)
        ext = self._slip_ticks(self.exit_range, self.exit_atr1m, fixed_ticks)
        slip_pts = (ent + ext) * TICK
        return (self.gross_pts - slip_pts) * MULT - 2.0 * commission_per_side


# A light metric-trade adaptor so metrics.compute_stats / segment_by_month work
# unchanged on a chosen cost model.
@dataclass
class _MT:
    net_usd: float
    entry_ts: datetime

    @property
    def is_win(self) -> bool:
        return self.net_usd > 0


def metric_trades(trades: list[BakeTrade], **cost) -> list[_MT]:
    return [_MT(t.net_usd(**cost), t.entry_ts) for t in trades]


def stats(trades: list[BakeTrade], **cost) -> Stats:
    return compute_stats(metric_trades(trades, **cost))


# --------------------------------------------------------------------------- #
# CANDIDATE 1 — SHORT-CONTINUATION
# --------------------------------------------------------------------------- #
def _session_flat(ts: datetime) -> bool:
    """'session flat' = the live weekend force-close predicate (Fri >=16:25 ET)."""
    return friday_force_flat(ts)


def run_candidate1(df: pd.DataFrame, reg: Regime30m) -> list[BakeTrade]:
    """Verbatim SHORT-CONTINUATION state machine over the 1-min frame."""
    t = pd.DatetimeIndex(df["time"])
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    sma100 = df["sma_100"].to_numpy(float)
    atr = df["atr"].to_numpy(float)
    rng = h - low
    ts_list = [x.to_pydatetime() for x in t]
    n = len(df)
    trades: list[BakeTrade] = []

    cooldown_until = -1  # bar index until which entries are suppressed
    i = 0
    while i < n:
        # need warm indicators + an armed, falling, below-trend regime to even look.
        if (
            i <= cooldown_until
            or np.isnan(sma100[i])
            or np.isnan(atr[i])
            or atr[i] <= 0
            or not reg.valid[i]
            or not (reg.falling[i] and reg.bclose[i] < reg.ema[i])
        ):
            i += 1
            continue

        # SETUP: a stretched-below close in the prior 20 1-min bars.
        lo20 = max(0, i - 20)
        stretched = np.any(c[lo20:i] <= sma100[lo20:i] - 0.75 * atr[lo20:i])
        if not stretched:
            i += 1
            continue

        # PULLBACK: first bar (from i) trading into the band AND closing < SMA100.
        band_lo = sma100[i] - 0.10 * atr[i]
        band_hi = sma100[i] + 0.25 * atr[i]
        traded_in = (h[i] >= band_lo) and (low[i] <= band_hi)
        if not (traded_in and c[i] < sma100[i]):
            i += 1
            continue

        pb = i  # pullback bar
        pb_low = low[pb]
        pb_high = h[pb]
        # CONFIRM: within the next 3 bars, a close < pullback low AND < SMA100.
        confirm = None
        for k in range(pb + 1, min(pb + 4, n)):
            if c[k] < pb_low and c[k] < sma100[k]:
                confirm = k
                break
        if confirm is None:
            i = pb + 1  # one attempt per sequence; advance past the pullback
            continue

        entry_i = confirm + 1  # SHORT the next bar's open
        if entry_i >= n:
            break
        a = atr[confirm]
        if np.isnan(a) or a <= 0:
            i = pb + 1
            continue
        entry_px = o[entry_i]
        # stop = max(pullback high, highest high of confirm bar + 2 prior) + 0.25*ATR
        hh = max(h[max(0, confirm - 2) : confirm + 1])
        stop = round_to_tick(max(pb_high, hh) + 0.25 * a)
        risk_pts = stop - entry_px
        if risk_pts < 0.5 * a or risk_pts > 1.5 * a:  # reject out-of-band risk -> sequence done
            i = pb + 1
            continue

        target = round_to_tick(entry_px - 2.0 * risk_pts)
        tr = _simulate_short(
            ts_list, o, h, low, c, rng, atr, reg, entry_i, entry_px, stop, target, risk_pts, a
        )
        trades.append(tr)
        # find the exit bar index to set cooldown (10 bars after exit).
        exit_i = tr._exit_i  # type: ignore[attr-defined]
        cooldown_until = exit_i + 10
        i = exit_i + 1
    return trades


def _simulate_short(
    ts_list, o, h, low, c, rng, atr, reg, entry_i, entry_px, stop, target, risk_pts, a
):
    """Run the SHORT exit bar-by-bar; returns a BakeTrade (with a private ``_exit_i``)."""
    n = len(c)
    cur_stop = stop
    lowest = low[entry_i]
    armed = False  # +1R lock+trail armed
    one_tick = TICK
    j = entry_i
    exit_px = None
    reason = None
    bars = 0
    while j < n:
        bars += 1
        # 1) intrabar protective stop (above): high >= stop -> stopped (pessimistic first)
        if h[j] >= cur_stop:
            exit_px, reason = cur_stop, ("trail_stop" if armed else "stop")
            break
        # 2) intrabar target (below): low <= target -> target
        if low[j] <= target:
            exit_px, reason = target, "target"
            break
        # 3) high-water (lowest) tracking for the trail
        lowest = min(lowest, low[j])
        # 4) arm +1R: price fell 1R in favor -> lock stop at entry-1tick, begin trailing
        if not armed and low[j] <= entry_px - risk_pts:
            armed = True
            cur_stop = min(cur_stop, round_to_tick(entry_px - one_tick))
        # 5) trail (down-only) once armed: stop = lowest_low + 1.0*ATR
        if armed:
            cand = round_to_tick(lowest + 1.0 * a)
            cur_stop = min(cur_stop, cand)
        # 6) regime flip up (30m close > EMA200) -> exit at close
        if reg.valid[j] and reg.bclose[j] > reg.ema[j]:
            exit_px, reason = c[j], "regime_flip"
            break
        # 7) age > 240 bars -> exit at close
        if bars > 240:
            exit_px, reason = c[j], "age"
            break
        # 8) session flat -> exit at close
        if _session_flat(ts_list[j]):
            exit_px, reason = c[j], "session_flat"
            break
        j += 1
    if exit_px is None:  # ran off the end while open -> drop (engine convention): mark last bar
        j = n - 1
        exit_px, reason = c[j], "eod_drop"
    depth = float(reg.ema[entry_i] - c[entry_i]) if reg.valid[entry_i] else float("nan")
    tr = BakeTrade(
        cand="C1_short",
        side="short",
        entry_ts=ts_list[entry_i],
        exit_ts=ts_list[j],
        entry_px=float(entry_px),
        exit_px=float(exit_px),
        gross_pts=float(entry_px - exit_px),  # short: profit when exit < entry
        init_risk_pts=float(risk_pts),
        exit_reason=reason,
        depth_pts=depth,
        entry_range=float(rng[entry_i]),
        entry_atr1m=float(atr[entry_i]) if not np.isnan(atr[entry_i]) else 0.0,
        exit_range=float(rng[j]),
        exit_atr1m=float(atr[j]) if not np.isnan(atr[j]) else 0.0,
    )
    tr._exit_i = j  # type: ignore[attr-defined]
    return tr


# --------------------------------------------------------------------------- #
# CANDIDATE 2 — CAPITULATION MEAN-REVERSION LONG
# --------------------------------------------------------------------------- #
def _rsi(close: pd.Series, period: int = 2) -> pd.Series:
    """Wilder RSI (ewm alpha=1/period, adjust=False) on a close series."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    return rsi.fillna(100.0)  # no losses in window -> RSI 100 (not a buy)


def build_15m(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 1-min -> closed 15-min OHLC; add ATR14(15m), Bollinger(20,2.5),
    RSI(2). Bollinger std is population (ddof=0), the common library convention."""
    g = (
        df.set_index("time")
        .resample("15min")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .dropna(subset=["close"])
    )
    g = add_atr(g, 14)  # -> g["atr"] = ATR14(15m)
    mid = g["close"].rolling(20).mean()
    sd = g["close"].rolling(20).std(ddof=0)
    g["bb_lower"] = mid - 2.5 * sd
    g["rsi2"] = _rsi(g["close"], 2)
    return g.reset_index().rename(columns={"index": "time"})


def run_candidate2(df: pd.DataFrame, reg: Regime30m) -> list[BakeTrade]:
    """Verbatim CAPITULATION LONG over 15-min signals + 1-min exits."""
    t = pd.DatetimeIndex(df["time"])
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    atr1m = df["atr"].to_numpy(float)
    rng = h - low
    ts_list = [x.to_pydatetime() for x in t]
    bar_ns = t.asi8
    n = len(df)

    g15 = build_15m(df)
    g_close = g15["close"].to_numpy(float)
    g_lower = g15["bb_lower"].to_numpy(float)
    g_rsi = g15["rsi2"].to_numpy(float)
    g_atr = g15["atr"].to_numpy(float)
    g_start = pd.DatetimeIndex(g15["time"])
    g_end_ns = (g_start + pd.Timedelta(minutes=15)).asi8

    trades: list[BakeTrade] = []
    next_free_ns = -1  # 1-min ns until which we are still in a prior position

    for b in range(len(g15)):
        if np.isnan(g_lower[b]) or np.isnan(g_atr[b]) or g_atr[b] <= 0:
            continue
        # RTH signal window: the 15m bar's CLOSE (bucket end) ET in [09:45, 15:15].
        close_et = (g_start[b] + pd.Timedelta(minutes=15)).to_pydatetime().astimezone(_ET)
        if not (time(9, 45) <= close_et.time() <= time(15, 15)):
            continue
        # capitulation signal on the CLOSED 15m bar.
        if not (g_close[b] < g_lower[b] and g_rsi[b] < 5.0):
            continue
        # entry = first 1-min bar at/after the 15m bar's END.
        ei = int(np.searchsorted(bar_ns, g_end_ns[b], side="left"))
        if ei >= n:
            break
        if bar_ns[ei] < next_free_ns:  # still in a prior trade -> skip (max 1 position)
            continue
        # regime: 1m close < 30m EMA200 at entry (gate-exact, completed bucket).
        if not reg.valid[ei] or not (c[ei] < reg.ema[ei]):
            continue
        # gap guard: the entry bar must be within ~5 min of the bucket end (else feed gap).
        if bar_ns[ei] - g_end_ns[b] > 5 * 60 * 1_000_000_000:
            continue

        a = float(g_atr[b])
        fill = o[ei]
        stop = round_to_tick(fill - 1.5 * a)
        target = round_to_tick(fill + 2.0 * a)
        tr = _simulate_long(ts_list, o, h, low, c, rng, atr1m, reg, ei, fill, stop, target, 1.5 * a)
        trades.append(tr)
        next_free_ns = bar_ns[tr._exit_i] + 1  # type: ignore[attr-defined]
    return trades


def _simulate_long(ts_list, o, h, low, c, rng, atr1m, reg, ei, fill, stop, target, risk_pts):
    """Run the LONG bracket bar-by-bar with a 15:55 ET flatten; returns a BakeTrade."""
    n = len(c)
    j = ei
    exit_px = None
    reason = None
    while j < n:
        et = ts_list[j].astimezone(_ET)
        # time stop FIRST when due (no overnight): flatten at/after 15:55 ET.
        if et.time() >= time(15, 55):
            exit_px, reason = c[j], "time_stop"
            break
        # 1) intrabar stop (below): low <= stop (pessimistic, checked before target)
        if low[j] <= stop:
            exit_px, reason = stop, "stop"
            break
        # 2) intrabar target (above): high >= target
        if h[j] >= target:
            exit_px, reason = target, "target"
            break
        j += 1
    if exit_px is None:
        j = n - 1
        exit_px, reason = c[j], "eod_drop"
    depth = float(reg.ema[ei] - c[ei]) if reg.valid[ei] else float("nan")
    tr = BakeTrade(
        cand="C2_long",
        side="long",
        entry_ts=ts_list[ei],
        exit_ts=ts_list[j],
        entry_px=float(fill),
        exit_px=float(exit_px),
        gross_pts=float(exit_px - fill),  # long: profit when exit > entry
        init_risk_pts=float(risk_pts),
        exit_reason=reason,
        depth_pts=depth,
        entry_range=float(rng[ei]),
        entry_atr1m=float(atr1m[ei]) if not np.isnan(atr1m[ei]) else 0.0,
        exit_range=float(rng[j]),
        exit_atr1m=float(atr1m[j]) if not np.isnan(atr1m[j]) else 0.0,
    )
    tr._exit_i = j  # type: ignore[attr-defined]
    return tr


# --------------------------------------------------------------------------- #
# Gated long reference book (for the daily-P&L correlation) — REAL TF exit
# --------------------------------------------------------------------------- #
def gated_daily_pnl(*, rebuild: bool, seg_len: int) -> tuple[dict, str]:
    """Daily per-ct net of the gated long strategy = the above-30m-EMA200 signal set
    replayed through the REAL TF trailing exit. Reuses the cached below-trend tape; the
    above-trend mask IS the gated book. Returns (date->net_usd_per_ct, note)."""
    try:
        tape = load_tape(GATED_TAPE_CACHE)
    except (OSError, EOFError, ValueError, TypeError, AttributeError) as exc:
        return {}, f"(gated reference unavailable: cached tape missing/unreadable: {exc})"
    if tape.bars != seg_len:
        return {}, (
            f"(gated reference SKIPPED: cached tape bars={tape.bars:,} != current "
            f"seg={seg_len:,}; rerun below_trend_study --rebuild-tape to refresh)"
        )
    trades = replay_masked(tape, cfg_params(75.0, 50.0, 150.0), mask_above(tape), qty=1)
    daily: dict = {}
    for t in trades:
        d = t.entry_ts.astimezone(_ET).date()
        daily[d] = daily.get(d, 0.0) + t.net_usd
    note = f"gated long (above-trend) reference: {len(trades):,} trades, {len(daily):,} active days"
    return daily, note


def candidate_daily_pnl(trades: list[BakeTrade]) -> dict:
    daily: dict = {}
    for t in trades:
        d = t.entry_ts.astimezone(_ET).date()
        daily[d] = daily.get(d, 0.0) + t.net_usd()
    return daily


def daily_correlation(cand_daily: dict, gated_daily: dict) -> dict:
    """Pearson correlation of candidate vs gated daily net. Reports the INTERSECTION
    (days both traded — the diversification read) and the UNION-over-gated-days
    (candidate filled 0 on non-trading days). Both flagged honest given likely sparsity."""
    inter_days = sorted(set(cand_daily) & set(gated_daily))
    union_days = sorted(set(gated_daily))  # the gated 'open-regime' day set
    out = {
        "n_gated_days": len(gated_daily),
        "n_cand_days": len(cand_daily),
        "n_overlap_days": len(inter_days),
    }

    def _pearson(xs, ys):
        if len(xs) < 3:
            return None
        x = np.asarray(xs, float)
        y = np.asarray(ys, float)
        if x.std() == 0 or y.std() == 0:
            return None
        return float(np.corrcoef(x, y)[0, 1])

    out["corr_intersection"] = _pearson(
        [cand_daily[d] for d in inter_days], [gated_daily[d] for d in inter_days]
    )
    out["corr_union_gateddays"] = _pearson(
        [cand_daily.get(d, 0.0) for d in union_days], [gated_daily[d] for d in union_days]
    )
    return out


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _fmt_stats(s: Stats) -> str:
    pf = "inf" if s.profit_factor == float("inf") else f"{s.profit_factor:.3f}"
    return (
        f"n={s.n:<4} win={s.win_rate * 100:5.1f}% exp/ct=${s.expectancy_usd:7.2f} "
        f"PF={pf:>6} net/ct=${s.net_usd:9.2f} maxDD=${s.max_drawdown_usd:8.2f}"
    )


def _year_split(trades: list[BakeTrade]) -> dict[int, Stats]:
    buckets: dict[int, list[BakeTrade]] = {}
    for t in trades:
        buckets.setdefault(t.year, []).append(t)
    return {y: stats(buckets[y]) for y in sorted(buckets)}


def _depth_split(trades: list[BakeTrade]) -> list[tuple[str, Stats]]:
    out = []
    for name, lo, hi in DEPTH_BANDS:
        sub = [t for t in trades if not np.isnan(t.depth_pts) and lo <= t.depth_pts < hi]
        out.append((name, stats(sub)))
    return out


def _single_year_dependence(years: dict[int, Stats], overall: Stats) -> tuple[bool, str]:
    """A candidate is single-year-dependent if dropping its single best-net year makes
    overall net <= 0 (the rest of the history does not stand on its own)."""
    if overall.net_usd <= 0 or not years:
        return False, "n/a (overall net <= 0)"
    best_year = max(years, key=lambda y: years[y].net_usd)
    rest = overall.net_usd - years[best_year].net_usd
    dep = rest <= 0
    return dep, (
        f"best year {best_year} net/ct ${years[best_year].net_usd:+.2f}; "
        f"remaining years net/ct ${rest:+.2f} -> "
        + ("CARRIED by one year" if dep else "stands without the best year")
    )


def _verdict(trades: list[BakeTrade]) -> tuple[bool, str]:
    """Head-to-head vs the gate's bar: PF>1.20 net AND expectancy>=$4/ct AND no
    single-year dependence (all on the headline cost model)."""
    if not trades:
        return False, "NO TRADES — the rule never fired (cannot clear the bar)."
    s = stats(trades)
    years = _year_split(trades)
    # inf PF (no losing trades) trivially clears the PF bar.
    pf_ok = True if s.profit_factor == float("inf") else (s.profit_factor >= PF_BAR)
    exp_ok = s.expectancy_usd >= EXPECTANCY_BAR_USD
    dep, dep_note = _single_year_dependence(years, s)
    passed = pf_ok and exp_ok and not dep
    pf_str = "inf" if s.profit_factor == float("inf") else f"{s.profit_factor:.3f}"
    parts = [
        f"PF {pf_str} {'>=' if pf_ok else '<'} {PF_BAR:.2f} [{'PASS' if pf_ok else 'FAIL'}]",
        f"exp/ct ${s.expectancy_usd:.2f} {'>=' if exp_ok else '<'} ${EXPECTANCY_BAR_USD:.0f} "
        f"[{'PASS' if exp_ok else 'FAIL'}]",
        f"single-year-dependence [{'FAIL' if dep else 'PASS'}: {dep_note}]",
    ]
    return passed, " | ".join(parts)


def _candidate_block(name: str, trades: list[BakeTrade], gated_daily: dict) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 96)
    add(name)
    add("=" * 96)
    if not trades:
        add("  *** NO TRADES — the verbatim rule produced zero entries over the full history.")
        passed, vtext = _verdict(trades)
        add(f"  VERDICT vs gate bar: {'PASS' if passed else 'FAIL'} — {vtext}")
        return "\n".join(lines)

    s = stats(trades)
    avg_r = float(np.mean([t.realized_r for t in trades]))
    pf_h = "inf" if s.profit_factor == float("inf") else f"{s.profit_factor:.3f}"
    add(
        f"  trades={s.n}  win={s.win_rate * 100:.1f}%  PF={pf_h}  "
        f"exp/ct=${s.expectancy_usd:.2f}  net/ct=${s.net_usd:.2f}  "
        f"maxDD/ct=${s.max_drawdown_usd:.2f}  avgR={avg_r:+.3f}"
    )
    add("")
    add("  -- exit reasons --")
    rc: dict[str, int] = {}
    rp: dict[str, float] = {}
    for t in trades:
        rc[t.exit_reason] = rc.get(t.exit_reason, 0) + 1
        rp[t.exit_reason] = rp.get(t.exit_reason, 0.0) + t.net_usd()
    for r in sorted(rc):
        add(f"     {r:<13} n={rc[r]:<4} net/ct=${rp[r]:+.2f}")
    add("")
    add("  -- per-year (flag if one year carries it) --")
    years = _year_split(trades)
    for y, ys in years.items():
        add(f"     {y}  {_fmt_stats(ys)}")
    dep, dep_note = _single_year_dependence(years, s)
    add(f"     single-year-dependence: {'YES' if dep else 'no'} — {dep_note}")
    add("")
    add("  -- by dormant-stretch depth (pts below 30m EMA200 at entry) --")
    for dn, ds in _depth_split(trades):
        add(f"     {dn:<16} {_fmt_stats(ds)}")
    add("")
    add("  -- cost sensitivity (PF / exp-per-ct) --")
    add(f"     {'model':<22} {'PF':>7} {'exp/ct$':>9} {'net/ct$':>11}")
    variants = [
        ("headline (1/2-tk, $0.31)", {}),
        ("0-tick slip, $0.31", {"fixed_ticks": 0}),
        ("1-tick slip, $0.31", {"fixed_ticks": 1}),
        ("2-tick slip, $0.31", {"fixed_ticks": 2}),
        ("headline, $0.62 (verified)", {"commission_per_side": COMMISSION_PER_SIDE_VERIFIED}),
    ]
    for label, cost in variants:
        vs = stats(trades, **cost)
        pf = "inf" if vs.profit_factor == float("inf") else f"{vs.profit_factor:.3f}"
        add(f"     {label:<22} {pf:>7} {vs.expectancy_usd:>9.2f} {vs.net_usd:>11.2f}")
    add("")
    add("  -- worst-5-trade tape (headline cost) --")
    worst = sorted(trades, key=lambda t: t.net_usd())[:5]
    add(
        f"     {'entry_ts (ET)':<17} {'side':<5} {'entry':>9} {'exit':>9} "
        f"{'reason':<12} {'R':>6} {'net/ct$':>9}"
    )
    for t in worst:
        ets = t.entry_ts.astimezone(_ET).strftime("%Y-%m-%d %H:%M")
        add(
            f"     {ets:<17} {t.side:<5} {t.entry_px:>9.2f} {t.exit_px:>9.2f} "
            f"{t.exit_reason:<12} {t.realized_r:>+6.2f} {t.net_usd():>+9.2f}"
        )
    add("")
    add("  -- daily P&L correlation vs the gated long strategy's open-regime days --")
    if gated_daily:
        cd = candidate_daily_pnl(trades)
        corr = daily_correlation(cd, gated_daily)
        ci = corr["corr_intersection"]
        cu = corr["corr_union_gateddays"]
        add(
            f"     gated active days={corr['n_gated_days']}  candidate days={corr['n_cand_days']}  "
            f"overlap days={corr['n_overlap_days']}"
        )
        ci_s = f"{ci:.3f}" if ci is not None else "n/a (n<3 or zero-var)"
        cu_s = f"{cu:.3f}" if cu is not None else "n/a"
        add(f"     corr (overlap days, both traded)             : {ci_s}")
        add(f"     corr (over all gated days, cand=0 when idle) : {cu_s}")
        add("     (candidates trade BELOW trend, the gated book ABOVE — low overlap by")
        add("      construction; read overlap corr as directional when overlap days are few.)")
    else:
        add("     (gated reference unavailable — see note in header)")
    add("")
    passed, vtext = _verdict(trades)
    add("  >>> VERDICT vs gate bar (PF>=1.20 net, exp>=$4/ct, no single-year dependence):")
    add(f"      {'PASS' if passed else 'FAIL'} — {vtext}")
    return "\n".join(lines)


def build_report(
    c1: list[BakeTrade],
    c2: list[BakeTrade],
    *,
    gated_daily: dict,
    gated_note: str,
    span: tuple,
    bars: int,
    roll_note: str,
    generated_at: datetime,
) -> str:
    lines: list[str] = []
    add = lines.append
    add("#" * 96)
    add("PHASE-15 — DORMANT-WINDOW STRATEGY BAKE-OFF (research, OFFLINE, modeled fills)")
    add(f"generated: {generated_at.isoformat()}   history: {span[0]} .. {span[1]}   bars: {bars:,}")
    add(
        f"30m regime: gate-exact resample('30min').last() + "
        f"ewm(span={REGIME_EMA_SPAN}, adjust=False),"
    )
    add(
        f"            >= {REGIME_MIN_BUCKETS} completed buckets; globally converged "
        f"(matches live -0.001pt)."
    )
    add(
        f"cost model: ${COMMISSION_PER_SIDE:.2f}/side commission + 1-tick base / "
        f"2-tick when fill-bar"
    )
    add(
        "            1-min range > 2*ATR14(1m); per-CONTRACT P&L; "
        "0/1/2-tick + $0.62 variants shown."
    )
    add(f"data prep : {roll_note}")
    add(f"reference : {gated_note}")
    add("#" * 96)
    add("")
    add(_candidate_block("CANDIDATE 1 — SHORT-CONTINUATION", c1, gated_daily))
    add("")
    add(_candidate_block("CANDIDATE 2 — CAPITULATION MEAN-REVERSION LONG", c2, gated_daily))
    add("")
    add("=" * 96)
    add("HEAD-TO-HEAD")
    add("=" * 96)
    p1, v1 = _verdict(c1)
    p2, v2 = _verdict(c2)
    add(f"  Candidate 1 (short-continuation)        : {'PASS' if p1 else 'FAIL'}")
    add(f"  Candidate 2 (capitulation long)         : {'PASS' if p2 else 'FAIL'}")
    if not p1 and not p2:
        add("")
        add("  >>> NEITHER candidate clears the gate's bar. 'NONE YET' is the honest result")
        add("      (the Botty / below-trend-edge precedent): the dormant window has no validated")
        add("      edge in EITHER direction on this history. Do NOT deploy either; the regime gate")
        add("      keeping TF flat below-trend remains the correct posture (§ change-management).")
    elif p1 and p2:
        add("  >>> BOTH clear the bar — surprising; treat with suspicion, demand a forward paper")
        add(
            "      window + an independent re-derivation before ANY deploy "
            "(overfit-to-history risk)."
        )
    else:
        winner = "Candidate 1 (short)" if p1 else "Candidate 2 (long)"
        add(f"  >>> Only {winner} clears the bar on this history. It is a CANDIDATE to evaluate")
        add(
            "      forward ONLY — a real deploy is a separate AUDIT decision + "
            "paper-forward proof;"
        )
        add(
            "      the forward edge in the live regime is the one thing this "
            "backtest cannot prove."
        )
    add("")
    add("CAVEATS: modeled fills (stop filled AT price intrabar, stop-before-target on a")
    add("same-bar collision); NEW research entries with no prod path; NQ~MNQ pts ($2 mult);")
    add("per-ct P&L; completed-bucket causal regime; NO param tuning beyond the verbatim")
    add("specs (validation, not optimization). Live forward edge unprovable here (§0.5.220).")
    add("=" * 96)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run(*, limit: int | None = None, rebuild_gated: bool = False) -> dict:
    df = data.load_history()
    if limit:
        df = df.iloc[:limit].reset_index(drop=True)
    df, roll_info = data.roll_adjust(df)
    roll_note = (
        f"roll-adjusted (Panama/difference): {len(roll_info.boundaries)} quarterly rolls, "
        f"offset {roll_info.total_offset_pts:.2f}pt"
    )
    seg = data.to_segments(df)[0]  # adds sma_100, atr (ATR14 1m), etc.
    reg = build_regime_30m(seg)
    c1 = run_candidate1(seg, reg)
    c2 = run_candidate2(seg, reg)
    gated_daily, gated_note = gated_daily_pnl(rebuild=rebuild_gated, seg_len=len(seg))
    return {
        "c1": c1,
        "c2": c2,
        "gated_daily": gated_daily,
        "gated_note": gated_note,
        "span": (df["time"].iloc[0], df["time"].iloc[-1]),
        "bars": len(df),
        "roll_note": roll_note,
    }


def main() -> None:
    from datetime import UTC

    ap = argparse.ArgumentParser(description="Phase-15 dormant-window strategy bake-off (research)")
    ap.add_argument("--limit", type=int, default=None, help="cap bars (smoke)")
    ap.add_argument("--rebuild-gated", action="store_true", help="(reserved) refresh gated tape")
    ap.add_argument(
        "--out", default=None, help="report path (default /tmp/dormant_bakeoff_<date>.txt)"
    )
    args = ap.parse_args()

    generated_at = datetime.now(UTC)
    res = run(limit=args.limit, rebuild_gated=args.rebuild_gated)
    report = build_report(
        res["c1"],
        res["c2"],
        gated_daily=res["gated_daily"],
        gated_note=res["gated_note"],
        span=res["span"],
        bars=res["bars"],
        roll_note=res["roll_note"],
        generated_at=generated_at,
    )
    out = args.out or f"/tmp/dormant_bakeoff_{generated_at.strftime('%Y-%m-%d')}.txt"
    with open(out, "w") as fh:
        fh.write(report + "\n")
    print(report)
    print(f"\n[written] {out}")


if __name__ == "__main__":
    main()
