"""Per-market cost model for the Phase-17 micro basket — the honesty layer.

Costs are charged on TURNOVER: when the vol-targeted position changes by ``d`` contracts
between two daily closes, ``|d|`` contracts trade ONE side, paying

    cost = |d| * (commission_per_side + slip_ticks * tick_value)

So entering and later unwinding each pay once, captured by the daily |Δposition|. All
prices are close-to-close (Carver), so there is no separate "stop slippage" — the model
is a single per-contract friction.

COMMISSIONS ARE ``to-verify`` (§0.5.97). IBKR micro-future commissions vary by product and
tier; the account's real fee schedule must replace these before any forward decision. The
defaults below are deliberately CONSERVATIVE all-in per-side estimates (commission +
exchange + regulatory), higher than the bare $0.25 IBKR commission, and crypto (MBT) is
set higher as micro-crypto carries a richer fee. The headline verdict additionally reports
a 0/1/2-tick slippage sensitivity so the reader sees the cost dependence directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from .markets import Market

# All-in commission per side per contract (USD), TO-VERIFY against the account schedule.
# Conservative: > IBKR's bare $0.25 micro commission, to cover exchange + NFA/reg fees.
_COMMISSION_PER_SIDE: dict[str, float] = {
    "MES": 0.52,
    "MGC": 0.52,
    "MHG": 0.52,
    "MCL": 0.52,
    "M6E": 0.52,
    "M6A": 0.52,
    "M6B": 0.52,
    "10Y": 0.52,
    "MBT": 2.50,  # micro bitcoin — richer fee
}
_DEFAULT_COMMISSION = 0.52


@dataclass(frozen=True)
class CostModel:
    """Per-contract, per-side cost = commission + ``slip_ticks`` * tick_value."""

    slip_ticks: float = 1.0  # adverse ticks per side at the daily-close rebalance

    def commission_per_side(self, m: Market) -> float:
        return _COMMISSION_PER_SIDE.get(m.symbol, _DEFAULT_COMMISSION)

    def per_contract_side(self, m: Market) -> float:
        """Total $ to trade one contract one side: commission + slippage."""
        return self.commission_per_side(m) + self.slip_ticks * m.tick_value_usd

    def with_slip(self, slip_ticks: float) -> CostModel:
        return CostModel(slip_ticks=slip_ticks)
