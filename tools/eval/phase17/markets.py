"""The Phase-17 basket — confirmed against the IB gateway, NOT from memory (§0.5.97).

Each spec below was RESOLVED on 2026-06-11 via a read-only ``reqContractDetails`` +
``reqHistoricalData`` probe against the paper gateway (clientId 117). The
``multiplier`` / ``min_tick`` are the values IBKR returned for the FRONT contract; the
``tick_value_usd`` is the derived ``multiplier * min_tick``. Commission per side is NOT
fixed here — IBKR micro commissions vary by product and tier; Stage 2 must specify each
from the account's real fee schedule (flagged ``to-verify``).

Buckets are the diversification axis (the breadth thesis): the edge — if any — is that a
ONE-signal trend overlay across LOW-correlation buckets nets positive even when each
single market is marginal. The Stage-2 correlation matrix must PROVE the buckets are
actually uncorrelated, or the thesis is void.

PROBE FINDINGS (2026-06-11):
  * Daily history present for every market below (paper account has the data permission).
  * ``includeExpired=True`` returns contract definitions back only to ~2024-06, so a
    rigorous per-contract Panama stitch spans ~24 months (2024-06 .. 2026-06). This
    happens to match the natural common portfolio window — the rates micros (10Y/2YY/30Y)
    only LISTED in 2024-05, so the diversified basket is window-bound to ~24mo regardless.
  * NOT found as micros: SIL (no micro silver), MNG (no micro NatGas on NYMEX) — dropped.
  * Rates micros are priced in YIELD (last ~4.47 for 10Y), not bond price — a trend on a
    yield series is economically valid but INVERTED vs a bond-price trend. Flagged.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Market:
    symbol: str
    exchange: str
    bucket: str
    multiplier: float  # $ per 1.0 index/price point (IBKR contract multiplier)
    min_tick: float  # IBKR minTick for the front contract
    note: str
    optional: bool = False  # included by default unless a correlation/quality reason drops it

    @property
    def tick_value_usd(self) -> float:
        return self.multiplier * self.min_tick


# Core basket (default ON). Specs verified via the 2026-06-11 probe.
BASKET: list[Market] = [
    Market(
        "MES", "CME", "equity", 5.0, 0.25, "Micro E-mini S&P 500 (one equity only — MES~M2K~MNQ)"
    ),
    Market("MGC", "COMEX", "metals", 10.0, 0.1, "Micro Gold"),
    Market("MHG", "COMEX", "metals", 2500.0, 0.0005, "Micro Copper"),
    Market("MCL", "NYMEX", "energy", 100.0, 0.01, "Micro WTI Crude"),
    Market("M6E", "CME", "fx", 12500.0, 0.0001, "Micro EUR/USD"),
    Market("M6A", "CME", "fx", 10000.0, 0.0001, "Micro AUD/USD"),
    Market("M6B", "CME", "fx", 6250.0, 0.0001, "Micro GBP/USD"),
    Market("MBT", "CME", "crypto", 0.1, 5.0, "Micro Bitcoin"),
    Market(
        "10Y",
        "CBOT",
        "rates",
        1000.0,
        0.001,
        "Micro 10Y Yield — PRICED IN YIELD (inverted vs bond price); listed 2024-05",
    ),
    # Optional — correlated with a core member; included only if the operator wants the
    # extra bucket depth (Stage-2 correlation matrix is the arbiter).
    Market("MET", "CME", "crypto", 0.1, 0.5, "Micro Ether (corr w/ MBT ~0.8)", optional=True),
    Market("M2K", "CME", "equity", 5.0, 0.1, "Micro Russell 2000 (corr w/ MES)", optional=True),
]


def core() -> list[Market]:
    return [m for m in BASKET if not m.optional]


def by_symbol(sym: str) -> Market:
    for m in BASKET:
        if m.symbol == sym:
            return m
    raise KeyError(sym)
