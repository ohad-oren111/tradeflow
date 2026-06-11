"""Phase-17 — Multi-Market Daily Trend (the breadth thesis).

Research-only (CLAUDE.md §0.5.208 MEASURE lane). Drives NO prod path. Multi-session.

Stage 1 (this commit): DATA. Backfill DAILY bars (clientId read-only) for a basket of
~10 micros spanning uncorrelated buckets, build continuous back-adjusted (Panama)
series per market, DATA-QA each, and report — then STOP for operator review before any
strategy work (Stage 2).
"""
