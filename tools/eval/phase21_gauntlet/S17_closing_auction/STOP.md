# S17 — Closing-auction imbalance / reversion — UNTESTABLE-HERE

**Verdict: UNTESTABLE-HERE (feed gate). Not run.**

The signal IS the proprietary data: NYSE/Nasdaq closing-auction imbalance feeds (published
~15:50–16:00 ET) showing buy/sell imbalance size per symbol. There is no free historical
archive of imbalance messages — vendors sell exactly this. Without the imbalance there is
no entry signal to backtest; daily bars contain the auction PRINT but not the imbalance
that preceded it.

**Unlock:** a historical imbalance feed (NYSE OpenBook/Imbalance, Nasdaq NOII via a vendor
like databento/Polygon). Also needs intraday prices for the 15:50→close window.
