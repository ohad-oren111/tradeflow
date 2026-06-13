"""Tests for comms.alert_format (SeanBot-style alerts) + telegram _pretty_alert."""

from __future__ import annotations

from comms.alert_format import (
    daily_summary_fields,
    format_daily_summary,
    format_entry,
    format_exit,
    format_stop_moved,
)
from comms.telegram import _pretty_alert


def test_format_entry():
    out = format_entry(
        direction="LONG", entry=30000.0, stop=29925.0, contracts=2, target_info="30150.00"
    )
    assert out.startswith("🟢 ENTRY — MNQ")
    assert "Long @ 30,000.00" in out
    assert "🛑 Stop: 29,925.00 (−75 pt)" in out  # entry-stop = 75
    assert "🎯 Target/Trail: 30150.00" in out
    assert "Bot size: 2 contracts" in out


def test_format_stop_moved_lock_in():
    # entry 30000, stop raised to entry+50 → protecting +50 pt (~$200 on 2 ct).
    out = format_stop_moved(entry=30000.0, old_stop=29925.0, new_stop=30050.0, contracts=2)
    assert out.startswith("🔒 STOP MOVED — MNQ (long @ 30,000.00)")
    assert "Stop raised: 29,925.00 → 30,050.00" in out
    assert "Now protecting +50 pt (~$+200.00 on 2 ct)" in out


def test_format_exit_win():
    out = format_exit(exit_price=30150.0, points=150.0, reason="trail stop", pnl_usd=596.0)
    assert out.startswith("💰 EXIT (profit) — MNQ")
    assert "Closed @ 30,150.00 · +150 pt" in out
    assert "Reason: trail stop" in out
    assert "P&L (2 ct): $+596.00" in out


def test_format_exit_loss():
    out = format_exit(exit_price=29925.0, points=-75.0, reason="stop loss", pnl_usd=-303.48)
    assert out.startswith("🔴 EXIT (loss) — MNQ")
    assert "· −75 pt" in out
    assert "P&L (2 ct): $−303.48" in out


def test_daily_summary_fields_math():
    wins, losses, net = daily_summary_fields([553.28, -303.48, -2.48, 0.0, -437.72])
    assert wins == 1  # only the +553.28
    assert losses == 3  # three negatives; the 0.0 is neither
    assert net == round(553.28 - 303.48 - 2.48 - 437.72, 2)


def test_format_daily_summary_net_loss():
    out = format_daily_summary(day="2026-06-02", wins=1, losses=3, net=-190.40)
    assert "📊 TradeFlow — Daily P&L 2026-06-02" in out
    assert "Winning trades: 1" in out
    assert "Losing trades: 3" in out
    assert "Daily Net P&L: 🔴 -$190.40" in out


def test_format_daily_summary_net_profit():
    out = format_daily_summary(day="2026-06-03", wins=2, losses=0, net=412.10)
    assert "Daily Net P&L: 🟢 $412.10" in out


# ------------------------------------------- telegram _pretty_alert dispatch


def test_pretty_alert_entry():
    body = (
        "entry_placed: symbol=MNQM6 direction=LONG qty=2 entry=30000.00 "
        "target=30150.00 stop=29925.00 lifecycle_id=abc"
    )
    out = _pretty_alert(body)
    assert out is not None and out.startswith("🟢 ENTRY — MNQ")
    assert "Long @ 30,000.00" in out


def test_pretty_alert_stop_moved():
    body = (
        "trailing_stop_ratcheted: symbol=MNQM6 stop_id=85 old=29925.00 new=30050.00 "
        "highest=30060.00 entry=30000.00 lifecycle_id=abc"
    )
    out = _pretty_alert(body)
    assert out is not None and out.startswith("🔒 STOP MOVED")
    assert "Now protecting +50 pt" in out


def test_pretty_alert_exit_win_and_loss():
    win = _pretty_alert(
        "exit_filled: symbol=MNQM6 qty=2 entry=30000.00 exit_price=30150.00 "
        "pnl_net=596.00 exit_reason=TARGET lifecycle_id=a"
    )
    assert win is not None and win.startswith("💰 EXIT (profit)")
    assert "· +150 pt" in win
    loss = _pretty_alert(
        "exit_filled: symbol=MNQM6 qty=2 entry=30000.00 exit_price=29925.00 "
        "pnl_net=-303.48 exit_reason=STOP lifecycle_id=a"
    )
    assert loss is not None and loss.startswith("🔴 EXIT (loss)")


def test_pretty_alert_daily_summary():
    out = _pretty_alert("daily_summary: day=2026-06-02 wins=1 losses=3 net=-190.40")
    assert out is not None and out.startswith("📊 TradeFlow — Daily P&L 2026-06-02")


def test_pretty_alert_unknown_returns_none():
    # Non-trade events fall back to the plain one-liner (handler adds the prefix).
    assert _pretty_alert("hourly_session_digest: window=...") is None
    assert _pretty_alert("kill_switch_tripped: reason=evaluator_error") is None
