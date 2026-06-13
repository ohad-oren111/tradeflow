-- =====================================================================
-- lifecycles: one row per trade lifecycle, identity stable across transitions
-- =====================================================================
CREATE TABLE lifecycles (
    lifecycle_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),

    symbol              TEXT         NOT NULL,
    strategy            TEXT         NOT NULL,
    direction           TEXT         NOT NULL CHECK (direction IN ('LONG','SHORT')),
    state               TEXT         NOT NULL CHECK (state IN ('IDLE','ENTERING','ACTIVE','EXITING','CLOSED')),

    entry_qty           INTEGER,
    entry_price         NUMERIC(12,4),
    entry_filled_at     TIMESTAMPTZ,
    entry_order_id      BIGINT,
    stop_order_id       BIGINT,
    target_order_id     BIGINT,
    stop_price          NUMERIC(12,4),
    target_price        NUMERIC(12,4),

    exit_qty            INTEGER,
    exit_price          NUMERIC(12,4),
    exit_filled_at      TIMESTAMPTZ,
    exit_order_id       BIGINT,
    exit_reason         TEXT         CHECK (exit_reason IS NULL OR exit_reason IN ('TARGET','STOP','EOD','MANUAL','KILL_SWITCH')),

    commission_total    NUMERIC(10,4),
    pnl_gross           NUMERIC(12,4),
    pnl_net             NUMERIC(12,4),

    metadata            JSONB        NOT NULL DEFAULT '{}'::jsonb,

    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX lifecycles_symbol_state_idx   ON lifecycles (symbol, state);
CREATE INDEX lifecycles_open_state_idx     ON lifecycles (state) WHERE state != 'CLOSED';
CREATE INDEX lifecycles_strategy_idx       ON lifecycles (strategy);
CREATE INDEX lifecycles_created_at_idx     ON lifecycles (created_at DESC);

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER lifecycles_touch_updated_at
    BEFORE UPDATE ON lifecycles
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- =====================================================================
-- lifecycle_events: append-only audit trail
-- =====================================================================
CREATE TABLE lifecycle_events (
    event_id       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    lifecycle_id   UUID         NOT NULL REFERENCES lifecycles(lifecycle_id) ON DELETE CASCADE,
    from_state     TEXT,
    to_state       TEXT         NOT NULL,
    reason         TEXT,
    payload        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    emitted_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX lifecycle_events_lifecycle_emitted_idx  ON lifecycle_events (lifecycle_id, emitted_at DESC);
CREATE INDEX lifecycle_events_to_state_emitted_idx   ON lifecycle_events (to_state, emitted_at DESC);

-- =====================================================================
-- RLS: deny-all; service_role bypasses
-- =====================================================================
ALTER TABLE lifecycles       ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifecycle_events ENABLE ROW LEVEL SECURITY;
