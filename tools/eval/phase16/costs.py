"""Cost + slippage model for Phase-16 — the honesty layer.

Verified MNQ economics (config/instruments.py, NOT re-derived):
  TICK_SIZE   = 0.25 index points
  MULTIPLIER  = $2.00 / index point   ($0.50 / tick)
  COMMISSION  = $0.62 / side / contract  (Friend's IBKR tier; §0.5.97)

Two pieces:

* ``CostModel`` charges COMMISSION on both fills (entry + exit) per contract.
* ``Slippage`` adjusts each FILL PRICE adversely by a number of ticks that depends
  on the fill TYPE (the brief's "1/2-tick conditional slippage"): an aggressive fill
  (a market entry at the next-bar open, a protective-stop exit, a market exit on a
  signal flip / time stop / session close) pays a FULL tick; a passive fill (a resting
  limit target) pays HALF a tick. The engine applies this at fill time so the
  pessimistic price path is explicit, then ``CostModel`` charges only commission.

Survivor sensitivity (``uniform``) re-runs every fill at 0 / 1 / 2 ticks flat.
"""

from __future__ import annotations

from dataclasses import dataclass

TICK_SIZE = 0.25
MULTIPLIER = 2.0  # $ per index point
COMMISSION_PER_SIDE = 0.62  # USD / side / contract


@dataclass(frozen=True)
class Slippage:
    """Adverse slippage in TICKS per fill, conditional on fill type.

    Defaults encode the brief's "1/2-tick conditional" headline: aggressive fills
    (entry, stop, market-exit) = 1 tick; passive limit target = 1/2 tick.
    """

    entry_ticks: float = 1.0
    stop_ticks: float = 1.0
    target_ticks: float = 0.5
    market_exit_ticks: float = 1.0  # flip / time-stop / session-close market-outs
    tick_size: float = TICK_SIZE

    @classmethod
    def uniform(cls, ticks: float) -> Slippage:
        """Flat ``ticks`` of slippage on every fill — used for 0/1/2-tick sensitivity."""
        return cls(
            entry_ticks=ticks,
            stop_ticks=ticks,
            target_ticks=ticks,
            market_exit_ticks=ticks,
        )

    def adjust_entry(self, px: float, direction: int) -> float:
        """A market entry pays UP (long fills higher, short fills lower)."""
        return px + direction * self.entry_ticks * self.tick_size

    def adjust_exit(self, px: float, direction: int, kind: str) -> float:
        """An exit RECEIVES less (long sells lower, short buys back higher).

        ``kind`` in {"stop", "target", "market"} picks the conditional tick count.
        """
        ticks = {
            "stop": self.stop_ticks,
            "target": self.target_ticks,
            "market": self.market_exit_ticks,
        }[kind]
        # direction is the POSITION direction; an exit trades the opposite way, so an
        # adverse fill moves the received price against the position by `direction`.
        return px - direction * ticks * self.tick_size


@dataclass(frozen=True)
class CostModel:
    commission_per_side_per_ct: float = COMMISSION_PER_SIDE
    multiplier: float = MULTIPLIER
    tick_size: float = TICK_SIZE

    def commission_usd(self, contracts: int) -> float:
        """Entry + exit commission, per side, per contract."""
        return self.commission_per_side_per_ct * 2.0 * contracts

    def net_pnl_usd(self, entry_px: float, exit_px: float, direction: int, contracts: int) -> float:
        """Net $ from already-slippage-adjusted fill prices, minus commission.

        ``entry_px`` / ``exit_px`` are the prices AFTER ``Slippage`` was applied by the
        engine, so slippage is already baked into the gross points here.
        """
        gross_pts = (exit_px - entry_px) * direction
        gross_usd = gross_pts * self.multiplier * contracts
        return gross_usd - self.commission_usd(contracts)
