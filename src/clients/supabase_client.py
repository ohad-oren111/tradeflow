"""Async REST client for Supabase PostgREST API.

Custom client — NOT supabase-py. The supabase-py SDK is sync-first and pulls in
heavy deps; TradeFlow drives Supabase through PostgREST directly via httpx.
See standing rule §0.5.T1 / handoff history.

Phase 1 PR 2 scope: select / insert / upsert + close. update / delete arrive in
PR 3+ when there is an actual caller for them.
"""

from __future__ import annotations

import logging
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
