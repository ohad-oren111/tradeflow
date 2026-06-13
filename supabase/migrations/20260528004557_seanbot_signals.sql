-- =====================================================================
-- seanbot_signals: parsed SeanBot Telegram signals for empirical
-- comparison against TradeFlow's [STRAT] eval logs (PR-D3a/D3b).
--
-- One row per inbound Telegram message from the "Trading NQ Triggers"
-- channel. Parser writes structured columns when match succeeds; raw_text
-- + parsed_ok=false written for unknown shapes so nothing is dropped.
--
-- UNIQUE(channel, message_id) makes ingest idempotent — listener can
-- replay backlog or be restarted without dup rows.
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.seanbot_signals (
  id           BIGSERIAL PRIMARY KEY,
  ts           TIMESTAMPTZ NOT NULL,
  channel      TEXT        NOT NULL,
  message_id   BIGINT      NOT NULL,
  type         TEXT        NOT NULL CHECK (type IN ('entry','exit','stop_moved','unknown')),
  direction    TEXT        CHECK (direction IN ('long','short')),
  symbol       TEXT,
  price        NUMERIC,
  stop_price   NUMERIC,
  target_price NUMERIC,
  pnl_points   NUMERIC,
  contracts    INTEGER,
  raw_text     TEXT        NOT NULL,
  parsed_ok    BOOLEAN     NOT NULL DEFAULT FALSE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (channel, message_id)
);

CREATE INDEX IF NOT EXISTS idx_seanbot_signals_ts      ON public.seanbot_signals(ts);
CREATE INDEX IF NOT EXISTS idx_seanbot_signals_type_ts ON public.seanbot_signals(type, ts);

ALTER TABLE public.seanbot_signals ENABLE ROW LEVEL SECURITY;
