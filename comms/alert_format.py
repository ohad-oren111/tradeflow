"""SeanBot-style Telegram alert formatters (Part 3 — pure, no IO).

Render TradeFlow's own ENTRY / STOP-MOVED / EXIT / DAILY-SUMMARY alerts in the
clean titled+emoji style the operator wants. Pure functions: the Telegram handler
parses the existing ``[ALERT] <event>: <k=v>`` log line and calls these with
explicit args, so emit sites stay simple and these stay unit-testable.

MNQ: $2/pt per contract → on 2 contracts a point is worth $4 (so +50 pt ≈ $200).
"""

from __future__ import annotations

USD_PER_PT_PER_CONTRACT = 2.0  # MNQ multiplier (config.instruments.MNQ.multiplier)


def _usd_for_points(points: float, contracts: int) -> float:
    return points * USD_PER_PT_PER_CONTRACT * contracts


def _signed_pt(points: float) -> str:
    """'+50' / '-75' with a unicode minus to match SeanBot's style."""
    return f"+{points:g}" if points >= 0 else f"−{abs(points):g}"


def _signed_usd(usd: float) -> str:
    return f"$+{usd:,.2f}" if usd >= 0 else f"$−{abs(usd):,.2f}"


def format_entry(
    *, direction: str, entry: float, stop: float, contracts: int, target_info: str
) -> str:
    """🟢 ENTRY — MNQ."""
    side = "Long" if direction.upper() == "LONG" else "Short"
    stop_pt = abs(entry - stop)
    return (
        "🟢 ENTRY — MNQ\n"
        f"{side} @ {entry:,.2f}\n"
        f"🛑 Stop: {stop:,.2f} ({_signed_pt(-stop_pt)} pt)\n"
        f"🎯 Target/Trail: {target_info}\n"
        f"Bot size: {contracts} contracts"
    )


def format_stop_moved(*, entry: float, old_stop: float, new_stop: float, contracts: int) -> str:
    """🔒 STOP MOVED — fires on each ratchet advance (lock-in, trail)."""
    protect_pt = new_stop - entry
    usd = _usd_for_points(protect_pt, contracts)
    return (
        "🔒 STOP MOVED — MNQ (long @ "
        f"{entry:,.2f})\n"
        f"Stop raised: {old_stop:,.2f} → {new_stop:,.2f}\n"
        f"Now protecting {_signed_pt(protect_pt)} pt (~{_signed_usd(usd)} on {contracts} ct)"
    )


def format_exit(*, exit_price: float, points: float, reason: str, pnl_usd: float) -> str:
    """💰/🔴 EXIT — win vs loss branch keyed on realized P&L."""
    win = pnl_usd >= 0
    title = "💰 EXIT (profit)" if win else "🔴 EXIT (loss)"
    return (
        f"{title} — MNQ\n"
        f"Closed @ {exit_price:,.2f} · {_signed_pt(points)} pt\n"
        f"Reason: {reason}\n"
        f"P&L ({_pnl_contracts(reason)} ct): {_signed_usd(pnl_usd)}"
    )


def _pnl_contracts(_reason: str) -> int:
    # P&L line always reports the bot's standard 2-contract size (matches SeanBot).
    return 2


def daily_summary_fields(pnls: list[float]) -> tuple[int, int, float]:
    """(wins, losses, net) from a day's lifecycle pnl_net values. Pure helper."""
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    return wins, losses, round(sum(pnls), 2)


def format_daily_summary(*, day: str, wins: int, losses: int, net: float) -> str:
    """📊 TradeFlow — Daily P&L. Wins/losses/net from lifecycles pnl_net."""
    emoji = "🟢" if net >= 0 else "🔴"
    net_str = f"${net:,.2f}" if net >= 0 else f"-${abs(net):,.2f}"
    return (
        f"📊 TradeFlow — Daily P&L {day}\n"
        f"Winning trades: {wins}\n"
        f"Losing trades: {losses}\n"
        f"Daily Net P&L: {emoji} {net_str}"
    )
