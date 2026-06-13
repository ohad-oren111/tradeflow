"""Phase 0 config tests."""

from config.instruments import MNQ
from config.risk_params import RISK


def test_mnq_spec_matches_architecture_brief_v3() -> None:
    """§3 of v0 architecture brief — verified vs CME + SeanBot. Do not edit without re-probing."""
    assert MNQ.symbol == "MNQ"
    assert MNQ.exchange == "CME"
    assert MNQ.tick_size == 0.25
    assert MNQ.multiplier == 2.0
    assert MNQ.tick_value_usd == 0.50
    # W-S14.2 Track 4b: per-side is the source of truth; RT = 2x (entry + exit).
    assert MNQ.commission_per_side_usd == 0.62
    assert MNQ.commission_rt_usd == 1.24
    assert MNQ.margin_intraday_usd == 2000.0
    assert MNQ.quarterly_months == (3, 6, 9, 12)
    assert MNQ.roll_days_before_expiry == 8


def test_risk_params_match_kickoff_section_3() -> None:
    """Kickoff §3 + PR #33 SeanBot V3 realignment — MA100 pullback strategy."""
    assert RISK.ma_fast == 50
    assert RISK.ma_slow == 100
    assert RISK.ma_touch_buffer_pts == 5.0
    assert RISK.ma_min_gap_pts == 0.5  # PR #33: lowered 2.0 -> 0.5 per SeanBot V3
    assert RISK.stop_loss_pts == 75.0
    assert RISK.take_profit_pts == 150.0
    assert RISK.cooldown_bars == 10
    assert RISK.contracts_per_trade == 2
    assert RISK.max_simultaneous_positions == 5
    assert RISK.session_edge_no_trade_minutes == 5


def test_kill_switch_thresholds() -> None:
    """SeanBot pattern 7 defaults."""
    assert RISK.max_daily_dd_pct == 0.08
    assert RISK.max_weekly_dd_pct == 0.15
    assert RISK.max_consecutive_losses == 6
    assert RISK.kill_poll_interval_sec == 30


def test_risk_per_trade_dollar_math() -> None:
    """75pt SL × $0.50/tick × 4 ticks/pt × 2 contracts = $300 max loss per trade.
    Verifies the tick math against §3 of v0 architecture brief."""
    ticks_per_point = 1.0 / MNQ.tick_size  # 4 ticks/pt
    max_loss_per_contract = RISK.stop_loss_pts * ticks_per_point * MNQ.tick_value_usd
    max_loss_per_trade = max_loss_per_contract * RISK.contracts_per_trade
    assert max_loss_per_trade == 300.0
