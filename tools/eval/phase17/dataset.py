"""Phase-17 Stage-2 dataset layer — load the Stage-1 continuous back-adjusted DAILY
CSVs, align to the COMMON trading window, and expose the SAME train/holdout split as the
rest of the campaign.

Pure pandas — no IB import, no prod-strategy import — so a candidate signal can never
drive a live path. The CSVs were written read-only in Stage-1
(``research/data/phase17/<sym>_daily.csv``); this layer only READS them.

Key Stage-2 conventions
-----------------------
* **SPLIT-ONCE boundary (committed, never tuned)** — the campaign's single calendar
  split (``tools.eval.phase16.dataset``):
      TRAIN   = .. 2025-09-01   (search here ONLY)
      HOLDOUT = 2025-09-01 ..   (touched ONCE, for the portfolio)
  The Phase-17 common window opens later (2024-05, rates-bound), so TRAIN here is
  ~2024-05..2025-08 and HOLDOUT ~2025-09..2026-06. Same boundary date as Phase-16.

* **Signal/vol warm-up uses each market's FULL per-contract history** (back to ~2023-06
  for the long markets), so a slow EWMAC is already warm at the start of TRAIN for the 8
  non-rates markets. Only 10Y (listed 2024-05) cannot warm a 256-day signal until
  mid-2025 — an honest, unavoidable limit (Stage-1 §"Honest limitations").

* **10Y sign convention (rates are priced in YIELD).** The Micro 10Y *Yield* future rises
  when the yield rises (long future profits on rising yield). The operator wants the trend
  system to read a FALLING-yield / RISING-bond trend as the correct ("long") direction, as
  a bond-price trend-follower would. We therefore build the signal/vol/P&L for 10Y on a
  NEGATED price proxy ``P = -yield``:
      yield falls (bond rallies)  ->  P = -yield RISES  ->  EWMAC(P) > 0  ->  long the trend
  Because |ΔP| = |Δyield|, the dollar magnitudes, tick value and vol are identical to
  trading the raw future; only the trend/position SIGN flips. A position>0 on ``P`` is
  economically "long the bond" (= short the yield future), and its P&L on ``P`` is the
  bond-proxy P&L. This is the only series with ``sign = -1``; proven on one example in the
  Stage-2 RESULTS.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .markets import Market, core

DATA_DIR = "/home/tradeflow/tradeflow/research/data/phase17"

# The single, pre-committed calendar split (same boundary as tools.eval.phase16.dataset).
TRAIN_END = "2025-09-01"  # exclusive — TRAIN is everything before this
HOLDOUT_START = "2025-09-01"
HOLDOUT_END = "2026-07-01"  # exclusive (data ends 2026-06-11)

# Sign convention per symbol: +1 normal, -1 for a yield-priced series (see module docstring).
SERIES_SIGN: dict[str, int] = {"10Y": -1}


@dataclass
class MarketSeries:
    market: Market
    df: pd.DataFrame  # full per-market series, columns: time, close, price (=sign*close)
    sign: int


def _load_one(sym: str) -> pd.DataFrame:
    path = f"{DATA_DIR}/{sym}_daily.csv"
    df = pd.read_csv(path, parse_dates=["time"])
    if df["time"].dt.tz is None:
        df["time"] = df["time"].dt.tz_localize("UTC")
    else:
        df["time"] = df["time"].dt.tz_convert("UTC")
    df["time"] = df["time"].dt.normalize()
    df = df.dropna(subset=["close"]).drop_duplicates("time", keep="last")
    return df.sort_values("time").reset_index(drop=True)


def load_series(markets: list[Market] | None = None) -> dict[str, MarketSeries]:
    """Load each market's full continuous daily series and attach the signed ``price``
    column (``sign * close``) the signal/vol/P&L are all computed on."""
    markets = markets or core()
    out: dict[str, MarketSeries] = {}
    for m in markets:
        df = _load_one(m.symbol)
        sign = SERIES_SIGN.get(m.symbol, 1)
        df["price"] = sign * df["close"].astype(float)
        out[m.symbol] = MarketSeries(market=m, df=df[["time", "close", "price"]].copy(), sign=sign)
    return out


@dataclass
class Panel:
    """A market panel aligned to the common trading window, with the split accessors.

    ``price`` is a wide frame (index=date, columns=symbol) of the SIGNED price series
    restricted to dates where EVERY market has a bar (the common window). Per-market
    signal/vol frames are computed on the FULL series upstream and then re-indexed to this
    common index, so warm-up uses pre-window history.
    """

    series: dict[str, MarketSeries]
    common_index: pd.DatetimeIndex
    price: pd.DataFrame  # common-window signed price, columns=symbol

    @property
    def symbols(self) -> list[str]:
        return list(self.price.columns)

    def split_mask(self, which: str) -> pd.Series:
        idx = self.common_index
        if which == "train":
            return idx < pd.Timestamp(TRAIN_END, tz="UTC")
        if which == "holdout":
            return (idx >= pd.Timestamp(HOLDOUT_START, tz="UTC")) & (
                idx < pd.Timestamp(HOLDOUT_END, tz="UTC")
            )
        raise ValueError(which)


def build_panel(markets: list[Market] | None = None) -> Panel:
    """Load all markets, intersect their dates into the common window, return a Panel."""
    series = load_series(markets)
    price_cols = {sym: ms.df.set_index("time")["price"] for sym, ms in series.items()}
    wide = pd.DataFrame(price_cols).sort_index()
    common = wide.dropna(how="any")  # only dates where EVERY market trades
    return Panel(series=series, common_index=common.index, price=common)
