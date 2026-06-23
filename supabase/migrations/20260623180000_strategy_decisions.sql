-- =====================================================================
-- strategy_decisions: durable mirror of TradeFlow's own per-eval strategy
-- decisions (PR-D3c). One row per analytically-useful bar decision — TF's
-- entries (long_signal) plus touch-band near-misses (touch_ok = true) —
-- written by src/comparison/decision_journal.py on the hourly digest tick.
--
-- SCHEMA-DRIFT REMEDIATION (Q2 DB-completeness audit, 2026-06-23): this
-- table was created out-of-band and has been written in production (2,558
-- rows as of the audit) with NO migration in the repo. A clean rebuild or
-- a fresh environment would therefore lack the table, and decision_journal
-- flushes — which swallow errors fire-and-forget — would fail SILENTLY.
-- This migration back-fills the canonical shape so the repo reproduces the
-- live schema. Idempotent (IF NOT EXISTS) => a no-op against the existing
-- production table, a create on any fresh database.
--
-- Columns mirror decision_journal.decision_to_row() exactly. UNIQUE
-- (symbol, decision_ts) IS the app's ON CONFLICT idempotency key
-- (_DECISIONS_ON_CONFLICT) — a re-flush/backfill upserts the same row.
--
-- RLS enabled with NO policies => deny-all to anon; the service-role key
-- bypasses RLS (mirrors lifecycles / seanbot_signals / signal_reconciliations).
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.strategy_decisions (
  id           BIGSERIAL   PRIMARY KEY,
  decision_ts  TIMESTAMPTZ NOT NULL,
  symbol       TEXT        NOT NULL,
  bar_count    INTEGER,
  decision     TEXT,
  failed_gate  TEXT,
  close        NUMERIC,
  sma100       NUMERIC,
  regime_ok    BOOLEAN,
  touch_ok     BOOLEAN,
  ma_order_ok  BOOLEAN,
  bullish_ok   BOOLEAN,
  gap_ok       BOOLEAN,
  raw          JSONB,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (symbol, decision_ts)
);

CREATE INDEX IF NOT EXISTS idx_strategy_decisions_decision_ts
  ON public.strategy_decisions(decision_ts);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_decision_ts_gate
  ON public.strategy_decisions(decision, failed_gate);

ALTER TABLE public.strategy_decisions ENABLE ROW LEVEL SECURITY;
