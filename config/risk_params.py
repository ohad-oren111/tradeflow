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

    # Strategy parameters (per kickoff §3 — MA50/MA100 bounce + ADX filter)
    ma_fast: int = 50
    ma_slow: int = 100
    ma_touch_buffer_pts: float = 5.0
    ma_min_gap_pts: float = 2.0
    stop_loss_pts: float = 75.0
    take_profit_pts: float = 150.0
    cooldown_bars: int = 10

    # ADX trend-strength filter
    adx_period: int = 14
    adx_min_threshold: float = 20.0

    # Session-edge no-trade window (minutes from RTH open and before RTH close)
    session_edge_no_trade_minutes: int = 5

    # Trading hours (America/New_York)
    market_open_et: str = "09:30"
    signal_scan_start_et: str = "09:35"  # 5min after open for SMA warmup
    signal_scan_end_et: str = "15:50"  # 10min before close
    force_close_et: str = "15:58"  # 2min before market close
    market_close_et: str = "16:00"

    # Weekend cutoff (per SeanBot pattern 8 — UTC, not ET)
    weekend_flat_cutoff_weekday: int = 4  # Friday (Mon=0)
    weekend_flat_cutoff_hour_utc: int = 20
    weekend_flat_cutoff_minute_utc: int = 30

    # Gateway restart window (no trading)
    gateway_restart_start_et: str = "23:45"
    gateway_restart_end_et: str = "00:15"


RISK = RiskParams()
# Lowercase alias for consumers that prefer `risk_params.x` over `RISK.x`.
risk_params = RISK
