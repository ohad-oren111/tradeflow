"""TradeFlow evaluation kit (Session 24) — accelerated, offline proofs of the live
strategy + exit, plus guarded live/fault tooling.

This package is ADDITIVE TEST TOOLING. It imports the real production strategy and
exit code (``src.strategy``, ``src.execution.trail_manager``, ``src.execution.kill_switch``)
and drives it over saved history or synthetic bars with MODELED fills. It NEVER
connects to IBKR, NEVER writes to the production Supabase, and NEVER touches a
prod lifecycle. The two live tiers (Phase 3 round-trip, Phase 4 fault injection)
are AUDIT-gated scripts that refuse to run without explicit operator-coordination
flags.

See ``tools/eval/README.md`` and CLAUDE.md §0.5.220 (the accelerated-test playbook).
"""
