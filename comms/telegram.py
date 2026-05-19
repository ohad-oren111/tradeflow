"""Telegram alert stubs. Phase 5 PR 13 wires the actual API."""

import logging

logger = logging.getLogger(__name__)


def alert_trade_open(
    symbol: str, entry: float, sl: float, contracts: int, max_risk_usd: float
) -> None:
    """Emoji 🟢 TRADE OPEN. Logs to stdout for now; Phase 5 will send to Telegram."""
    msg = (
        f"🟢 TRADE OPEN\n"
        f"Symbol: {symbol}\n"
        f"Entry: {entry:.2f}\n"
        f"SL: {sl:.2f}\n"
        f"Contracts: {contracts}\n"
        f"Max risk: ${max_risk_usd:.2f}"
    )
    logger.info("[telegram-stub] %s", msg.replace("\n", " | "))


def alert_trade_close(exit_price: float, pnl_usd: float, reason: str, daily_pnl_usd: float) -> None:
    """Emoji 🔴 TRADE CLOSED."""
    msg = (
        f"🔴 TRADE CLOSED\n"
        f"Exit: {exit_price:.2f}\n"
        f"P&L: {pnl_usd:+.2f}\n"
        f"Reason: {reason}\n"
        f"Daily P&L: {daily_pnl_usd:+.2f}"
    )
    logger.info("[telegram-stub] %s", msg.replace("\n", " | "))


def alert_kill_switch(guard: str, equity_usd: float, notes: str = "") -> None:
    """Emoji ⛔ KILL SWITCH ACTIVATED."""
    msg = (
        f"⛔ KILL SWITCH ACTIVATED\n"
        f"Guard: {guard}\n"
        f"Equity: ${equity_usd:.2f}\n"
        f"Trading halted."
    )
    if notes:
        msg += f"\n{notes}"
    logger.warning("[telegram-stub] %s", msg.replace("\n", " | "))
