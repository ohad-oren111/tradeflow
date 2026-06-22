"""Env-wiring tests for the entry point (`main._build_orchestrator_from_env`).

Focus: the INSTRUMENT override that pins the traded front-month contract. The
bot has no auto-roll, so this env is the only knob that rolls the contract each
quarter — a regression that silently reverts it to the expired default would
take the feed dark (the 2026-06-19 MNQM6-expiry outage). These tests assert the
override flows through and that an unset env preserves the historical default.

Constructors (IBClient / SupabaseClient) do not open sockets, so building the
orchestrator from env is a pure, offline operation here.
"""

from __future__ import annotations

import pytest

import main as main_module


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal env for a successful build (paper account is the only required var)."""
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", "DUQ331660")
    # Keep the optional vars unset/explicit so the test is hermetic.
    monkeypatch.delenv("INSTRUMENT", raising=False)


def test_instrument_env_override_flows_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("INSTRUMENT", "MNQU6")

    orch = main_module._build_orchestrator_from_env()

    assert orch._instrument == "MNQU6"


def test_instrument_defaults_to_mnqm6_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)  # INSTRUMENT deleted

    orch = main_module._build_orchestrator_from_env()

    # Unset env must be a no-op vs the historical hard-coded default.
    assert orch._instrument == "MNQM6"


def test_arbitrary_instrument_passed_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("INSTRUMENT", "MNQZ6")

    orch = main_module._build_orchestrator_from_env()

    assert orch._instrument == "MNQZ6"
