"""Phase-9 (research, OFFLINE) — PORTFOLIO SIZING + DRAWDOWN-CONTROL study (Session 24).

The existing engine is SINGLE-position. This adds the missing piece: a portfolio-level
simulator that runs up to N concurrent positions (each M contracts, each its own
independent stop/trail lifecycle) on the SAME validated edge, tracks a portfolio equity
curve / aggregate exposure / portfolio MaxDD, and applies SeanBot's risk controls at the
portfolio level. Nothing here reimplements entry or exit:

  * Entry : the REAL ``engine.FastGateEntry`` (regime ON) — SB's strategy family (SMA50/100
            bounce + the C1 30m-EMA200 regime gate + 10-bar cooldown). A signal TAPE is
            built once (cooldown disabled) so each portfolio config replays cheaply; the
            replay re-applies the cooldown so it stays byte-faithful to the live path.
  * Exit  : the REAL ``engine._process_exit_bar`` -> ``trail_manager.compute_ratcheted_stop``
            / ``should_hard_exit`` (base entry-75, +50 lock, trail, 1000 ceiling), run per
            position independently.
  * Halt  : the REAL ``kill_switch.evaluate_triggers`` (warn@6 / halt@10 consecutive losses,
            + the realized-DD brake) applied to the portfolio's closed-trade stream.

WHY THIS MISSION (from SB's own code, now in hand): SB's strategy == ours; his own
backtests rate it PF 1.06-1.13 / WR ~33% / MaxDD 40-52% — matching ours. The dollar gap is
LEVERAGE: SB runs 2 contracts x up to 3 concurrent (config/settings.py POSITION_TIERS
[(0,2)], MAX_CONTRACTS=2, MAX_POSITIONS=3, EXIT_MODE trailing, SL75, TRAIL_OFFSET 250,
regime gate ON). TradeFlow runs a single position. This study reproduces SB's dollar profile
on our validated edge AND asks whether we can keep the return with LESS drawdown.

FIDELITY ANCHOR (mandatory): with N=1 and M=2 (the engine's default qty) the portfolio sim
reproduces ``engine.simulate_segment`` (regime ON) BYTE-FOR-BYTE — the validated
single-position result (n~1479 / PF 1.174 / +$28.7k). Per-trade exit prices, reasons and the
realized stats must match. Only then are the multi-position numbers trustworthy. (Net scales
linearly with M; M=1 simply halves it.)

PART A — MATCH SB: SB's exact live config (N=3, M=2, trailing, regime ON) over 26mo. Reported
in TWO regimes because TF's real single-position kill-switch (warn@6/halt@10 CONSECUTIVE
losses) bricks a concurrent book: (1) SB-EXACT (kill-switch ON) HALTS in month 1 — 3 correlated
longs stop together => 3 'consecutive losses' per down-move, so ~3-4 bad clusters trip halt@10;
(2) SB-LEVERAGE (premature halt OFF) shows the raw leveraged dollar profile. trail 250 (his) vs
150 (ours) side by side. Full portfolio picture: net$, equity-curve shape, PF, Sharpe, MaxDD
($ and %), rolling-10-trading-day P&L distribution (best/worst/median). The KEY RISK surfaced
explicitly: concurrent longs are CORRELATED — they stop together in a down-move, so 3-concurrent
MaxDD is NOT 3 independent books (~sqrt(3)x); quantified vs single x3.

PART B — BEAT HIM (drawdown control on the same edge): MODEST overlays — (a) vol-scaled
sizing, (b) equity-curve de-risking, (c) tighter portfolio daily-loss cap, (d) dynamic
max-concurrent by vol. Rolling train->test walk-forward selects the overlay on train by a
RISK-ADJUSTED objective (Calmar = net / MaxDD), scores OOS, aggregates. Reports selection
stability + a full-sample neighbor surface (plateau vs spike). VERDICT: which overlay (if any)
delivers materially LOWER MaxDD at comparable-or-better net return OUT-OF-SAMPLE.

CAVEATS (carried into every report, never hidden):
  * MODELED fills — entry at the signal-bar close; the protective stop fills AT the stop price
    intrabar (§0.5.206). Mark-to-market equity uses each bar's close. LOGIC expectancy, not
    live order plumbing or partial fills.
  * CORRELATED CONCURRENCY — the signal family clusters entries near the same pullback, so
    concurrent positions are highly correlated; the MaxDD here is the real (amplified) one,
    and is the central risk this study exists to surface.
  * Shared cooldown — one strategy instance => the 10-bar cooldown after ANY close gates the
    next entry portfolio-wide (the faithful single-instance model; it's also what makes N=1
    reproduce the engine). A per-slot cooldown would stack faster — a sensitivity, not the base.
  * NQ 1-min bars (point-identical to MNQ; $2 multiplier). §0.5.97 friction PER contract.
  * %-figures use a STATED nominal account (--start-capital, default $25k); $ figures are the
    unambiguous truth. Walk-forward is the honest read; the forward edge in the real regime is
    unprovable here — turning either sizing or concurrency on LIVE is a SEPARATE AUDIT for the
    operator AFTER these numbers AND a forward paper window.

  python -m tools.eval.portfolio_study [--validate] [--limit N] [--rebuild-tape]
                                       [--start-capital 25000]
"""

from __future__ import annotations

import argparse
import pickle
import time as _time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config.instruments import MNQ
from config.risk_params import RiskParams
from src.execution.kill_switch import evaluate_triggers
from tools.eval import data, engine
from tools.eval.backtest import friday_force_flat
from tools.eval.below_trend_study import _month_bounds
from tools.eval.engine import Trade, _pnl, _process_exit_bar
from tools.eval.metrics import Stats, compute_stats

TAPE_CACHE = "/tmp/tf_portfolio_tape.pkl"
DEFAULT_START_CAPITAL = 25_000.0  # stated nominal retail account for %-figures only

# Exit configs (stop_loss, lock_in, trail_offset).
TF_TRAIL = (75.0, 50.0, 150.0)  # TradeFlow live trailing-ratchet exit
SB_TRAIL = (75.0, 50.0, 250.0)  # SeanBot's TRAIL_OFFSET 250 (same SL/lock)


# --------------------------------------------------------------------------- #
# Signal tape (regime ON; cooldown disabled so the replay owns the cooldown)
# --------------------------------------------------------------------------- #
@dataclass
class PortfolioTape:
    ts: list
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    atr: np.ndarray
    sig_idx: np.ndarray  # bar indices where the REAL regime-ON gate fired long_signal
    sig_px: np.ndarray  # entry price (= bar close) at each sig_idx
    span: tuple
    bars: int


def _exit_params(stop_loss: float, lock_in: float, trail: float) -> RiskParams:
    return RiskParams(
        regime_gate_enabled=True,
        exit_mode="trailing",
        stop_loss_pts=stop_loss,
        lock_in_pts=lock_in,
        trail_offset_pts=trail,
    )


def build_tape(seg: pd.DataFrame, instrument: str = "NQ") -> PortfolioTape:
    """Run the REAL FastGateEntry with the regime gate ON over every bar, recording every
    long_signal (cooldown never armed -> raw gate fires; the replay re-applies cooldown)."""
    params = _exit_params(*TF_TRAIL)  # regime ON; exit knobs irrelevant to signal generation
    drv = engine.FastGateEntry(params, instrument)
    n = len(seg)
    sig_idx: list[int] = []
    sig_px: list[float] = []
    for j in range(n):
        decision, signal = drv.step(seg, j)
        if decision == "long_signal" and signal is not None:
            sig_idx.append(j)
            sig_px.append(float(signal.entry_price))
    ts = [t.to_pydatetime() if hasattr(t, "to_pydatetime") else t for t in seg["time"].tolist()]
    atr = seg["atr"].to_numpy(dtype=float) if "atr" in seg.columns else np.full(n, np.nan)
    return PortfolioTape(
        ts=ts,
        high=seg["high"].to_numpy(dtype=float),
        low=seg["low"].to_numpy(dtype=float),
        close=seg["close"].to_numpy(dtype=float),
        atr=atr,
        sig_idx=np.asarray(sig_idx, dtype=np.int64),
        sig_px=np.asarray(sig_px, dtype=float),
        span=(ts[0], ts[-1]),
        bars=n,
    )


def save_tape(tape: PortfolioTape, path: str) -> None:
    with open(path, "wb") as fh:
        pickle.dump(vars(tape), fh)


def load_tape(path: str) -> PortfolioTape:
    with open(path, "rb") as fh:
        d = pickle.load(fh)
    return PortfolioTape(**d) if isinstance(d, dict) else d


# --------------------------------------------------------------------------- #
# Portfolio config (sizing + the Part-B overlays, all OFF by default)
# --------------------------------------------------------------------------- #
@dataclass
class PortfolioConfig:
    name: str = "baseline"
    max_positions: int = 1  # N
    contracts: int = 2  # M (engine default qty == 2; the fidelity anchor)
    exit_cfg: tuple = TF_TRAIL  # (stop_loss, lock_in, trail_offset)
    friction_pts: float = engine.DEFAULT_FRICTION_PTS  # PER contract (§0.5.97)
    # --- SB portfolio risk controls (real kill_switch) ---
    kill_switch_enabled: bool = True  # False => no premature halt (raw leverage profile)
    warn_consec_losses: int = 6
    halt_consec_losses: int = 10
    allocation_usd: float | None = None  # DD brake INERT when None (SB default)
    max_drawdown_pct: float = 33.0
    cooldown_bars: int = 10
    # --- Part-B overlays (None/0 == off) ---
    vol_scale_atr_hi: float | None = None  # atr >= hi -> contracts halved (min 1)
    derisk_trigger_usd: float | None = None  # mtm DD >= trigger -> de-risk
    derisk_restore_usd: float | None = None  # mtm DD <= restore -> un-de-risk
    derisk_max_positions: int = 1  # N while de-risked
    derisk_contracts: int = 1  # M while de-risked
    daily_loss_cap_usd: float | None = None  # day realized <= -cap -> no new entries that day
    dyn_conc_atr_hi: float | None = None  # atr >= hi -> max_positions forced to dyn_conc_hi_n
    dyn_conc_hi_n: int = 1

    def exit_params(self) -> RiskParams:
        return _exit_params(*self.exit_cfg)


@dataclass
class PortfolioPosition:
    entry_ts: object
    entry_price: float
    highest: float
    current_stop: float
    bars_held: int
    contracts: int


@dataclass
class PortfolioResult:
    trades: list[Trade]
    equity_mtm: np.ndarray  # per-bar mark-to-market portfolio P&L (realized + unrealized)
    bar_ts: list
    max_concurrent_seen: int
    concurrency_bar_count: dict  # {n_open: bars} exposure histogram
    halted_at: object | None  # ts of a kill-switch pause, else None
    n_signals_taken: int
    n_signals_skipped_slot: int  # blocked because all N slots were full
    n_signals_skipped_cooldown: int


# --------------------------------------------------------------------------- #
# The portfolio simulator
# --------------------------------------------------------------------------- #
def simulate_portfolio(
    tape: PortfolioTape,
    cfg: PortfolioConfig,
    *,
    lo: int = 0,
    hi: int | None = None,
    force_flat=friday_force_flat,
) -> PortfolioResult:
    """Bar-by-bar portfolio replay over ``tape``. Up to ``cfg.max_positions`` concurrent
    positions, each ``cfg.contracts`` contracts, each its own real stop/trail lifecycle.

    At N=1, M=2, overlays off, this reproduces ``engine.simulate_segment`` (regime ON)
    byte-for-byte (same per-bar exit, same cooldown re-application, same single-slot rule)."""
    hi = tape.bars if hi is None else hi
    params = cfg.exit_params()
    sl = params.stop_loss_pts
    mult = MNQ.multiplier

    sig_set = {}
    s0 = int(np.searchsorted(tape.sig_idx, lo, side="left"))
    for k in range(s0, len(tape.sig_idx)):
        e = int(tape.sig_idx[k])
        if e >= hi:
            break
        sig_set[e] = float(tape.sig_px[k])

    open_positions: list[PortfolioPosition] = []
    trades: list[Trade] = []
    closed_pnls_newest_first: list[float] = []
    realized_cum = 0.0
    last_close_bar = -(10**9)
    halted = False
    halted_at = None
    equity = np.empty(hi - lo, dtype=float)
    bar_ts = tape.ts[lo:hi]
    max_conc = 0
    conc_hist: dict[int, int] = {}
    taken = skip_slot = skip_cd = 0

    # de-risk state + daily-cap state
    peak_equity = 0.0
    derisked = False
    cur_day = None
    day_realized = 0.0
    day_blocked = False

    for idx, j in enumerate(range(lo, hi)):
        ts = tape.ts[j]
        high = float(tape.high[j])
        low = float(tape.low[j])
        close = float(tape.close[j])

        # daily-cap day rollover (UTC date)
        day = ts.date()
        if day != cur_day:
            cur_day = day
            day_realized = 0.0
            day_blocked = False

        n_open_start = len(open_positions)

        # ---- 1) process the real exit for every open position ----
        still: list[PortfolioPosition] = []
        for pos in open_positions:
            closed = _process_exit_bar(
                pos, params, ts=ts, high=high, low=low, close=close, force_flat=force_flat
            )
            pos.bars_held += 1
            if closed is not None:
                exit_price, reason = closed
                gp, gu, nf, nc = _pnl(pos.entry_price, exit_price, cfg.friction_pts, pos.contracts)
                trades.append(
                    Trade(
                        entry_ts=pos.entry_ts,
                        entry_price=pos.entry_price,
                        exit_ts=ts,
                        exit_price=exit_price,
                        exit_reason=reason,
                        bars_held=pos.bars_held,
                        highest=pos.highest,
                        final_stop=pos.current_stop,
                        gross_pts=gp,
                        gross_usd=gu,
                        net_usd=nf,
                        net_usd_commission_only=nc,
                    )
                )
                realized_cum += nf
                day_realized += nf
                closed_pnls_newest_first.insert(0, nf)
                last_close_bar = j
            else:
                still.append(pos)
        open_positions = still

        # ---- 2) kill-switch (real evaluate_triggers) on the closed stream ----
        if cfg.kill_switch_enabled and not halted and len(closed_pnls_newest_first) > 0:
            verdict = evaluate_triggers(
                closed_pnls_newest_first,
                realized_cum,
                cfg.allocation_usd,
                warn_consec_losses=cfg.warn_consec_losses,
                halt_consec_losses=cfg.halt_consec_losses,
                max_drawdown_pct=cfg.max_drawdown_pct,
            )
            if verdict.action == "pause":
                # SB semantics: raise halt + flatten everything, cease trading for the run.
                for pos in open_positions:
                    gp, gu, nf, nc = _pnl(pos.entry_price, close, cfg.friction_pts, pos.contracts)
                    trades.append(
                        Trade(
                            entry_ts=pos.entry_ts,
                            entry_price=pos.entry_price,
                            exit_ts=ts,
                            exit_price=close,
                            exit_reason="kill_switch_flat",
                            bars_held=pos.bars_held,
                            highest=pos.highest,
                            final_stop=pos.current_stop,
                            gross_pts=gp,
                            gross_usd=gu,
                            net_usd=nf,
                            net_usd_commission_only=nc,
                        )
                    )
                    realized_cum += nf
                open_positions = []
                halted = True
                halted_at = ts

        # ---- 3) overlays: resolve the effective N and M for a NEW entry on this bar ----
        atr_j = float(tape.atr[j]) if not np.isnan(tape.atr[j]) else 0.0
        n_eff = cfg.max_positions
        m_eff = cfg.contracts
        # (d) dynamic max-concurrent by vol
        if cfg.dyn_conc_atr_hi is not None and atr_j >= cfg.dyn_conc_atr_hi:
            n_eff = min(n_eff, cfg.dyn_conc_hi_n)
        # (a) vol-scaled sizing
        if cfg.vol_scale_atr_hi is not None and atr_j >= cfg.vol_scale_atr_hi:
            m_eff = max(1, m_eff // 2)
        # (b) equity-curve de-risking (mtm drawdown vs running peak)
        if cfg.derisk_trigger_usd is not None:
            mtm = realized_cum + sum(
                (close - p.entry_price) * mult * p.contracts for p in open_positions
            )
            dd = peak_equity - mtm
            if not derisked and dd >= cfg.derisk_trigger_usd:
                derisked = True
            elif derisked and cfg.derisk_restore_usd is not None and dd <= cfg.derisk_restore_usd:
                derisked = False
            if derisked:
                n_eff = min(n_eff, cfg.derisk_max_positions)
                m_eff = min(m_eff, cfg.derisk_contracts)
        # (c) daily loss cap
        if cfg.daily_loss_cap_usd is not None and day_realized <= -cfg.daily_loss_cap_usd:
            day_blocked = True

        # ---- 4) entry: a signal this bar, slot free at bar start, cooldown clear, not halted ----
        if j in sig_set and not halted and not day_blocked:
            cooldown_ok = j >= last_close_bar + 1 + cfg.cooldown_bars
            if not cooldown_ok:
                skip_cd += 1
            elif n_open_start >= n_eff:
                skip_slot += 1
            else:
                epx = sig_set[j]
                open_positions.append(
                    PortfolioPosition(
                        entry_ts=ts,
                        entry_price=epx,
                        highest=epx,
                        current_stop=epx - sl,
                        bars_held=0,
                        contracts=m_eff,
                    )
                )
                taken += 1

        # ---- 5) mark-to-market equity + exposure bookkeeping ----
        mtm = realized_cum + sum(
            (close - p.entry_price) * mult * p.contracts for p in open_positions
        )
        equity[idx] = mtm
        peak_equity = max(peak_equity, mtm)
        n_now = len(open_positions)
        max_conc = max(max_conc, n_now)
        conc_hist[n_now] = conc_hist.get(n_now, 0) + 1

    return PortfolioResult(
        trades=trades,
        equity_mtm=equity,
        bar_ts=bar_ts,
        max_concurrent_seen=max_conc,
        concurrency_bar_count=conc_hist,
        halted_at=halted_at,
        n_signals_taken=taken,
        n_signals_skipped_slot=skip_slot,
        n_signals_skipped_cooldown=skip_cd,
    )


# --------------------------------------------------------------------------- #
# Portfolio metrics
# --------------------------------------------------------------------------- #
@dataclass
class PortfolioMetrics:
    n_trades: int
    win_rate: float
    net_usd: float
    profit_factor: float
    maxdd_realized_usd: float  # peak-to-trough on the realized closed-trade curve
    maxdd_mtm_usd: float  # peak-to-trough on the per-bar mark-to-market curve (TRUE DD)
    maxdd_mtm_pct: float  # as % of start_capital (stated nominal)
    return_pct: float  # net / start_capital
    calmar: float  # net_usd / maxdd_mtm_usd (capital-free risk-adjusted)
    sharpe_daily_ann: float
    roll10_best: float
    roll10_worst: float
    roll10_median: float
    max_concurrent: int
    avg_concurrent_when_open: float


def _max_drawdown_curve(equity: np.ndarray) -> float:
    peak = -np.inf
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return float(mdd)


def _daily_pnl_series(trades: list[Trade]) -> pd.Series:
    if not trades:
        return pd.Series(dtype=float)
    by_day: dict = {}
    for t in trades:
        d = t.exit_ts.date()
        by_day[d] = by_day.get(d, 0.0) + t.net_usd
    s = pd.Series(by_day).sort_index()
    full = pd.date_range(s.index.min(), s.index.max(), freq="B")
    return s.reindex(full.date, fill_value=0.0)


def compute_portfolio_metrics(result: PortfolioResult, *, start_capital: float) -> PortfolioMetrics:
    trades = result.trades
    base = compute_stats(trades)
    maxdd_real = base.max_drawdown_usd
    maxdd_mtm = _max_drawdown_curve(result.equity_mtm) if len(result.equity_mtm) else 0.0
    net = base.net_usd
    daily = _daily_pnl_series(trades)
    if len(daily) > 1 and daily.std(ddof=0) > 0:
        sharpe = float(daily.mean() / daily.std(ddof=0) * np.sqrt(252.0))
    else:
        sharpe = 0.0
    if len(daily) >= 10:
        roll = daily.rolling(10).sum().dropna()
        r_best, r_worst, r_med = float(roll.max()), float(roll.min()), float(roll.median())
    else:
        r_best = r_worst = r_med = 0.0
    open_bars = sum(n * c for n, c in result.concurrency_bar_count.items() if n > 0)
    open_bar_count = sum(c for n, c in result.concurrency_bar_count.items() if n > 0)
    avg_conc = (open_bars / open_bar_count) if open_bar_count else 0.0
    return PortfolioMetrics(
        n_trades=base.n,
        win_rate=base.win_rate,
        net_usd=net,
        profit_factor=base.profit_factor,
        maxdd_realized_usd=maxdd_real,
        maxdd_mtm_usd=maxdd_mtm,
        maxdd_mtm_pct=(maxdd_mtm / start_capital * 100.0) if start_capital else 0.0,
        return_pct=(net / start_capital * 100.0) if start_capital else 0.0,
        calmar=(net / maxdd_mtm) if maxdd_mtm > 0 else float("inf"),
        sharpe_daily_ann=sharpe,
        roll10_best=r_best,
        roll10_worst=r_worst,
        roll10_median=r_med,
        max_concurrent=result.max_concurrent_seen,
        avg_concurrent_when_open=avg_conc,
    )


# --------------------------------------------------------------------------- #
# Fidelity anchor — N=1, M=2 portfolio == engine.simulate_segment (regime ON)
# --------------------------------------------------------------------------- #
def validate(seg: pd.DataFrame, tape: PortfolioTape) -> tuple[bool, str]:
    """N=1, M=2, no overlays, no kill-switch trips -> reproduce engine.simulate_segment
    (regime ON) byte-for-byte: same trade count, per-trade exit price/reason, and net."""
    params = _exit_params(*TF_TRAIL)
    eng = engine.simulate_segment(
        seg, engine.FastGateEntry(params, "NQ"), params, qty=2, force_flat=friday_force_flat
    ).trades
    cfg = PortfolioConfig(name="anchor", max_positions=1, contracts=2, exit_cfg=TF_TRAIL)
    res = simulate_portfolio(tape, cfg)
    port = [t for t in res.trades if t.exit_reason != "kill_switch_flat"]
    mism = 0
    for a, b in zip(eng, port, strict=False):
        if (
            a.entry_ts != b.entry_ts
            or abs(a.exit_price - b.exit_price) > 1e-6
            or a.exit_reason != b.exit_reason
            or abs(a.net_usd - b.net_usd) > 1e-6
        ):
            mism += 1
    en = sum(t.net_usd for t in eng)
    pn = sum(t.net_usd for t in port)
    se = compute_stats(eng)
    ok = len(eng) == len(port) and mism == 0 and abs(en - pn) < 1e-3
    msg = (
        f"n_engine={len(eng)} n_portfolio={len(port)} engine_net={en:.2f} "
        f"portfolio_net={pn:.2f} per_trade_mismatch={mism} PF={se.profit_factor:.3f} "
        f"-> {'OK' if ok else 'FAIL'}"
    )
    return ok, msg


# --------------------------------------------------------------------------- #
# Walk-forward over the Part-B overlay set (select by Calmar on train, score OOS)
# --------------------------------------------------------------------------- #
@dataclass
class FoldResult:
    train_label: str
    test_label: str
    sel_name: str
    train_calmar: float
    test: PortfolioMetrics
    test_trades: list[Trade] = field(default_factory=list)


def overlay_grid(base_exit: tuple) -> list[PortfolioConfig]:
    """A MODEST curated overlay set on the SB sizing frame (N=3, M=2) — multiple-comparison
    guard. Each is a named, distinct risk treatment; the walk-forward picks among them.

    All run with kill_switch_enabled=False: Part A showed TF's single-position halt@10 bricks
    a concurrent book in month 1, so leaving it on would make overlays 'win' by dodging the
    premature halt (a confound). Disabling it lets each overlay compete on its actual
    drawdown-control merit over the full window. Re-calibrating the kill-switch for concurrency
    is a SEPARATE control (a recommendation), not one of these drawdown overlays."""
    return [
        PortfolioConfig(
            name="SB-baseline",
            max_positions=3,
            contracts=2,
            exit_cfg=base_exit,
            kill_switch_enabled=False,
        ),
        PortfolioConfig(
            name="vol-scaled",
            max_positions=3,
            contracts=2,
            exit_cfg=base_exit,
            kill_switch_enabled=False,
            vol_scale_atr_hi=7.0,  # ~p85 of signal-bar ATR(14): halve size on the top ~15% vol
        ),
        PortfolioConfig(
            name="equity-derisk",
            max_positions=3,
            contracts=2,
            exit_cfg=base_exit,
            kill_switch_enabled=False,
            derisk_trigger_usd=3000.0,
            derisk_restore_usd=1500.0,
            derisk_max_positions=1,
            derisk_contracts=1,
        ),
        PortfolioConfig(
            name="daily-cap",
            max_positions=3,
            contracts=2,
            exit_cfg=base_exit,
            kill_switch_enabled=False,
            daily_loss_cap_usd=1500.0,
        ),
        PortfolioConfig(
            name="dyn-concurrency",
            max_positions=3,
            contracts=2,
            exit_cfg=base_exit,
            kill_switch_enabled=False,
            dyn_conc_atr_hi=9.0,  # ~p90 of signal-bar ATR(14): drop to N=1 on the top ~10% vol
            dyn_conc_hi_n=1,
        ),
    ]


def walk_forward(
    tape: PortfolioTape,
    *,
    start_capital: float,
    base_exit: tuple = SB_TRAIL,
    train_months: int = 6,
    test_months: int = 2,
    step_months: int = 2,
    min_train_trades: int = 20,
) -> tuple[list[FoldResult], list[Trade], dict[str, list[PortfolioMetrics]]]:
    """Rolling train->test. On train pick the overlay with the best Calmar (net/MaxDD),
    score that overlay on the FOLLOWING test fold, pool OOS trades. Also returns each
    overlay's per-test-fold metrics for a stability/robustness read."""
    months = _month_bounds(tape)
    n_months = len(months)
    grid = overlay_grid(base_exit)
    folds: list[FoldResult] = []
    oos: list[Trade] = []
    per_overlay_test: dict[str, list[PortfolioMetrics]] = {c.name: [] for c in grid}
    m = 0
    while m + train_months + test_months <= n_months:
        tr0, tr1 = months[m][1], months[m + train_months - 1][2]
        te0, te1 = months[m + train_months][1], months[m + train_months + test_months - 1][2]
        train_label = f"{months[m][0]}..{months[m + train_months - 1][0]}"
        test_label = (
            f"{months[m + train_months][0]}..{months[m + train_months + test_months - 1][0]}"
        )
        best = None  # (calmar, cfg, train_metrics)
        for cfg in grid:
            tr = simulate_portfolio(tape, cfg, lo=tr0, hi=tr1)
            mt = compute_portfolio_metrics(tr, start_capital=start_capital)
            if mt.n_trades < min_train_trades:
                continue
            cal = mt.calmar if mt.calmar != float("inf") else 1e9
            if best is None or cal > best[0]:
                best = (cal, cfg, mt)
        if best is None:
            m += step_months
            continue
        _cal, cfg, train_mt = best
        te = simulate_portfolio(tape, cfg, lo=te0, hi=te1)
        te_mt = compute_portfolio_metrics(te, start_capital=start_capital)
        oos.extend(te.trades)
        folds.append(
            FoldResult(train_label, test_label, cfg.name, train_mt.calmar, te_mt, te.trades)
        )
        for cfg2 in grid:  # stability: every overlay scored on this test fold
            t2 = simulate_portfolio(tape, cfg2, lo=te0, hi=te1)
            per_overlay_test[cfg2.name].append(
                compute_portfolio_metrics(t2, start_capital=start_capital)
            )
        m += step_months
    return folds, oos, per_overlay_test


# --------------------------------------------------------------------------- #
# Verdict — does any overlay beat SB OOS? (lower MaxDD, comparable-or-better net)
# --------------------------------------------------------------------------- #
def verdict_beats_sb(
    base_trades: list[Trade],
    overlay_pools: dict[str, list[Trade]],
    *,
    dd_frac: float = 0.85,
    net_frac: float = 0.90,
) -> tuple[str, float, Stats] | None:
    """Pick the overlay that delivers materially LOWER MaxDD at comparable-or-better
    net OOS, or None if no overlay qualifies (incl. a degenerate/empty base).

    "comparable-or-better net" is sign-aware: if the base is net-profitable, an overlay
    must keep >= ``net_frac`` of it; if the base is net-negative, an overlay must at
    least not lose MORE (``net >= base_net``). The degenerate guard stops an empty pool
    (base_net == base_mdd == 0) being falsely reported as a winner."""
    base = compute_stats(base_trades)
    if base.n == 0 or base.max_drawdown_usd <= 0:
        return None
    net_floor = min(net_frac * base.net_usd, base.net_usd)
    best: tuple[str, float, Stats] | None = None
    for name, trades in overlay_pools.items():
        if name == "SB-baseline":
            continue
        st = compute_stats(trades)
        if st.n == 0:
            continue
        lower_dd = st.max_drawdown_usd <= dd_frac * base.max_drawdown_usd
        comparable_net = st.net_usd >= net_floor
        if lower_dd and comparable_net:
            cal = (st.net_usd / st.max_drawdown_usd) if st.max_drawdown_usd > 0 else float("inf")
            if best is None or cal > best[1]:
                best = (name, cal, st)
    return best


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _fmt_m(m: PortfolioMetrics) -> str:
    pf = f"{m.profit_factor:.3f}" if m.profit_factor != float("inf") else "inf"
    cal = f"{m.calmar:.2f}" if m.calmar != float("inf") else "inf"
    return (
        f"n={m.n_trades:<5} win={m.win_rate * 100:4.1f}% net=${m.net_usd:>10.0f} "
        f"PF={pf:>6} maxDD(mtm)=${m.maxdd_mtm_usd:>9.0f} ({m.maxdd_mtm_pct:4.0f}%) "
        f"Calmar={cal:>5} Sharpe={m.sharpe_daily_ann:5.2f}"
    )


def build_report(tape: PortfolioTape, validate_msg: str, *, start_capital: float) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 100)
    add("PHASE 9 (research) — PORTFOLIO SIZING + DRAWDOWN-CONTROL STUDY (real entry + real exit)")
    add("=" * 100)
    add("  OFFLINE research. No prod path. Turning live sizing/concurrency on = a SEPARATE AUDIT")
    add("  for the operator AFTER these numbers + a forward paper window. Modeled fills;")
    add("  CORRELATED concurrency (the MaxDD here is the real, amplified one).")
    add("")
    add(f"data span    : {tape.span[0]}  ->  {tape.span[1]}")
    add(f"bars         : {tape.bars:,}   regime-ON gate signals (raw) : {len(tape.sig_idx):,}")
    add(f"nominal acct : ${start_capital:,.0f} (for %-figures only; $ figures are the truth)")
    add(f"friction     : {engine.DEFAULT_FRICTION_PTS} idx-pt PER contract (§0.5.97)")
    add("")

    # ---- FIDELITY ANCHOR ----
    add("-- FIDELITY ANCHOR: N=1, M=2 portfolio == engine.simulate_segment (regime ON) ------")
    add(f"  {validate_msg}")
    anchor = compute_portfolio_metrics(
        simulate_portfolio(tape, PortfolioConfig("anchor", 1, 2, TF_TRAIL)),
        start_capital=start_capital,
    )
    add(f"  N=1 M=2 (current TF single-position, trail150) : {_fmt_m(anchor)}")
    add("")

    # ---- PART A — MATCH SB ----
    add("=" * 100)
    add("PART A — MATCH SB (his dollars on our edge): N=3 concurrent x M=2 contracts, regime ON")
    add("=" * 100)
    # Two regimes: (1) SB's EXACT live config including TF's real kill-switch (warn@6/halt@10);
    # (2) the same sizing with the premature halt DISABLED, to see the raw leveraged dollar
    # profile. The gap between them is itself a headline finding (see KILL-SWITCH below).
    configs_a = [
        ("single   TF t150 (N=1,M=2) ks-on", PortfolioConfig("a-single", 1, 2, TF_TRAIL)),
        ("SB-EXACT SB t250 (N=3,M=2) ks-on", PortfolioConfig("a-sb-exact", 3, 2, SB_TRAIL)),
        (
            "SB-lev   SB t250 (N=3,M=2) ks-OFF",
            PortfolioConfig("a-lev250", 3, 2, SB_TRAIL, kill_switch_enabled=False),
        ),
        (
            "SB-lev   TF t150 (N=3,M=2) ks-OFF",
            PortfolioConfig("a-lev150", 3, 2, TF_TRAIL, kill_switch_enabled=False),
        ),
    ]
    results_a = {}
    for label, cfg in configs_a:
        r = simulate_portfolio(tape, cfg)
        mt = compute_portfolio_metrics(r, start_capital=start_capital)
        results_a[cfg.name] = (r, mt)
        add(f"  {label:<34}: {_fmt_m(mt)}")
    add("")

    # ---- the KILL-SWITCH finding (why SB-exact != SB's dollars) ----
    r_exact, mt_exact = results_a["a-sb-exact"]
    add("  KILL-SWITCH TRAP (the first headline) — SB's EXACT config bricks itself early:")
    add(
        f"    N=3 with TF's real kill-switch (halt@10 consecutive losses) HALTS at "
        f"{r_exact.halted_at}"
    )
    add(
        f"    after only {mt_exact.n_trades} trades (net ${mt_exact.net_usd:,.0f}) — vs "
        f"{results_a['a-lev250'][1].n_trades} trades un-halted."
    )
    add("    WHY: 3 concurrent longs are correlated; one down-move stops all 3 => 3 'consecutive")
    add("    losses' at once, so ~3-4 bad clusters trip the halt@10 that was calibrated for a")
    add("    SINGLE-position book. The single-position kill-switch is the WRONG control for a")
    add("    concurrent book — turning on SB's concurrency WITHOUT re-calibrating it bricks the")
    add("    bot in month 1. (This is an operational gate, separate from the edge.)")
    add("")

    # ---- SB's real leveraged dollar profile (premature halt disabled) ----
    r_lev, mt_lev = results_a["a-lev250"]
    mt_single = results_a["a-single"][1]
    add("  SB's LEVERAGED DOLLAR PROFILE (N=3, halt disabled — what the leverage actually buys):")
    add(f"    single (N=1)      : {_fmt_m(mt_single)}")
    add(f"    SB-leverage (N=3) : {_fmt_m(mt_lev)}")
    add(
        f"    => net {mt_lev.net_usd / mt_single.net_usd:.2f}x single on "
        f"{mt_lev.n_trades / max(1, mt_single.n_trades):.2f}x the trades; "
        f"Calmar {mt_single.calmar:.2f} -> {mt_lev.calmar:.2f} (risk-adjusted return DROPS)."
    )
    add("")
    add("  rolling 10-trading-day P&L distribution (best / median / worst 10-day window):")
    for key, label in [
        ("a-single", "single   N=1 t150"),
        ("a-lev250", "SB-lev   N=3 t250"),
        ("a-lev150", "SB-lev   N=3 t150"),
    ]:
        mt = results_a[key][1]
        add(
            f"    {label:<22}: best=+${mt.roll10_best:>8.0f}  median=${mt.roll10_median:>8.0f}  "
            f"worst=${mt.roll10_worst:>9.0f}"
        )
    add("")
    add("  aggregate exposure (SB-leverage N=3 t250):")
    add(
        f"    max concurrent reached={mt_lev.max_concurrent}  avg concurrent (when in a "
        f"trade)={mt_lev.avg_concurrent_when_open:.2f}"
    )
    total_bars = sum(r_lev.concurrency_bar_count.values())
    for n_open in sorted(r_lev.concurrency_bar_count):
        share = r_lev.concurrency_bar_count[n_open] / total_bars * 100.0
        add(f"      {n_open} open : {share:5.1f}% of bars")
    add("")
    add("  CORRELATED-CONCURRENCY RISK (the central warning) — N=3 MaxDD vs single (halt off):")
    single_mdd = mt_single.maxdd_mtm_usd
    lev250_mdd = mt_lev.maxdd_mtm_usd
    lev150_mdd = results_a["a-lev150"][1].maxdd_mtm_usd
    add(f"    single (N=1,M=2) MaxDD            : ${single_mdd:,.0f}")
    add(f"    if 3 INDEPENDENT books (~sqrt3 x) : ${single_mdd * (3**0.5):,.0f}  (diversified)")
    add(f"    if 3 PERFECTLY correlated (x3)    : ${single_mdd * 3:,.0f}  (stop together)")
    add(
        f"    ACTUAL N=3 MaxDD t250            : ${lev250_mdd:,.0f}  "
        f"({lev250_mdd / single_mdd:.2f}x single)"
    )
    add(
        f"    ACTUAL N=3 MaxDD t150            : ${lev150_mdd:,.0f}  "
        f"({lev150_mdd / single_mdd:.2f}x single)"
    )
    add("    => at/above x3 (not sqrt3): concurrency is a drawdown AMPLIFIER, not diversification")
    add("       (entries cluster on the same pullback + the bigger book also takes MORE trades).")
    add("")
    add("  Reconcile vs SB's documented PF ~1.13 / MaxDD ~40%: PF here is the same family;")
    add(f"  MaxDD% depends on the account base (${start_capital:,.0f} assumed).")
    add("")

    # ---- PART B — BEAT HIM ----
    add("=" * 100)
    add("PART B — BEAT HIM (drawdown control on the SAME edge): walk-forward, select by Calmar")
    add("=" * 100)
    add("  overlays (modest, curated): SB-baseline, vol-scaled, equity-derisk, daily-cap, dyn-conc")
    add("  base sizing N=3 x M=2; base exit SB trail250; KILL-SWITCH OFF (so overlays compete on")
    add("  drawdown control, not on dodging the month-1 halt-trap). Calmar = net / MaxDD(mtm).")
    add("")
    add("  -- FULL-SAMPLE neighbor surface (in-sample; plateau=robust, spike=overfit) --")
    grid = overlay_grid(SB_TRAIL)
    for cfg in grid:
        mt = compute_portfolio_metrics(simulate_portfolio(tape, cfg), start_capital=start_capital)
        add(f"    {cfg.name:<16}: {_fmt_m(mt)}")
    add("")
    add("  -- WALK-FORWARD (train=6mo -> test=2mo, step=2mo; pick best-Calmar overlay on train) --")
    folds, oos, per_overlay = walk_forward(tape, start_capital=start_capital, base_exit=SB_TRAIL)
    add(
        f"    {'train':>16} {'test':>16}  {'selected':>15} {'tr.Calmar':>9}  ||  "
        f"{'te.net$':>9} {'te.maxDD$':>9} {'te.Calmar':>9}"
    )
    for f in folds:
        trc = f"{f.train_calmar:.2f}" if f.train_calmar != float("inf") else "inf"
        tec = f"{f.test.calmar:.2f}" if f.test.calmar != float("inf") else "inf"
        add(
            f"    {f.train_label:>16} {f.test_label:>16}  {f.sel_name:>15} {trc:>9}  ||  "
            f"{f.test.net_usd:>9.0f} {f.test.maxdd_mtm_usd:>9.0f} {tec:>9}"
        )
    add("")
    oos_stats = compute_stats(oos)
    oos_net = oos_stats.net_usd
    oos_mdd = oos_stats.max_drawdown_usd
    add("  AGGREGATE OUT-OF-SAMPLE (walk-forward selected overlay, all test folds pooled):")
    add(
        f"    n={oos_stats.n}  net=${oos_net:,.0f}  realized-MaxDD=${oos_mdd:,.0f}  "
        f"PF={oos_stats.profit_factor:.3f}  "
        f"Calmar(net/realDD)={(oos_net / oos_mdd) if oos_mdd > 0 else float('inf'):.2f}"
    )
    sel_counts: dict = {}
    for f in folds:
        sel_counts[f.sel_name] = sel_counts.get(f.sel_name, 0) + 1
    add(f"    selected-overlay frequency: {dict(sorted(sel_counts.items(), key=lambda x: -x[1]))}")
    add("")
    add("  -- per-overlay OOS stability (each overlay scored on EVERY test fold, pooled) --")
    add(f"    {'overlay':<16} {'sum.net$':>10} {'sum.maxDD$(pooled)':>18} {'mean.te.Calmar':>14}")
    # rebuild pooled OOS per overlay by re-running each on the test windows (cheap)
    months = _month_bounds(tape)
    n_months = len(months)
    pooled: dict[str, list[Trade]] = {c.name: [] for c in grid}
    mm = 0
    while mm + 6 + 2 <= n_months:
        te0 = months[mm + 6][1]
        te1 = months[mm + 6 + 2 - 1][2]
        for cfg in grid:
            pooled[cfg.name].extend(simulate_portfolio(tape, cfg, lo=te0, hi=te1).trades)
        mm += 2
    baseline_pool = compute_stats(pooled["SB-baseline"])
    for cfg in grid:
        st = compute_stats(pooled[cfg.name])
        cals = [m.calmar for m in per_overlay[cfg.name] if m.calmar != float("inf")]
        mean_cal = (sum(cals) / len(cals)) if cals else float("inf")
        add(
            f"    {cfg.name:<16} {st.net_usd:>10.0f} {st.max_drawdown_usd:>18.0f} "
            f"{mean_cal:>14.2f}"
        )
    add("")

    # ---- VERDICT ----
    add("-- VERDICT ----------")
    base_net = baseline_pool.net_usd
    base_mdd = baseline_pool.max_drawdown_usd
    best_overlay = verdict_beats_sb(pooled["SB-baseline"], pooled)
    add(
        f"  SB-baseline pooled OOS: net=${base_net:,.0f}  realized-MaxDD=${base_mdd:,.0f}  "
        f"Calmar={(base_net / base_mdd) if base_mdd > 0 else float('inf'):.2f}"
    )
    if best_overlay is not None:
        name, cal, st = best_overlay
        add(
            f"  BEATS SB (lower MaxDD, comparable+ net, OOS): {name} -> net=${st.net_usd:,.0f} "
            f"(>=90% of base), MaxDD=${st.max_drawdown_usd:,.0f} (<=85% of base), Calmar={cal:.2f}"
        )
    else:
        add("  No overlay delivers materially lower MaxDD at comparable-or-better net OOS.")
        add("  => On this edge, the modest drawdown-control overlays do NOT beat SB's")
        add("     risk-adjusted profile out-of-sample (the honest answer).")
    add("")
    add("-- CAVEATS ----------")
    add("  * Modeled fills; CORRELATED concurrency (the MaxDD is the real amplified one).")
    add("  * KILL-SWITCH: SB's exact config keeps TF's halt@10, which bricks the N=3 book in")
    add("    month 1 (Part A). The leverage/Part-B numbers DISABLE that premature halt; turning")
    add("    on concurrency live REQUIRES re-calibrating the kill-switch (per-cluster or higher")
    add("    threshold) first — a separate control, surfaced here, not solved here.")
    add("  * Shared 10-bar cooldown (one strategy instance); per-slot cooldown would stack faster.")
    add("  * %-figures use the stated nominal account; $ figures are unambiguous.")
    add("  * NQ≈MNQ points, $2 mult; §0.5.97 friction PER contract. Grid kept modest.")
    add("  * Walk-forward is the honest read. The forward edge in the real regime is unprovable")
    add("    here — turning sizing/concurrency on LIVE is a SEPARATE AUDIT + a forward paper")
    add("    window before any trust.")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap bars (smoke)")
    ap.add_argument("--rebuild-tape", action="store_true")
    ap.add_argument("--validate", action="store_true", help="assert N=1,M=2 == engine (anchor)")
    ap.add_argument("--start-capital", type=float, default=DEFAULT_START_CAPITAL)
    ap.add_argument("--tape-cache", default=TAPE_CACHE)
    args = ap.parse_args()

    df = data.load_history()
    if args.limit:
        df = df.iloc[: args.limit].reset_index(drop=True)
    seg = data.to_segments(df)[0]

    tape = None
    if not args.rebuild_tape and not args.limit:
        try:
            tape = load_tape(args.tape_cache)
            if tape.bars != len(seg):
                tape = None
        except (OSError, pickle.PickleError, TypeError, AttributeError):
            tape = None
    if tape is None:
        t0 = _time.perf_counter()
        tape = build_tape(seg, "NQ")
        print(
            f"[tape] built {tape.bars:,} bars, {len(tape.sig_idx):,} regime-ON signals "
            f"in {_time.perf_counter() - t0:.0f}s"
        )
        if not args.limit:
            save_tape(tape, args.tape_cache)

    vmsg = "skipped (pass --validate)"
    if args.validate:
        ok, vmsg = validate(seg, tape)
        vmsg = ("PASS  " if ok else "FAIL  ") + vmsg
        if not ok:
            raise SystemExit(f"[validate] N=1,M=2 portfolio != engine — ABORT study. {vmsg}")

    print(build_report(tape, vmsg, start_capital=args.start_capital))


if __name__ == "__main__":
    main()
