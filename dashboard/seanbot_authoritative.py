"""Authoritative SeanBot daily realized P&L — operator-supplied ground truth.

Why this file exists (DASH-FIX, §7.3 follow-up):

The scoreboard previously reconstructed SeanBot's daily P&L by summing his
per-exit Telegram alerts. That reconstruction is structurally lossy and was
proven wrong against the operator's trusted numbers (e.g. 2026-06-01 summed to
~−$1,884 vs the trusted −$866.72). A PART-A probe of all 126 captured
``seanbot_signals`` rows confirmed the root cause:

  1. SeanBot posts NO end-of-day / daily-summary line to the channel — there is
     nothing in the captures to parse for an authoritative total.
  2. The listener only began capturing mid-day 2026-05-28, so 2026-05-26 and
     2026-05-27 (the two largest anchors) have ZERO captured data.
  3. SeanBot re-announces both entries and exits at drifting prices minutes
     apart, so the per-exit stream cannot be unambiguously collapsed to true
     trades — no dedup method reproduces the trusted anchors.

So the authoritative SeanBot daily P&L cannot come from the captures. The only
ground truth is the operator's own trusted daily figures, recorded below. These
are NOT fabricated and NOT a parse — they are the operator-supplied numbers
(handoff anchors). The scoreboard prefers these per day; days with no anchor
fall back to the lossy per-exit estimate, clearly labelled as an estimate.

Maintenance: append new days here as the operator confirms each day's true
SeanBot figure. Until a day is added, the scoreboard shows that day's deduped
per-exit *estimate* with an explicit "est." marker.
"""

from __future__ import annotations

# Operator-trusted SeanBot realized P&L per UTC trading day (USD on his own
# 2-contract book). Source: operator handoff anchors — SeanBot's self-reported
# truth, NOT reconstructed from captured alerts. Keys are ``YYYY-MM-DD``.
AUTHORITATIVE_SEANBOT_DAILY_PNL: dict[str, float] = {
    "2026-05-26": 2108.18,
    "2026-05-27": 1380.94,
    "2026-05-28": -616.20,
    "2026-06-01": -866.72,
    "2026-06-02": -202.96,
}


def authoritative_pnl(day: str) -> float | None:
    """Return the operator-trusted SeanBot P&L for ``day`` (``YYYY-MM-DD``),
    or ``None`` if that day has no authoritative anchor (caller falls back to
    the labelled per-exit estimate)."""
    return AUTHORITATIVE_SEANBOT_DAILY_PNL.get(day)
