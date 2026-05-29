"""Risk parameters. Defaults from SeanBot + kickoff §3.

Field names aligned with the SeanBot signal-detection reference in PR #10:
- ma_touch_buffer_pts / ma_min_gap_pts / stop_loss_pts / take_profit_pts
  replace touch_buffer_pts / min_gap_pts / sl_points / trail_offset_pts.
- adx_min_threshold / adx_period / session_edge_no_trade_minutes added for the
  MA50/MA100 bounce + ADX filter strategy.

No value tuning vs the prior defaults — only field renames + additions.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskParams:
    # Kill switch thresholds (per SeanBot pattern 7)
    kill_poll_interval_sec: int = 30
    max_daily_dd_pct: float = 0.08  # 8% — catastrophic
    max_weekly_dd_pct: float = 0.15  # 15% — catastrophic
    max_consecutive_losses: int = 6  # bug detector

    # Position limits (per kickoff §3 + Ohad's plan §4.2)
    max_simultaneous_positions: int = 5
    max_contracts_per_trade: int = 2
    contracts_per_trade: int = 2  # default standard sizing

    # Strategy parameters (per kickoff §3 — MA50/MA100 bounce, no ADX after PR #33)
    ma_fast: int = 50
    ma_slow: int = 100
    ma_touch_buffer_pts: float = 5.0
    ma_min_gap_pts: float = 0.5  # SeanBot V3 config/settings.py:44 (PR #33)
    # Candle-confirmation tolerance for the LONG bullish gate (W-S14.2 Track 2,
    # operator-approved D-1). bullish_ok := close >= open - ma_bullish_tolerance_pts.
    # 0.0 reproduces the prior strict close>=open; the A-S14.1 calibration backtest
    # showed the strict gate rejected 7/12 captured SeanBot entries on flat/near-doji
    # 1-min touch bars. 2.0pt recovered 8/12 (from 2/12) at +1 realistic trade/day.
    # Entry-filter threshold only — regime/stop/SL/TP/kill-switch are unchanged.
    ma_bullish_tolerance_pts: float = 2.0
    stop_loss_pts: float = 75.0
    take_profit_pts: float = 150.0
    cooldown_bars: int = 10

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
    regime_gate_enabled: bool = True

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


RISK = RiskParams()
# Lowercase alias for consumers that prefer `risk_params.x` over `RISK.x`.
risk_params = RISK
