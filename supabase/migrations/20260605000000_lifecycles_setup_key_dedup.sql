-- =====================================================================
-- PR-B: same-setup dedup — durable backstop for "never two brackets for
-- one triggering bar" while allowing up to max_concurrent stacked positions.
--
-- REPLACES the PR-A GATE-1 index `lifecycles_one_open_per_symbol_strategy`
-- (UNIQUE (symbol, strategy) WHERE state <> 'CLOSED'), which hard-capped ONE
-- open position per (symbol, strategy). With concurrency ON (max_concurrent=3)
-- that ≤1 index would block legitimate distinct-setup stacking, so it is dropped.
--
-- The new index keys on the per-triggering-bar `setup_key` stored in
-- lifecycles.metadata (JSONB — no schema/column migration). Both entry paths
-- (strategy bar-eval + SeanBot-trigger) compute an IDENTICAL setup_key when they
-- react to the same 1-min bar; the second insert for that key hits 23505
-- (unique_violation) -> PostgREST 409, which create_lifecycle catches and
-- converts to InvariantViolationError (so NO second bracket / 4 contracts — the
-- 2026-06-01 double-entry, lifecycles c06ed026 + 347d5a12). This is the DB
-- backstop behind the in-process asyncio.Lock + in-lock setup_key probe.
--
-- An EXPRESSION partial unique index: Postgres accepts a parenthesised JSONB
-- extraction `(metadata->>'setup_key')` as an index column.
--
-- NON-STRANDING: a currently-open lifecycle predates setup_key, so
-- (metadata->>'setup_key') IS NULL. Postgres treats NULLs as DISTINCT in a
-- unique index, so the build never conflicts on legacy open rows (verified
-- exactly 1 non-CLOSED row at authoring time, lifecycle 2812b3fb / its successor;
-- even multiple NULL-key open rows would not collide). Distinct REAL setup_keys
-- stack freely up to max_concurrent.
--
-- NOT identity coupling (cf. Botty G105/G106): the surrogate lifecycle_id UUID
-- stays the primary key; the index is PARTIAL (WHERE state <> 'CLOSED'), so
-- unlimited CLOSED rows (full history + re-entry after close) remain untouched.
--
-- Additive + idempotent (DROP INDEX IF EXISTS / CREATE UNIQUE INDEX IF NOT EXISTS).
-- =====================================================================

DROP INDEX IF EXISTS lifecycles_one_open_per_symbol_strategy;

CREATE UNIQUE INDEX IF NOT EXISTS lifecycles_one_open_per_setup
  ON public.lifecycles (symbol, strategy, (metadata->>'setup_key'))
  WHERE state <> 'CLOSED';
