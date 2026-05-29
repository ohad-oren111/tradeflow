"""MNQ contract spec. Verified against CME and SeanBot config/settings.py:32-35.
DO NOT modify without re-probing the source. See §0.5.97."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MNQSpec:
    symbol: str = "MNQ"
    exchange: str = "CME"
    currency: str = "USD"
    sec_type: str = "FUT"
    tick_size: float = 0.25  # CME / SeanBot config/settings.py:32
    multiplier: float = 2.0  # CME / SeanBot config/settings.py:33
    tick_value_usd: float = 0.50  # derived: multiplier * tick_size
    # IBKR bills commission per contract on EACH side (entry AND exit).
    # W-S14.2 Track 4b: this was previously a single `commission_rt_usd = 0.62`
    # field, but the close paths charge `qty * commission_rt_usd` — one side only
    # — so the bot undercharged ~$1.24 on a 2-ct round trip ($600.76 net vs the
    # broker's $599.52). The per-side rate is now the source of truth and the
    # round-trip cost is derived (entry + exit = 2x per side). Existing call
    # sites read `MNQ.commission_rt_usd` unchanged — they now get the true RT.
    commission_per_side_usd: float = 0.62  # Friend's IBKR tier; verify yours independently
    margin_intraday_usd: float = 2000.0  # SeanBot config/settings.py:34
    margin_cme_maintenance_usd: float = 3636.0  # ChatGPT R2 verified
    quarterly_months: tuple = (3, 6, 9, 12)
    roll_days_before_expiry: int = 8  # per SeanBot data/feed.py:106

    @property
    def commission_rt_usd(self) -> float:
        """Round-trip commission per contract = per-side rate charged on both
        entry and exit. Callers multiply by qty for the lifecycle's total."""
        return 2.0 * self.commission_per_side_usd


MNQ = MNQSpec()
