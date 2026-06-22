"""Tests for the IB market-data farm-flap auto-resubscribe path (§0.5.181).

The IBKR data farm drops several times a day with some combination of 2103 /
2105 / 10182 and recovers ~1s later (2104/2106); the keepUpToDate bar
subscription dies and is never re-armed. ``IBClient`` watches ``ib.errorEvent``
and re-invokes the orchestrator's subscribe callable after a debounce.

W-S14.2 Track 3: detection is "ANY of the farm-flap codes", not the full trio —
the 04:03 UTC flap that blinded the feed for 37 min was 2105 + 10182 with no
2103, so the old all-of-trio guard never fired. The 30s idempotency guard keeps
a multi-code flap collapsed to exactly one resubscribe.

Mocked at the IBClient wrapper boundary — no real ib_async network calls. Each
test gets a fresh mock IB (per the ``mock_ib_factory`` fixture in conftest).
``asyncio_mode = auto`` (pyproject) means async tests need no explicit marker.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from src.clients import ib_client as ib_client_mod
from src.clients.ib_client import IBClient


class _FakeEvent:
    """Minimal stand-in for ib_async ``Event`` that captures ``+=`` handlers."""

    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


def _make_client(mock_ib_factory) -> tuple[IBClient, MagicMock]:
    fake_ib = mock_ib_factory()
    fake_ib.errorEvent = _FakeEvent()
    client = IBClient(host="h", port=4002, client_id=1, ib_factory=lambda: fake_ib)
    return client, fake_ib


def _feed(client: IBClient, *codes: int) -> None:
    """Fire ib.errorEvent(reqId, errorCode, errorString, contract) for each code."""
    for code in codes:
        client._on_ib_error_farm_flap(4, code, f"err {code}", None)


# C1 — the full trio still collapses to exactly one resubscribe (guard holds).
async def test_completed_trio_triggers_single_resubscribe(mock_ib_factory, monkeypatch):
    monkeypatch.setattr(ib_client_mod, "_FARM_FLAP_DEBOUNCE_SEC", 0.0)
    client, fake_ib = _make_client(mock_ib_factory)
    resubscribe = AsyncMock(return_value=None)
    client.arm_farm_flap_watch(asyncio.get_running_loop(), resubscribe)

    assert len(fake_ib.errorEvent.handlers) == 1, "exactly one errorEvent handler wired"

    _feed(client, 2103, 2105, 10182)
    # Recovery codes must be ignored (not part of the trio).
    _feed(client, 2104, 2106)
    await asyncio.sleep(0.05)

    assert resubscribe.await_count == 1


# C2 — both the INFO recovery log and the [ALERT] line are emitted.
async def test_resubscribe_emits_info_and_alert_logs(mock_ib_factory, monkeypatch, caplog):
    monkeypatch.setattr(ib_client_mod, "_FARM_FLAP_DEBOUNCE_SEC", 0.0)
    client, _ = _make_client(mock_ib_factory)
    resubscribe = AsyncMock(return_value=None)
    client.arm_farm_flap_watch(asyncio.get_running_loop(), resubscribe)

    with caplog.at_level(logging.INFO, logger="src.clients.ib_client"):
        _feed(client, 2103, 2105, 10182)
        await asyncio.sleep(0.05)

    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        m.startswith("[ORCH] bar_subscription auto-resubscribed after farm-flap") for m in msgs
    )
    # Demoted from [ALERT] to [FEED] (episode-scoped alerting, 2026-06-22): a
    # routine farm-flap auto-resubscribe is log-only, not a Telegram message.
    assert any(m.startswith("[FEED] bar_sub_resubscribed_after_farm_flap") for m in msgs)


# C3 — two trios inside the 30s guard collapse to a single resubscribe.
async def test_two_rapid_trios_dedup_to_single_resubscribe(mock_ib_factory, monkeypatch):
    monkeypatch.setattr(ib_client_mod, "_FARM_FLAP_DEBOUNCE_SEC", 0.0)
    client, _ = _make_client(mock_ib_factory)
    resubscribe = AsyncMock(return_value=None)
    client.arm_farm_flap_watch(asyncio.get_running_loop(), resubscribe)

    _feed(client, 2103, 2105, 10182)  # trio 1 — claims the slot
    _feed(client, 2103, 2105, 10182)  # trio 2 — within the 30s guard, skipped
    await asyncio.sleep(0.05)

    assert resubscribe.await_count == 1


# C4a — a 2105-only flap (no 2103) must resubscribe (the 04:03 UTC blind window).
async def test_2105_only_triggers_resubscribe(mock_ib_factory, monkeypatch):
    monkeypatch.setattr(ib_client_mod, "_FARM_FLAP_DEBOUNCE_SEC", 0.0)
    client, _ = _make_client(mock_ib_factory)
    resubscribe = AsyncMock(return_value=None)
    client.arm_farm_flap_watch(asyncio.get_running_loop(), resubscribe)

    _feed(client, 2105)
    await asyncio.sleep(0.05)

    assert resubscribe.await_count == 1


# C4b — a 10182-only flap must resubscribe.
async def test_10182_only_triggers_resubscribe(mock_ib_factory, monkeypatch):
    monkeypatch.setattr(ib_client_mod, "_FARM_FLAP_DEBOUNCE_SEC", 0.0)
    client, _ = _make_client(mock_ib_factory)
    resubscribe = AsyncMock(return_value=None)
    client.arm_farm_flap_watch(asyncio.get_running_loop(), resubscribe)

    _feed(client, 10182)
    await asyncio.sleep(0.05)

    assert resubscribe.await_count == 1


# C4c — the 2105 + 10182 flap (no 2103) collapses to a single resubscribe.
async def test_2105_plus_10182_collapses_to_one_resubscribe(mock_ib_factory, monkeypatch):
    monkeypatch.setattr(ib_client_mod, "_FARM_FLAP_DEBOUNCE_SEC", 0.0)
    client, _ = _make_client(mock_ib_factory)
    resubscribe = AsyncMock(return_value=None)
    client.arm_farm_flap_watch(asyncio.get_running_loop(), resubscribe)

    _feed(client, 2105, 10182)  # the exact 04:03 UTC signature
    await asyncio.sleep(0.05)

    assert resubscribe.await_count == 1


# C4d — an unrelated error code must NOT resubscribe.
async def test_unrelated_error_code_does_not_resubscribe(mock_ib_factory, monkeypatch):
    monkeypatch.setattr(ib_client_mod, "_FARM_FLAP_DEBOUNCE_SEC", 0.0)
    client, _ = _make_client(mock_ib_factory)
    resubscribe = AsyncMock(return_value=None)
    client.arm_farm_flap_watch(asyncio.get_running_loop(), resubscribe)

    _feed(client, 2104, 2106, 200, 399)  # recovery + benign codes
    await asyncio.sleep(0.05)

    assert resubscribe.await_count == 0


# C5 — the resubscribe touches only the data path; no order/strategy side effects.
async def test_resubscribe_touches_only_data_path_no_orders(mock_ib_factory, monkeypatch):
    monkeypatch.setattr(ib_client_mod, "_FARM_FLAP_DEBOUNCE_SEC", 0.0)
    client, fake_ib = _make_client(mock_ib_factory)
    fake_ib.isConnected.return_value = True
    fake_ib.reqHistoricalDataAsync = AsyncMock(return_value=[])
    contract = MagicMock(localSymbol="MNQM6")

    async def resubscribe() -> None:
        # Mirrors orchestrator._start_bar_subscription reusing the retained
        # contract + args — only the market-data line is exercised.
        await client.subscribe_bars(contract, bar_size="1 min", use_rth=False)

    client.arm_farm_flap_watch(asyncio.get_running_loop(), resubscribe)
    _feed(client, 2103, 2105, 10182)
    await asyncio.sleep(0.05)

    fake_ib.reqHistoricalDataAsync.assert_awaited_once()
    fake_ib.placeOrder.assert_not_called()
    fake_ib.cancelOrder.assert_not_called()
