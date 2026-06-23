"""Tests for src.instruments.front_month — dynamic front-month resolve + roll (PR #170).

asyncio_mode=auto (pyproject) → async tests need no decorator. The resolver is
tested via an injected details_fetcher and the roller via injected callables —
no live broker, no orchestrator, no raw ib_async chain. Fresh mocks per test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.instruments.front_month import (
    FrontMonthRoller,
    resolve_front_month,
    select_front_month,
)

# A representative MNQ chain (local_symbol, lastTradeDateOrContractMonth).
_CHAIN = [
    ("MNQM6", "20260619"),  # Jun 2026 — expired relative to the test "now"
    ("MNQU6", "20260918"),  # Sep 2026
    ("MNQZ6", "20261218"),  # Dec 2026
    ("MNQH7", "20270319"),  # Mar 2027
]


def _details(chain: list[tuple[str, str]]) -> list:
    """Build fake ContractDetails (.contract.localSymbol/.lastTradeDateOrContractMonth)."""
    return [
        SimpleNamespace(contract=SimpleNamespace(localSymbol=sym, lastTradeDateOrContractMonth=exp))
        for sym, exp in chain
    ]


# ----------------------------------------------------------- select_front_month (pure)


def test_select_picks_nearest_expiry_beyond_buffer():
    # (a) now well before any expiry → nearest qualifying is MNQU6 (dte 88 > 8).
    now = datetime(2026, 6, 22, tzinfo=UTC)
    result = select_front_month(_CHAIN, buffer_days=8, now=now)
    assert result is not None
    symbol, dte = result
    assert symbol == "MNQU6"
    assert dte == 88


def test_select_skips_expiry_inside_buffer_for_next():
    # (b) now 6 days before MNQU6 expiry (< 8-day buffer) → skip it, pick MNQZ6.
    now = datetime(2026, 9, 12, tzinfo=UTC)
    result = select_front_month(_CHAIN, buffer_days=8, now=now)
    assert result is not None
    symbol, _dte = result
    assert symbol == "MNQZ6"


def test_select_skips_expired_contracts():
    # MNQM6 (20260619) is in the past for now=2026-06-22 → never selected.
    now = datetime(2026, 6, 22, tzinfo=UTC)
    symbol, _dte = select_front_month(_CHAIN, buffer_days=8, now=now)
    assert symbol != "MNQM6"


def test_select_returns_none_when_nothing_qualifies():
    now = datetime(2027, 6, 1, tzinfo=UTC)  # past every listed expiry
    assert select_front_month(_CHAIN, buffer_days=8, now=now) is None


def test_select_ignores_unparseable_expiry():
    chain = [("BAD", "not-a-date"), ("MNQU6", "20260918")]
    now = datetime(2026, 6, 22, tzinfo=UTC)
    symbol, _dte = select_front_month(chain, buffer_days=8, now=now)
    assert symbol == "MNQU6"


# ----------------------------------------------------------- resolve_front_month


async def test_resolve_reads_chain_from_details_fetcher():
    fetcher = AsyncMock(return_value=_details(_CHAIN))
    now = datetime(2026, 6, 22, tzinfo=UTC)
    result = await resolve_front_month(fetcher, buffer_days=8, now=now)
    assert result == ("MNQU6", 88)
    fetcher.assert_awaited_once()


# ----------------------------------------------------------- FrontMonthRoller


def _roller(*, resolver, current, positions, override=None):
    """Build a roller with fresh injected mocks. Returns (roller, restart_mock)."""
    restart = MagicMock(name="request_roll_restart")
    roller = FrontMonthRoller(
        resolver=resolver,
        current_instrument=lambda: current,
        get_positions=AsyncMock(return_value=positions),
        request_roll_restart=restart,
        override=override,
    )
    return roller, restart


async def test_override_short_circuits_resolution():
    # (d) INSTRUMENT override set → never resolves, never rolls.
    resolver = AsyncMock(return_value=("MNQZ6", 90))
    roller, restart = _roller(resolver=resolver, current="MNQU6", positions=[], override="MNQU6")
    status = await roller.check_once()
    assert status == "override"
    resolver.assert_not_awaited()
    restart.assert_not_called()


async def test_no_roll_when_resolved_equals_current():
    resolver = AsyncMock(return_value=("MNQU6", 88))
    roller, restart = _roller(resolver=resolver, current="MNQU6", positions=[])
    status = await roller.check_once()
    assert status == "current"
    restart.assert_not_called()


async def test_roll_deferred_when_in_position():
    # (c) resolved differs but a position is open → defer, do NOT restart.
    resolver = AsyncMock(return_value=("MNQZ6", 90))
    roller, restart = _roller(
        resolver=resolver, current="MNQU6", positions=[SimpleNamespace(position=2.0)]
    )
    status = await roller.check_once()
    assert status == "deferred"
    restart.assert_not_called()


async def test_roll_fires_restart_when_flat_and_different():
    resolver = AsyncMock(return_value=("MNQZ6", 90))
    roller, restart = _roller(resolver=resolver, current="MNQU6", positions=[])
    status = await roller.check_once()
    assert status == "rolling"
    restart.assert_called_once()


async def test_unresolved_does_not_roll():
    resolver = AsyncMock(return_value=None)
    roller, restart = _roller(resolver=resolver, current="MNQU6", positions=[])
    status = await roller.check_once()
    assert status == "unresolved"
    restart.assert_not_called()


async def test_resolver_exception_does_not_roll_or_raise():
    resolver = AsyncMock(side_effect=RuntimeError("chain fetch boom"))
    roller, restart = _roller(resolver=resolver, current="MNQU6", positions=[])
    status = await roller.check_once()  # must not raise
    assert status == "unresolved"
    restart.assert_not_called()


async def test_position_check_failure_defers_does_not_roll():
    # Unknown flatness must NOT roll (safe default).
    resolver = AsyncMock(return_value=("MNQZ6", 90))
    restart = MagicMock(name="request_roll_restart")
    roller = FrontMonthRoller(
        resolver=resolver,
        current_instrument=lambda: "MNQU6",
        get_positions=AsyncMock(side_effect=RuntimeError("positions boom")),
        request_roll_restart=restart,
    )
    status = await roller.check_once()
    assert status == "deferred"
    restart.assert_not_called()
