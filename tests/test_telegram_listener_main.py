"""Tests for the dialog-cache warmup added before ``client.get_entity()``.

A fresh ``StringSession`` has an empty dialog cache, so a title-string env
like ``"Trading NQ Triggers"`` raises ``ValueError`` from
``client.get_entity()`` unless the dialogs are walked first. The patch in
``telegram_listener.main`` walks dialogs once at startup — these tests pin
that ordering so a future refactor doesn't silently regress the fix.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.listeners import telegram_listener  # noqa: F401 — populate package attr


async def _aiter(items: list) -> object:
    for item in items:
        yield item


@pytest.fixture()
def env_fixture(monkeypatch):
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "x" * 100)
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "deadbeef")
    monkeypatch.setenv("TELEGRAM_SOURCE_CHANNEL", "Trading NQ Triggers")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-key")


async def test_main_warms_dialog_cache_before_get_entity(env_fixture, caplog):
    caplog.set_level(logging.INFO)
    calls: list[str] = []

    fake_entity = MagicMock(id=987654321)
    fake_client = MagicMock()
    fake_client.start = AsyncMock(side_effect=lambda: calls.append("start") or None)

    def iter_dialogs():
        calls.append("iter_dialogs")
        return _aiter([MagicMock(), MagicMock(), MagicMock()])

    fake_client.iter_dialogs = iter_dialogs

    async def get_entity_async(_chan):
        calls.append("get_entity")
        return fake_entity

    fake_client.get_entity = get_entity_async

    # run_until_disconnected returns immediately so main() exits cleanly.
    fake_client.run_until_disconnected = AsyncMock(return_value=None)

    fake_client.on = MagicMock(return_value=lambda fn: fn)

    fake_supabase = MagicMock()
    fake_supabase.close = AsyncMock(return_value=None)

    with (
        patch("src.listeners.telegram_listener.TelegramClient", return_value=fake_client),
        patch("src.listeners.telegram_listener.StringSession", return_value=MagicMock()),
        patch(
            "src.listeners.telegram_listener.SupabaseClient",
            return_value=fake_supabase,
        ),
    ):
        from src.listeners.telegram_listener import main

        await main()

    # iter_dialogs must run after start and before get_entity, exactly once.
    assert calls == ["start", "iter_dialogs", "get_entity"], calls
    assert any(
        "[TG_LISTENER] dialog cache warmed — count=3" in r.getMessage() for r in caplog.records
    )
    assert any(
        "[TG_LISTENER] connected — channel=Trading NQ Triggers id=987654321" in r.getMessage()
        for r in caplog.records
    )


async def test_main_no_session_idle_returns(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "")

    with patch("src.listeners.telegram_listener.asyncio.sleep", new=AsyncMock()):
        from src.listeners.telegram_listener import main

        await main()

    assert any("[TG_LISTENER] no session" in r.getMessage() for r in caplog.records)
