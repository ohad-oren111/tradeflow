"""Tests for tools/eval/scenarios.py — Phase 2 synthetic scenarios.

CI runs a FAST subset: the exit + kill-switch scenarios with the regime gate OFF
(no 6000-bar warmup) plus ONE regime-ON block scenario, each checked for the
expected verdict AND determinism (same seed → identical actual). The full K>=5
regime-ON suite (all 8) is run by ``python -m tools.eval.scenarios`` and reported
separately; this keeps the pytest suite bounded while still proving each path and
that the harness is reproducible.
"""

from __future__ import annotations

import pytest

from tools.eval import scenarios as scn


@pytest.mark.parametrize(
    "fn",
    [
        scn.scn2_profit_walk,
        scn.scn3_base_stop_loss,
        scn.scn4_v_reversal_giveback,
        scn.scn8_hard_ceiling,
    ],
)
def test_exit_scenarios_pass_and_deterministic(fn):
    v1 = fn(0, 12345, above_trend=False)
    v2 = fn(0, 12345, above_trend=False)
    assert v1.passed, f"{fn.__name__} failed: {v1.actual}"
    assert v1.actual == v2.actual, "non-deterministic"


def test_killswitch_streak_scenario():
    v1 = scn.scn7_killswitch_streak(0, 77)
    v2 = scn.scn7_killswitch_streak(0, 77)
    assert v1.passed, v1.actual
    assert v1.actual == v2.actual


def test_regime_block_scenario():
    # one regime-ON variant — exercises the armed 30m-EMA200 gate on real strategy code.
    v = scn.scn1_below_trend_block(0, 4242)
    assert v.passed, v.actual
    assert v.actual["decision_regime_on"] == "noop_regime"
    assert v.actual["decision_regime_off"] == "long_signal"
    assert v.actual["armed_buckets"] >= 202


def test_scenario_registry_complete():
    assert len(scn.SCENARIOS) == 8
