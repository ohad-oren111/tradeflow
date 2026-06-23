-- =====================================================================
-- lifecycles: ADDITIVE broker-truth realized P&L + commission columns
-- (Q4b). The existing pnl_gross / pnl_net / commission_total stay a
-- FIXED-rate estimate (MNQ.commission_rt_usd) and remain the kill-switch
-- drawdown input — UNCHANGED. These new nullable columns carry IBKR's
-- own numbers, captured best-effort by the reconciler from the broker
-- fills' commissionReport once they arrive (commissionReport is delivered
-- asynchronously after the fill, so it is backfilled, not written inline).
--
--   commission_broker   = Σ commissionReport.commission over the lifecycle's
--                         entry + exit fills (true round-trip commission).
--   realized_pnl_broker = Σ commissionReport.realizedPNL over the exit
--                         (closing) fills (IBKR's realized P&L for the close).
--
-- Both NULL when the broker numbers are not yet available (e.g. the fill
-- predates a reconnect that cleared IB.fills(), or commissionReport has not
-- arrived). No default, no backfill of historical rows.
--
-- ⚠ SEMANTICS NOT YET LIVE-VERIFIED on DUQ331660 (no fills available to
-- probe at authoring time — last trade 2026-06-18). Confirm against the next
-- live round-trip before trusting these columns as ground truth.
-- =====================================================================
ALTER TABLE public.lifecycles
  ADD COLUMN IF NOT EXISTS commission_broker   NUMERIC(10, 4),
  ADD COLUMN IF NOT EXISTS realized_pnl_broker NUMERIC(12, 4);
