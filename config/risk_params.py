"""Risk parameters. Defaults from SeanBot + kickoff §3.

Field names aligned with the SeanBot signal-detection reference in PR #10:
- ma_touch_buffer_pts / ma_min_gap_pts / stop_loss_pts / take_profit_pts
  replace touch_buffer_pts / min_gap_pts / sl_points / trail_offset_pts.
- adx_min_threshold / adx_period / session_edge_no_trade_minutes added for the
  MA50/MA100 bounce + ADX filter strategy.

No value tuning vs the prior defaults — only field renames + additions.
"""

import os
from dataclasses import dataclass


def _env_str(key: str, default: str) -> str:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return float(raw)


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no")


def _env_opt_float(key: str) -> float | None:
    """Like _env_float but returns None when unset — for genuinely optional knobs
    (e.g. allocation) where 'unset' has distinct meaning from any numeric default."""
    raw = os.getenv(key)
    if raw is None or raw == "":
        return None
    return float(raw)


@dataclass(frozen=True)
class RiskParams:
    # Kill switch thresholds (per SeanBot pattern 7)
    kill_poll_interval_sec: int = 30
    max_daily_dd_pct: float = 0.08  # 8% — catastrophic
    max_weekly_dd_pct: float = 0.15  # 15% — catastrophic
    max_consecutive_losses: int = 6  # bug detector
    # Kill-switch master enable (safety circuit breaker — can only STOP trading).
    kill_switch_enabled: bool = True

    # PR A — tiered kill switch (all env-tunable, no code change to retune).
    # Default = KEEP TRADING. A 6–9 consecutive-loss streak NOTIFIES once (no
    # pause); only >=10 consecutive losses or a realized drawdown >= 33% of the
    # configured allocation (since the epoch) PAUSES (raises the halt + flattens).
    #   KILL_SWITCH_WARN_CONSEC_LOSSES — notify-only streak threshold.
    #   KILL_SWITCH_HALT_CONSEC_LOSSES — hard-halt streak threshold.
    #   KILL_SWITCH_ALLOCATION_USD     — None/unset → % drawdown brake INERT (loud
    #                                    startup warning); set to make it active.
    #   KILL_SWITCH_MAX_DRAWDOWN_PCT   — % of allocation (whole number, e.g. 33).
    #   KILL_SWITCH_PNL_EPOCH          — ISO ts; drawdown measured from here.
    #                                    Empty → deploy time (pre-deploy losses
    #                                    don't count). See [[kill-switch-retune]].
    #   KILL_SWITCH_MAX_CONSEC_EVAL_ERRORS — how many CONSECUTIVE transient/network
    #                                    evaluator faults (Supabase/httpx read or
    #                                    connect timeouts) to tolerate before the
    #                                    fail-safe halt fires. Default 3 — a single
    #                                    Supabase ReadTimeout no longer spuriously
    #                                    halts a healthy, flat bot. A NON-transient
    #                                    (logic) error still halts on the FIRST hit.
    kill_switch_warn_consec_losses: int = 6
    kill_switch_halt_consec_losses: int = 10
    kill_switch_allocation_usd: float | None = None
    kill_switch_max_drawdown_pct: float = 33.0
    kill_switch_pnl_epoch: str = ""
    kill_switch_max_consec_eval_errors: int = 3
    # Equity base for the daily/weekly drawdown % triggers. None → use the live
    # broker NetLiquidation each poll. NOTE: on the ~$1M paper account, 8%/15% of
    # net-liq is ~$80k/$150k, so the DD triggers are very loose for a 2-contract
    # MNQ position — the consecutive-loss trigger is the effective brake. Set this
    # to an allocated capital (e.g. 50_000) to make the DD triggers meaningful.
    kill_switch_equity_base_usd: float | None = None

    # Position limits (per kickoff §3 + Ohad's plan §4.2)
    max_simultaneous_positions: int = 5
    max_contracts_per_trade: int = 2
    contracts_per_trade: int = 2  # default standard sizing

    # STABILIZE-4 — foreign-position auto-flatten guard. A broker position that does
    # not reconcile to tracked INTENT (its non-CLOSED lifecycles) is flattened at
    # market once it is PERSISTENTLY foreign — confirmed across this many consecutive
    # full-scan ticks (the debounce that prevents liquidating a just-opened position
    # during the open→track race). Direction-agnostic: keys off intent, never
    # "long-only". `enabled=False` disables auto-liquidation (still halts + alerts).
    foreign_flatten_enabled: bool = True
    foreign_flatten_confirm_ticks: int = 2

    # Strategy parameters (per kickoff §3 — MA50/MA100 bounce, no ADX after PR #33)
    ma_fast: int = 50
    ma_slow: int = 100
    ma_touch_buffer_pts: float = 5.0
    ma_min_gap_pts: float = 0.5  # SeanBot V3 config/settings.py:44 (PR #33)
    # Candle-confirmation tolerance for the LONG bullish gate. bullish_ok :=
    # close >= open - ma_bullish_tolerance_pts; 0.0 == SeanBot's strict close>=open.
    # PR-1 (entry-gate parity): default reset 2.0 -> 0.0 to match SeanBot's CURRENT
    # live Python (`close > open`, ma_bounce.py check_signal). Env-tunable via
    # BULLISH_TOLERANCE. CONFLICT FLAG: TF's older W-S14.2 calibration (against
    # captured SeanBot Telegram entries) found strict close>open matched only 2/12
    # of those fires vs 8/12 at 2.0pt — i.e. SeanBot was THEN more permissive. The
    # operator's parity target now says strict; if SeanBot's live code has not
    # tightened since W-S14.2 this may REDUCE live parity — set BULLISH_TOLERANCE=2.0
    # to revert. (Entry-filter only; exit/SL/TP/kill-switch unchanged.)
    ma_bullish_tolerance_pts: float = 0.0
    stop_loss_pts: float = 75.0
    take_profit_pts: float = 150.0
    cooldown_bars: int = 10

    # PR B — exit mode for the take-profit leg of the native OCA entry bracket.
    # "fixed" (default): LMT take-profit @ entry+take_profit_pts, a valid native
    # OCA bracket child alongside the fixed STP — this delivers PR B's core win
    # (the protective STP is now a native bracket child, so it SURVIVES a client
    # disconnect/redeploy, the root-cause fix for the naked-stop incident).
    # "trailing" is NOT currently usable: IBKR rejects a TRAIL order as a bracket
    # child of a MKT parent (Error 328 — "Trailing stop orders can be attached to
    # limit or stop-limit orders only"; observed live 2026-06-02 02:21Z). A native
    # trailing TP needs a standalone post-fill placement redesign (queued).
    # The fixed protective STP @ entry-stop_loss_pts is ALWAYS present and NEVER
    # trails, in either mode. Env: EXIT_MODE, TRAIL_OFFSET.
    exit_mode: str = "fixed"
    trail_offset_pts: float = 150.0
    # SeanBot V3/V12 bot-ratcheted exit ladder (EXIT_MODE=trailing). On each 1-min
    # bar close the bot walks a single resting GTC SELL STP UP only (never down):
    #   base            : entry - stop_loss_pts (75)
    #   peak >= lock_in : max(prev, entry + lock_in_pts)  — V12 +50 lock-in
    #   peak >= trail   : max(prev, highest - trail_offset_pts) — trail tail
    #   close >= entry + hard_ceiling_pts → market-exit (V3 hard cap).
    # Env: LOCK_IN_PTS, HARD_CEILING_PTS (stop_loss_pts/trail_offset_pts reused).
    lock_in_pts: float = 50.0
    hard_ceiling_pts: float = 1000.0

    # REPLICATE — SeanBot-triggered, validity-checked second entry path. A SeanBot
    # LONG MNQ notification triggers a TF entry IFF (at TF's action time) the
    # current price is still near the MA AND not a stale chase vs SB's signal
    # price. Bounds derived from signal_reconciliations + 1-min bars (2026-06-03):
    # the SB entry band (SB_price - sma100) spanned -12.9..+30.7 (median +1.3); the
    # +1min price drift vs SB's signal had p90 ~+28. This path catches the near-MA
    # touches TF's own once-per-closed-bar gate structurally misses (it took only
    # ~9% of SB's entries; 71% missed on the touch gate). FLAT/no-stack + the halt
    # are enforced downstream by the existing _handle_trade_signal/create_lifecycle
    # path — this path NEVER stacks and NEVER double-enters an own-gate setup.
    # Env: SB_TRIGGER_ENABLED, SB_NEAR_MA_BELOW_PTS, SB_NEAR_MA_ABOVE_PTS,
    # SB_NO_CHASE_MAX_PTS, SB_TRIGGER_MAX_BAR_AGE_SEC.
    sb_trigger_enabled: bool = True
    sb_near_ma_below_pts: float = 15.0  # accept price >= sma100 - 15 (matches touch lower band)
    sb_near_ma_above_pts: float = 35.0  # accept price <= sma100 + 35 (covers SB band max +30.7)
    sb_no_chase_max_pts: float = 25.0  # reject price > SB_signal + 25 (stale chase)
    sb_trigger_max_bar_age_sec: float = 180.0  # require a settled bar this fresh (current truth)

    # PR C — max tolerated gap (in missing bars) in the live feed after a
    # reconnect/resubscribe before the strategy buffer is invalidated and
    # re-seeded from history. The SMA must never span a gap. Default 1 tolerates a
    # single skipped thin bar; a larger gap (≥2 missing) triggers a re-seed.
    # Env: BAR_GAP_MAX_TOLERANCE_BARS.
    bar_gap_max_tolerance_bars: int = 1

    # Session-edge buffer applied to every transition (Sunday open, CME daily
    # break boundaries, Friday weekend cutoff). Minutes, wall-clock.
    session_edge_no_trade_minutes: int = 5

    # 24/5 CME futures session boundaries (America/New_York wall-clock).
    sunday_open_et: str = "18:00"  # weekly open
    daily_break_start_et: str = "17:00"  # CME maintenance break start (Mon–Thu)
    daily_break_end_et: str = "18:00"  # CME maintenance break end (Mon–Thu)

    # SeanBot C1 regime gate — 30-min EMA200 level filter for LONG entries.
    # When True, detect_signal blocks LONG signals if current price <= 30m EMA200.
    # Fail-open on warmup (<202 30-min bars), missing timestamps, or exception.
    # PR-1 (entry-gate parity): default reset True -> False — the operator removed
    # the regime gate from SeanBot's live check_signal, so TF EXCLUDES it for
    # parity. Code retained (env-tunable via REGIME_GATE_ENABLED) for reversibility.
    regime_gate_enabled: bool = False

    # EOD force-close — fires on ``force_close_weekday`` at ``force_close_et``.
    # Default is Friday at 16:25 ET (5 min before the weekend cutoff at 16:30 ET).
    # Positions persist overnight Mon–Thu under 24/5.
    force_close_et: str = "16:25"
    force_close_weekday: int = 4  # Friday (Mon=0)

    # Operator-imposed weekend flat cutoff (America/New_York wall-clock).
    # Independent of CME's actual Friday 17:00 ET close so we get out 30 min early.
    weekend_flat_cutoff_weekday: int = 4  # Friday (Mon=0)
    weekend_flat_cutoff_hour_et: int = 16
    weekend_flat_cutoff_minute_et: int = 30

    # IB Gateway daily restart window (no trading; may span midnight).
    gateway_restart_start_et: str = "23:45"
    gateway_restart_end_et: str = "00:15"


def _load_exit_mode() -> str:
    # Default "fixed": "trailing" is IB-rejected as a MKT-bracket child (Error 328);
    # see RiskParams.exit_mode. Kept selectable for the future standalone redesign.
    mode = _env_str("EXIT_MODE", "fixed").strip().lower()
    if mode not in ("trailing", "fixed"):
        raise RuntimeError(f"Invalid EXIT_MODE={mode!r}; must be 'trailing' or 'fixed'.")
    return mode


RISK = RiskParams(
    # PR B — env-tunable exit knobs (default = trailing TP, 150pt offset).
    exit_mode=_load_exit_mode(),
    trail_offset_pts=_env_float("TRAIL_OFFSET", 150.0),
    # SeanBot V3/V12 ratchet ladder knobs (EXIT_MODE=trailing).
    lock_in_pts=_env_float("LOCK_IN_PTS", 50.0),
    hard_ceiling_pts=_env_float("HARD_CEILING_PTS", 1000.0),
    # PR C — feed-gap tolerance (missing bars) before invalidate + re-seed.
    bar_gap_max_tolerance_bars=_env_int("BAR_GAP_MAX_TOLERANCE_BARS", 1),
    # PR A — tiered kill switch (env-tunable; default keeps trading).
    kill_switch_enabled=_env_bool("KILL_SWITCH_ENABLED", True),
    kill_switch_warn_consec_losses=_env_int("KILL_SWITCH_WARN_CONSEC_LOSSES", 6),
    kill_switch_halt_consec_losses=_env_int("KILL_SWITCH_HALT_CONSEC_LOSSES", 10),
    kill_switch_allocation_usd=_env_opt_float("KILL_SWITCH_ALLOCATION_USD"),
    kill_switch_max_drawdown_pct=_env_float("KILL_SWITCH_MAX_DRAWDOWN_PCT", 33.0),
    kill_switch_pnl_epoch=_env_str("KILL_SWITCH_PNL_EPOCH", ""),
    # Tolerate up to N consecutive transient evaluator faults before halting
    # (default 3; never treated as 0 — a 0 would halt on the first blip).
    kill_switch_max_consec_eval_errors=_env_int("KILL_SWITCH_MAX_CONSEC_EVAL_ERRORS", 3),
    # PR-1 — entry-gate parity with SeanBot live check_signal (env-tunable):
    #   BULLISH_TOLERANCE (0.0 = strict close>=open),
    #   REGIME_GATE_ENABLED (False = excluded, per operator).
    ma_bullish_tolerance_pts=_env_float("BULLISH_TOLERANCE", 0.0),
    regime_gate_enabled=_env_bool("REGIME_GATE_ENABLED", False),
    # REPLICATE — SeanBot-triggered validity-checked entry (env-tunable).
    sb_trigger_enabled=_env_bool("SB_TRIGGER_ENABLED", True),
    sb_near_ma_below_pts=_env_float("SB_NEAR_MA_BELOW_PTS", 15.0),
    sb_near_ma_above_pts=_env_float("SB_NEAR_MA_ABOVE_PTS", 35.0),
    sb_no_chase_max_pts=_env_float("SB_NO_CHASE_MAX_PTS", 25.0),
    sb_trigger_max_bar_age_sec=_env_float("SB_TRIGGER_MAX_BAR_AGE_SEC", 180.0),
)
# Lowercase alias for consumers that prefer `risk_params.x` over `RISK.x`.
risk_params = RISK
