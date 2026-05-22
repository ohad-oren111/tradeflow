"""Asyncio-friendly set of lifecycle_ids that need reconciliation soon.

Single-loop usage — no locks needed. OrderRouter adds on events;
Reconciler drains atomically (.drain() returns + clears).
"""

from __future__ import annotations


class DirtySet:
    """Set of lifecycle_ids flagged for next-tick reconciliation."""

    def __init__(self) -> None:
        self._ids: set[str] = set()

    def add(self, lifecycle_id: str) -> None:
        self._ids.add(lifecycle_id)

    def drain(self) -> set[str]:
        ids = self._ids
        self._ids = set()
        return ids

    def __len__(self) -> int:
        return len(self._ids)

    def __contains__(self, lifecycle_id: object) -> bool:
        return lifecycle_id in self._ids
