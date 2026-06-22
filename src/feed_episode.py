"""Pure feed-episode state machine + self-heal escalation ladder.

A *feed episode* is one continuous period during which the bar feed is stale
(no new bars during an expected CME session). The orchestrator opens an episode
on the first stale detection and closes it when a real bar resumes. Episode
scoping collapses a multi-day outage to a bounded set of Telegram alerts
(``feed_episode_open`` once, ``feed_episode_recovered`` once) instead of one per
5-minute watchdog cycle — the 2026-06-20 outage emitted ~800 messages because
every cycle re-alerted.

This module is intentionally PURE: no clock, no I/O, no logging. The orchestrator
owns all side effects (logging, Telegram, reconnects); this module only decides
*what* should happen. That keeps the escalation/alert-routing logic unit-testable
with explicit args and no shared mock state (master test-safety guardrails).

Escalation ladder (driven by the count of consecutive heal cycles that have NOT
been cleared by a real bar):

    cycles 1..2          -> RESUBSCRIBE   (soft: re-request keepUpToDate)
    cycles 3..4          -> RECONNECT     (forced socket reconnect)
    cycle  5+            -> GATEWAY_RESTART_HANDOFF

The app cannot restart the IB-gateway container (no docker socket), so the final
rung is a *handoff*: the orchestrator logs a machine-readable marker that the
host watchdog (`scripts/tradeflow_watchdog.py`, which has docker perms) detects
and acts on by restarting the gateway. The thresholds live here so the ladder is
testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# Ladder thresholds, in units of consecutive failed heal cycles. A "failed" cycle
# is one that did not produce a live bar (the orchestrator resets the count to 0
# in _on_new_bar). The count is incremented BEFORE the action is chosen, so the
# first heal cycle has count == 1.
RESUBSCRIBE_MAX_CYCLES = 2
RECONNECT_MAX_CYCLES = 4
# count >= RECONNECT_MAX_CYCLES + 1 -> hand off to the host watchdog.


class FeedHealAction(Enum):
    """The escalation rung to take for a given failed-heal count."""

    RESUBSCRIBE = "resubscribe"
    RECONNECT = "reconnect"
    GATEWAY_RESTART_HANDOFF = "gateway_restart_handoff"


def next_heal_action(consecutive_failed_heals: int) -> FeedHealAction:
    """Map the consecutive-failed-heal count to the next escalation rung (pure).

    >>> next_heal_action(1)
    <FeedHealAction.RESUBSCRIBE: 'resubscribe'>
    >>> next_heal_action(3)
    <FeedHealAction.RECONNECT: 'reconnect'>
    >>> next_heal_action(5)
    <FeedHealAction.GATEWAY_RESTART_HANDOFF: 'gateway_restart_handoff'>
    """
    if consecutive_failed_heals <= RESUBSCRIBE_MAX_CYCLES:
        return FeedHealAction.RESUBSCRIBE
    if consecutive_failed_heals <= RECONNECT_MAX_CYCLES:
        return FeedHealAction.RECONNECT
    return FeedHealAction.GATEWAY_RESTART_HANDOFF


@dataclass
class FeedEpisode:
    """Tracks one stale-feed episode so alerting is episode-scoped, not per-cycle.

    All transitions are idempotent and return whether the caller should emit the
    corresponding *once-per-episode* Telegram alert / handoff marker. The caller
    keeps the side effects; this object keeps only the booleans.
    """

    open: bool = False
    # True once the gateway-restart handoff marker has been logged for THIS
    # episode, so it is emitted at most once (the watchdog only needs to see it
    # once; re-logging every cycle would defeat the noise-collapse goal).
    handoff_emitted: bool = False

    def on_stale(self) -> bool:
        """Record a stale-feed observation.

        Returns True exactly once per episode — on the transition from healthy to
        stale — so the caller emits a single ``feed_episode_open`` alert. Repeated
        stale observations while already open return False (log-only).
        """
        if self.open:
            return False
        self.open = True
        self.handoff_emitted = False
        return True

    def on_recovered(self) -> bool:
        """Record a real live bar.

        Returns True iff an episode was open (so the caller emits a single
        ``feed_episode_recovered`` alert) and resets episode state. Returns False
        when no episode was open (healthy bars — no alert).
        """
        was_open = self.open
        self.open = False
        self.handoff_emitted = False
        return was_open

    def should_emit_handoff(self) -> bool:
        """Return True the first time the gateway-restart handoff is reached in
        this episode, False thereafter — so the watchdog handoff marker is logged
        once per episode, not once per heal cycle.
        """
        if self.handoff_emitted:
            return False
        self.handoff_emitted = True
        return True
