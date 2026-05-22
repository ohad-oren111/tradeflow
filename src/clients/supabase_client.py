"""Async REST client for Supabase PostgREST API.

Custom client — NOT supabase-py. The supabase-py SDK is sync-first and pulls in
heavy deps; TradeFlow drives Supabase through PostgREST directly via httpx.
See standing rule §0.5.T1 / handoff history.

Phase 1 PR 2 scope: select / insert / upsert + close. update / delete arrive in
PR 3+ when there is an actual caller for them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)


class SupabaseClient:
    """Async REST wrapper around PostgREST. Tests inject ``http_client``."""

    def __init__(
        self,
        url: str,
        key: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = f"{url.rstrip('/')}/rest/v1"
        self._key = key
        self._http: httpx.AsyncClient = http_client or httpx.AsyncClient(
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

    async def select(
        self,
        table: str,
        filters: dict[str, str] | None = None,
        columns: str = "*",
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"select": columns}
        if filters:
            params.update(filters)
        url = f"{self._base}/{table}"
        LOGGER.debug("[supabase_client] select %s — params=%s", table, params)
        r = await self._http.get(url, params=params)
        r.raise_for_status()
        return r.json()

    async def insert(
        self,
        table: str,
        payload: dict[str, Any] | list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        url = f"{self._base}/{table}"
        rows = len(payload) if isinstance(payload, list) else 1
        LOGGER.info("[supabase_client] insert %s — rows=%s", table, rows)
        r = await self._http.post(url, json=payload, headers={"Prefer": "return=representation"})
        r.raise_for_status()
        return r.json()

    async def upsert(
        self,
        table: str,
        payload: dict[str, Any] | list[dict[str, Any]],
        on_conflict: str | None = None,
    ) -> list[dict[str, Any]]:
        url = f"{self._base}/{table}"
        headers = {"Prefer": "resolution=merge-duplicates,return=representation"}
        params: dict[str, str] = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
        rows = len(payload) if isinstance(payload, list) else 1
        LOGGER.info(
            "[supabase_client] upsert %s — rows=%s on_conflict=%s",
            table,
            rows,
            on_conflict,
        )
        r = await self._http.post(url, json=payload, params=params, headers=headers)
        r.raise_for_status()
        return r.json()

    async def close(self) -> None:
        await self._http.aclose()

    # ----------------------------------------------------------- lifecycles API
    # Additive helpers introduced in PR #9 for the state machine. Existing
    # method signatures above are unchanged.

    async def insert_lifecycle(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Insert a row into ``lifecycles`` and return the inserted representation."""
        url = f"{self._base}/lifecycles"
        LOGGER.info("[supabase_client] insert lifecycles — symbol=%s", row.get("symbol"))
        r = await self._http.post(url, json=row, headers={"Prefer": "return=representation"})
        r.raise_for_status()
        return r.json()

    async def update_lifecycle(
        self,
        lifecycle_id: str,
        updates: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """PATCH a single ``lifecycles`` row by ``lifecycle_id``."""
        url = f"{self._base}/lifecycles"
        params = {"lifecycle_id": f"eq.{lifecycle_id}"}
        LOGGER.info(
            "[supabase_client] update lifecycles — id=%s keys=%s",
            lifecycle_id,
            sorted(updates.keys()),
        )
        r = await self._http.patch(
            url,
            json=updates,
            params=params,
            headers={"Prefer": "return=representation"},
        )
        r.raise_for_status()
        return r.json()

    async def select_lifecycles_non_closed(self) -> list[dict[str, Any]]:
        """Return all lifecycles with state != 'CLOSED'."""
        url = f"{self._base}/lifecycles"
        params = {"select": "*", "state": "neq.CLOSED"}
        LOGGER.debug("[supabase_client] select lifecycles non_closed")
        r = await self._http.get(url, params=params)
        r.raise_for_status()
        return r.json()

    async def select_lifecycles_non_closed_for(
        self,
        symbol: str,
        strategy: str,
    ) -> list[dict[str, Any]]:
        """Return non-CLOSED lifecycles for a given (symbol, strategy) pair.

        Used by the state machine's create_lifecycle probe — enforces the
        "at most one non-CLOSED per (symbol, strategy)" invariant in code.
        """
        url = f"{self._base}/lifecycles"
        params = {
            "select": "*",
            "symbol": f"eq.{symbol}",
            "strategy": f"eq.{strategy}",
            "state": "neq.CLOSED",
        }
        LOGGER.debug(
            "[supabase_client] select lifecycles non_closed — symbol=%s strategy=%s",
            symbol,
            strategy,
        )
        r = await self._http.get(url, params=params)
        r.raise_for_status()
        return r.json()

    async def insert_lifecycle_event(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Append a row to the ``lifecycle_events`` audit trail."""
        url = f"{self._base}/lifecycle_events"
        LOGGER.info(
            "[supabase_client] insert lifecycle_events — id=%s to_state=%s",
            row.get("lifecycle_id"),
            row.get("to_state"),
        )
        r = await self._http.post(url, json=row, headers={"Prefer": "return=representation"})
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------- halt_acks API
    # PR #12 — halt-ack poll. Reconciler queries this each drain tick when the
    # orchestrator is halted; operator INSERTs (via Supabase dashboard SQL
    # editor or scripts/ack_halt.sh in a future PR) clear the halt.

    async def get_newest_halt_ack(self, since: datetime) -> dict[str, Any] | None:
        """Return the newest ``halt_acks`` row with ``acked_at > since``, or None.

        Returns the row dict (``halt_ack_id``, ``acked_at``, ``note``) or None
        when no qualifying row exists. Tolerates the table being absent (404):
        logs DEBUG and returns None so the reconciler falls back to the file
        flag without raising. Other HTTP / network errors are re-raised so the
        caller can fall back deliberately rather than silently masking outages.
        """
        iso = since.astimezone(UTC).isoformat()
        url = f"{self._base}/halt_acks"
        params = {
            "select": "halt_ack_id,acked_at,note",
            "acked_at": f"gt.{iso}",
            "order": "acked_at.desc",
            "limit": "1",
        }
        LOGGER.debug("[supabase_client] get_newest_halt_ack — since=%s", iso)
        r = await self._http.get(url, params=params)
        if r.status_code == 404:
            LOGGER.debug("[supabase_client] halt_acks: table missing — 404 (migration not applied)")
            return None
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return None
        return rows[0]
