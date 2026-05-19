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
    commission_rt_usd: float = 0.62  # Friend's IBKR tier; verify yours independently
    margin_intraday_usd: float = 2000.0  # SeanBot config/settings.py:34
    margin_cme_maintenance_usd: float = 3636.0  # ChatGPT R2 verified
    quarterly_months: tuple = (3, 6, 9, 12)
    roll_days_before_expiry: int = 8  # per SeanBot data/feed.py:106


MNQ = MNQSpec()
