"""Q-E — tests for the on-occurrence auto-verification watchers + the broker-pnl
semantics verdict. Pure functions; fresh synthetic log text per test (no shared state)."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

from src.execution.reconciler import broker_pnl_semantics_verdict

# scripts/ is not a package; load occurrence_watchers by path. Register it in
# sys.modules before exec so its @dataclass can resolve its own __module__.
_SPEC = importlib.util.spec_from_file_location(
    "occurrence_watchers",
    str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "occurrence_watchers.py"),
)
ow = importlib.util.module_from_spec(_SPEC)
sys.modules["occurrence_watchers"] = ow
_SPEC.loader.exec_module(ow)


# --------------------------------------------------------------- quarterly_roll
def test_quarterly_roll_confirmed():
    log = (
        "[ROLL] rolling MNQU6→MNQZ6 dte=0 — flat book; graceful restart to re-resolve\n"
        "[KILL] epoch: restored — 2026-06-23T17:21:02+00:00 (age 88.0h)\n"
        "[WARMUP-ENABLE] strategy buffer seeded from backfill — bars=7000\n"
    )
    r = ow.check_quarterly_roll(log)
    assert r.status == "CONFIRMED"


def test_quarterly_roll_partial_when_reseed_missing():
    log = (
        "[ROLL] rolling MNQU6→MNQZ6 dte=0 — flat book; graceful restart\n"
        "[KILL] epoch: restored — 2026-06-23T17:21:02+00:00 (age 88.0h)\n"
    )
    r = ow.check_quarterly_roll(log)
    assert r.status == "PARTIAL"
    assert "warmup-reseed" in r.detail


def test_quarterly_roll_pending_on_no_roll():
    log = "[ROLL] resolved front-month=MNQU6 dte=86 — no roll (already current)\n"
    assert ow.check_quarterly_roll(log).status == "PENDING"


# ------------------------------------------------------- feed_wedge_escalation
def test_feed_wedge_escalation_confirmed_with_expiry_hint():
    log = (
        "[ALERT] feed_episode_open: stale_min=6\n"
        "[FEED] feed_episode_gateway_restart_needed — app self-heals exhausted (5 cycles)\n"
        "[FEED] episode terminal — expiry_suspected=True\n"
    )
    r = ow.check_feed_wedge_escalation(log)
    assert r.status == "CONFIRMED"
    assert "expiry-suspected" in r.detail


def test_feed_wedge_escalation_partial_open_only():
    log = "[ALERT] feed_episode_open: stale_min=6\n"
    assert ow.check_feed_wedge_escalation(log).status == "PARTIAL"


def test_feed_wedge_escalation_pending():
    assert ow.check_feed_wedge_escalation("[BAR] MNQU6: settled\n").status == "PENDING"


# -------------------------------------------------------- broker_pnl_semantics
def test_broker_pnl_semantics_pending():
    assert ow.check_broker_pnl_semantics("nothing here\n").status == "PENDING"


def test_broker_pnl_semantics_confirmed_surfaces_verdict():
    log = (
        "[RECON] broker_pnl backfilled — id=abc commission=1.24 realized=598.76\n"
        "[WATCH] broker_pnl_semantics: id=abc realized_broker=598.76 pnl_net_est=599.52 "
        "abs_delta=-0.76 rel=-0.1% — verdict=ESTIMATE-FAITHFUL\n"
    )
    r = ow.check_broker_pnl_semantics(log)
    assert r.status == "CONFIRMED"
    assert "ESTIMATE-FAITHFUL" in r.detail


# ------------------------------------------------- semantics verdict (reconciler)
def test_semantics_verdict_faithful_within_tolerance():
    v, _ = broker_pnl_semantics_verdict(599.52, 599.0)
    assert v == "ESTIMATE-FAITHFUL"


def test_semantics_verdict_broker_lower():
    # realized materially below the estimate -> estimate too rosy
    v, d = broker_pnl_semantics_verdict(400.0, 600.0)
    assert v == "BROKER-LOWER"
    assert "rel=" in d


def test_semantics_verdict_broker_higher():
    v, _ = broker_pnl_semantics_verdict(700.0, 500.0)
    assert v == "BROKER-HIGHER"


def test_semantics_verdict_indeterminate_on_missing():
    assert broker_pnl_semantics_verdict(None, 1.0)[0] == "INDETERMINATE"
    assert broker_pnl_semantics_verdict(1.0, None)[0] == "INDETERMINATE"
