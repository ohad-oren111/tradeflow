-- =====================================================================
-- halt_acks: append-only ack log for clearing the orchestrator halt flag.
--
-- Each row is one operator ack. Reconciler polls newest acked_at > the
-- halt_raised_at timestamp; if a row matches, the in-memory halt clears.
-- See PR #12 brief — Supabase is the primary ack channel; /tmp/halt_clear
-- inside the container is the fallback when Supabase is unreachable.
-- =====================================================================
CREATE TABLE halt_acks (
    halt_ack_id  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    acked_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    note         TEXT
);

CREATE INDEX halt_acks_acked_at_idx ON halt_acks (acked_at DESC);

ALTER TABLE halt_acks ENABLE ROW LEVEL SECURITY;
