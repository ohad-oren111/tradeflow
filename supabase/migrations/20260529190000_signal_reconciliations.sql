-- =====================================================================
-- signal_reconciliations: durable SeanBot-vs-TradeFlow reconciliation
-- records (W-S15.2). One row per reconciled SeanBot ENTRY signal.
--
-- Replaces the ephemeral /app/logs/reconciliations.jsonl as the source
-- for the daily-report SeanBot scorecard, which was wiped on every
-- container --force-recreate ("journal not found").
--
-- UNIQUE(channel, message_id) mirrors seanbot_signals and matches the
-- app-side reconciler's idempotency key — a re-poll/backfill upserts
-- the same row instead of duplicating.
--
-- RLS enabled with NO policies => deny-all to anon; the service-role
-- key bypasses RLS (mirrors lifecycles / seanbot_signals).
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.signal_reconciliations (
  id              BIGSERIAL   PRIMARY KEY,
  signal_ts       TIMESTAMPTZ NOT NULL,
  channel         TEXT        NOT NULL,
  message_id      BIGINT      NOT NULL,
  seanbot_type    TEXT,
  direction       TEXT,
  symbol          TEXT,
  price           NUMERIC,
  classification  TEXT        NOT NULL,
  justification   TEXT,
  tf_decision     JSONB,
  acknowledged_at TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (channel, message_id)
);

CREATE INDEX IF NOT EXISTS idx_signal_reconciliations_signal_ts
  ON public.signal_reconciliations(signal_ts);
CREATE INDEX IF NOT EXISTS idx_signal_reconciliations_classification_ts
  ON public.signal_reconciliations(classification, signal_ts);

ALTER TABLE public.signal_reconciliations ENABLE ROW LEVEL SECURITY;
