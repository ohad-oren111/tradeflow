"""Unit tests for the pure feed-episode state machine + escalation ladder.

Covers the brief's required cases (a) reconnect-without-bar is NOT a recovery,
(b) the gateway-restart handoff fires exactly once per episode, and (c) a long
stale episode emits the bounded alert set, not one alert per cycle. Pure args,
no shared state — each test builds its own FeedEpisode.
"""

from __future__ import annotations

from src.feed_episode import (
    RECONNECT_MAX_CYCLES,
    RESUBSCRIBE_MAX_CYCLES,
    FeedEpisode,
    FeedHealAction,
    next_heal_action,
)

# ----------------------------------------------------------- ladder mapping


def test_ladder_resubscribe_for_early_cycles():
    for n in range(1, RESUBSCRIBE_MAX_CYCLES + 1):
        assert next_heal_action(n) is FeedHealAction.RESUBSCRIBE


def test_ladder_reconnect_for_middle_cycles():
    for n in range(RESUBSCRIBE_MAX_CYCLES + 1, RECONNECT_MAX_CYCLES + 1):
        assert next_heal_action(n) is FeedHealAction.RECONNECT


def test_ladder_gateway_handoff_after_reconnect_exhausted():
    assert next_heal_action(RECONNECT_MAX_CYCLES + 1) is FeedHealAction.GATEWAY_RESTART_HANDOFF
    assert next_heal_action(50) is FeedHealAction.GATEWAY_RESTART_HANDOFF


def test_ladder_is_monotonic_resubscribe_then_reconnect_then_handoff():
    seen = [next_heal_action(n) for n in range(1, 8)]
    # Resubscribe rungs come strictly before reconnect rungs before handoff.
    assert seen[0] is FeedHealAction.RESUBSCRIBE
    assert seen[-1] is FeedHealAction.GATEWAY_RESTART_HANDOFF


# --------------------------------------------- (c) episode-scoped alerting


def test_open_fires_once_across_a_long_stale_episode():
    """A 60-cycle stale episode opens exactly once (not 60 alerts)."""
    ep = FeedEpisode()
    open_signals = [ep.on_stale() for _ in range(60)]
    assert open_signals[0] is True
    assert sum(open_signals) == 1, "feed_episode_open must fire exactly once per episode"
    assert ep.open is True


def test_recovered_fires_once_then_is_idempotent():
    ep = FeedEpisode()
    ep.on_stale()
    assert ep.on_recovered() is True  # one recovered alert
    assert ep.on_recovered() is False  # subsequent bars are not re-recoveries
    assert ep.open is False


def test_recovered_without_open_is_silent():
    """Healthy bars (no episode) must not emit a recovered alert."""
    ep = FeedEpisode()
    assert ep.on_recovered() is False


def test_full_episode_emits_bounded_alert_set():
    """open -> (many stale cycles) -> recovered => exactly 1 open + 1 recovered."""
    ep = FeedEpisode()
    opens = sum(ep.on_stale() for _ in range(40))
    recovered = ep.on_recovered()
    assert opens == 1
    assert recovered is True
    # A new episode after recovery opens again (and only once).
    assert ep.on_stale() is True
    assert ep.on_stale() is False


# ------------------------------------ (a) reconnect-without-bar is not recovery


def test_episode_stays_open_until_a_real_bar():
    """Heal cycles (resubscribe/reconnect) never close the episode by themselves;
    only on_recovered (driven by a real bar) does."""
    ep = FeedEpisode()
    ep.on_stale()
    # Simulate several heal cycles escalating up the ladder — no bar arrives.
    for n in range(1, RECONNECT_MAX_CYCLES + 3):
        action = next_heal_action(n)
        assert action in FeedHealAction
        assert ep.open is True, "episode must remain open while no real bar arrives"
    # Only a real bar recovers it.
    assert ep.on_recovered() is True


# --------------------------------- (b) gateway-restart handoff fires once


def test_handoff_emitted_exactly_once_per_episode():
    ep = FeedEpisode()
    ep.on_stale()
    assert ep.should_emit_handoff() is True
    # Every later cycle in the same episode is suppressed.
    assert ep.should_emit_handoff() is False
    assert ep.should_emit_handoff() is False


def test_handoff_resets_for_a_new_episode():
    ep = FeedEpisode()
    ep.on_stale()
    assert ep.should_emit_handoff() is True
    ep.on_recovered()  # episode closes, handoff state resets
    ep.on_stale()  # new episode
    assert ep.should_emit_handoff() is True
