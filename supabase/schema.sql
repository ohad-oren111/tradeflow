-- ⚠️⚠️⚠️ STALE / ASPIRATIONAL — DO NOT TRUST FOR THE LIVE SCHEMA ⚠️⚠️⚠️
-- (Q2 DB-completeness audit, 2026-06-23.) This Phase-0 "v1" design was NEVER
-- deployed. The tables below — trades, positions, daily_summary,
-- kill_switch_events, signals — DO NOT EXIST in production (verified: all
-- return HTTP 404) and NO code reads or writes them. The live persistence
-- model is lifecycles + lifecycle_events (one row per trade, UUID identity),
-- plus halt_acks / seanbot_signals / signal_reconciliations / strategy_decisions.
-- GROUND TRUTH = the applied files in supabase/migrations/, NOT this file.
-- Kept only for historical reference; do not extend it. See
-- docs/audits/Q2_db_completeness_audit_2026-06-23.md.
--
-- TradeFlow Supabase schema v1
-- Defined in Phase 0 PR 1. Modify via migrations (PR > schema.sql diff).
-- DO NOT reference columns not present in this file (§0.5.96 carry-over from Botty).

CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    trade_id TEXT NOT NULL UNIQUE,            -- bot-generated stable ID (UUID4 hex)
    symbol TEXT NOT NULL,                     -- e.g. 'MNQM6'
    side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    contracts INTEGER NOT NULL CHECK (contracts > 0),
    entry_price NUMERIC,
    exit_price NUMERIC,
    sl_price NUMERIC,                         -- current stop-loss level
    highest_price NUMERIC,                    -- for trailing-stop reconstruction
    entry_at TIMESTAMPTZ,
    exit_at TIMESTAMPTZ,
    pnl_usd NUMERIC,
    commission_usd NUMERIC,
    status TEXT NOT NULL CHECK (status IN ('OPEN','CLOSED','CANCELLED','FAILED')),
    exit_reason TEXT,                         -- 'stop_loss','trail_stop','manual','market_close','kill_switch','ibkr_order_failed','limit_not_filled','false_signal_bad_sma'
    entry_order_ref TEXT,                     -- IBKR orderRef per §11 of architecture brief
    exit_order_ref TEXT,
    paper_mode BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_entry_at ON trades(entry_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_paper_mode ON trades(paper_mode);

CREATE TABLE IF NOT EXISTS positions (
    id BIGSERIAL PRIMARY KEY,
    trade_id TEXT NOT NULL UNIQUE REFERENCES trades(trade_id),
    symbol TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    entry_price NUMERIC NOT NULL,
    sl_price NUMERIC NOT NULL,
    highest_price NUMERIC NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ENTERING','ACTIVE','EXITING')),
    unrealized_pnl_usd NUMERIC,
    sl_order_id TEXT,                         -- IBKR order ID of the active GTC stop (§0.5.T5)
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_summary (
    id BIGSERIAL PRIMARY KEY,
    trade_date DATE NOT NULL UNIQUE,
    trades_count INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    gross_pnl_usd NUMERIC NOT NULL DEFAULT 0,
    commissions_usd NUMERIC NOT NULL DEFAULT 0,
    net_pnl_usd NUMERIC NOT NULL DEFAULT 0,
    starting_equity_usd NUMERIC,
    ending_equity_usd NUMERIC,
    max_drawdown_during_day_usd NUMERIC,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS kill_switch_events (
    id BIGSERIAL PRIMARY KEY,
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    guard TEXT NOT NULL CHECK (guard IN ('daily_dd','weekly_dd','consecutive_losses','manual')),
    equity_at_trigger_usd NUMERIC,
    daily_pnl_usd NUMERIC,
    weekly_pnl_usd NUMERIC,
    consecutive_losses INTEGER,
    positions_flattened INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id BIGSERIAL PRIMARY KEY,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    bar_time TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    signal_type TEXT NOT NULL,                -- 'long_entry','exit_trail','exit_sl','exit_market_close'
    bar_open NUMERIC,
    bar_high NUMERIC,
    bar_low NUMERIC,
    bar_close NUMERIC,
    sma_50 NUMERIC,
    sma_100 NUMERIC,
    ma_gap NUMERIC,                           -- sma_100 - sma_50
    touched_sma NUMERIC,                      -- which SMA value was within touch_buffer of bar_low
    decision TEXT NOT NULL CHECK (decision IN ('FILLED','SKIPPED','REJECTED')),
    decision_reason TEXT,                     -- 'cooldown','kill_switch_active','outside_hours','filled', etc.
    trade_id TEXT REFERENCES trades(trade_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_bar_time ON signals(bar_time DESC);
CREATE INDEX IF NOT EXISTS idx_signals_decision ON signals(decision);
