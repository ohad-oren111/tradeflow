"""Phase-16 — WIDE STRATEGY DISCOVERY (book-grounded, anti-overfit).

A research-only campaign (CLAUDE.md §0.5.208 MEASURE lane; never drives a prod
path) that searches a wide library of book-grounded candidate strategy families on
the saved 27-month MNQ/MES 1-min tape under a HARD anti-overfit protocol:

  1. SPLIT ONCE — train 2024-03..2025-08, holdout 2025-09..2026-06. Search on train
     ONLY; the holdout is touched ONCE per train-survivor, ever (``protocol.py``).
  2. PRE-REGISTERED gates (``gates.py``) committed before any result is read.
  3. DEFLATED / HAIRCUT Sharpe (``metrics.deflated_sharpe`` — Bailey & Lopez de Prado
     2014 / AFML Ch.8) so a win after N trials clears a higher bar than after 3.
  4. Pessimistic fills + conditional slippage (``costs.py``), 0/1/2-tick sensitivity.

Citation policy (operator decision, 2026-06-11): the actual texts are NOT on the VPS
(docs/research/book_principles.md — copyright). Each family cites the CANONICAL
published formulation by author/work/chapter and is flagged ``to-verify-against-source``;
NO page numbers are fabricated from memory. See ``families.py`` docstrings.

Nothing here imports a prod entry/exit path that would place an order. The generic
backtest engine (``engine.py``) re-implements the SAME pessimistic-fill / ratcheted-exit
semantics the live bot uses (and the BT-1 / tools.eval engines use), pinned by tests.
"""
