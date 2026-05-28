"""Tests for Track 3 decision journal (orchestrator ring buffer + JSONL) and the
inspect_decisions replay tool. Mocked at the IBClient/SupabaseClient boundary;
no IB, no DB, no real filesystem outside tmp_path.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from scripts import inspect_decisions
from src.clients.ib_client import IBClient
from src.clients.supabase_client import SupabaseClient
from src.orchestrator import _DECISION_JOURNAL_MAXLEN, Orchestrator


def _make_orch() -> Orchestrator:
    ib = AsyncMock(spec=IBClient)
    db = AsyncMock(spec=SupabaseClient)
    return Orchestrator(ib, db, paper_account="DUQ1234567")


def _sample(ts: str, decision: str = "noop_filter", failed: str | None = "touch") -> dict:
    return {
        "ts": ts,
        "decision": decision,
        "failed": failed,
        "close": 30312.5,
        "sma100": 30310.2,
        "regime_ok": True,
        "touch_ok": False,
        "ma_order_ok": True,
        "bullish_ok": True,
        "gap_ok": True,
    }


# ----- C4: ring buffer ------------------------------------------------------


def test_decision_journal_caps_at_maxlen_and_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr("src.orchestrator._DECISION_JSONL_PATH", str(tmp_path / "decisions.jsonl"))
    orch = _make_orch()
    total = _DECISION_JOURNAL_MAXLEN + 25
    for i in range(total):
        orch._record_decision(_sample(f"2026-05-28T22:{i:02d}:00+00:00", decision=f"d{i}"))

    # Ring buffer bounded.
    assert len(orch._decision_journal) == _DECISION_JOURNAL_MAXLEN
    # get_recent_decisions returns newest first.
    recent = orch.get_recent_decisions(5)
    assert len(recent) == 5
    assert recent[0]["decision"] == f"d{total - 1}"
    assert recent[-1]["decision"] == f"d{total - 5}"
    # Each record carries all gate keys.
    for key in (
        "ts",
        "decision",
        "failed",
        "close",
        "sma100",
        "regime_ok",
        "touch_ok",
        "ma_order_ok",
        "bullish_ok",
        "gap_ok",
    ):
        assert key in recent[0]


def test_record_decision_ignores_none():
    orch = _make_orch()
    orch._record_decision(None)
    assert len(orch._decision_journal) == 0


def test_record_decision_appends_jsonl(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "decisions.jsonl"  # parent dir does not exist yet
    monkeypatch.setattr("src.orchestrator._DECISION_JSONL_PATH", str(path))
    orch = _make_orch()
    orch._record_decision(_sample("2026-05-28T22:01:00+00:00", decision="noop_regime", failed=None))
    orch._record_decision(_sample("2026-05-28T22:02:00+00:00", decision="long_signal", failed=None))

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["decision"] == "noop_regime"


def test_record_decision_survives_unwritable_path(monkeypatch):
    # An unwritable JSONL path must not break the bar path — ring buffer still fills.
    monkeypatch.setattr("src.orchestrator._DECISION_JSONL_PATH", "/proc/cannot/write.jsonl")
    orch = _make_orch()
    orch._record_decision(_sample("2026-05-28T22:03:00+00:00"))
    assert len(orch._decision_journal) == 1


# ----- C6: inspect_decisions replay tool ------------------------------------


def _write_journal(tmp_path) -> str:
    rows = [
        _sample("2026-05-28T22:00:00+00:00", decision="noop_warmup", failed=None),
        _sample("2026-05-28T22:05:00+00:00", decision="noop_filter", failed="touch"),
        _sample("2026-05-28T22:10:00+00:00", decision="noop_regime", failed=None),
        _sample("2026-05-28T22:15:00+00:00", decision="long_signal", failed=None),
    ]
    p = tmp_path / "decisions.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(p)


def test_inspect_load_and_render(tmp_path):
    path = _write_journal(tmp_path)
    rows = inspect_decisions.load_decisions(path)
    assert len(rows) == 4
    out = inspect_decisions.render(rows)
    assert "noop_filter" in out
    assert "failed=touch" in out
    assert "long_signal" in out


def test_inspect_last_filter(tmp_path):
    path = _write_journal(tmp_path)
    rows = inspect_decisions.load_decisions(path)
    last2 = inspect_decisions.filter_decisions(rows, last=2)
    assert len(last2) == 2
    assert last2[-1]["decision"] == "long_signal"


def test_inspect_since_filter(tmp_path):
    path = _write_journal(tmp_path)
    rows = inspect_decisions.load_decisions(path)
    since = inspect_decisions.filter_decisions(rows, since="2026-05-28T22:10:00+00:00")
    assert [r["decision"] for r in since] == ["noop_regime", "long_signal"]


def test_inspect_around_window_filter(tmp_path):
    path = _write_journal(tmp_path)
    rows = inspect_decisions.load_decisions(path)
    around = inspect_decisions.filter_decisions(
        rows, around="2026-05-28T22:05:00+00:00", window_min=1.0
    )
    assert [r["decision"] for r in around] == ["noop_filter"]


def test_inspect_skips_malformed_lines(tmp_path):
    p = tmp_path / "decisions.jsonl"
    p.write_text(
        json.dumps(_sample("2026-05-28T22:00:00+00:00")) + "\n"
        "this is not json\n"
        + json.dumps(_sample("2026-05-28T22:01:00+00:00", decision="long_signal"))
        + "\n"
    )
    rows = inspect_decisions.load_decisions(str(p))
    assert len(rows) == 2  # the garbage line is skipped, not fatal


def test_inspect_missing_file_returns_empty():
    assert inspect_decisions.load_decisions("/nonexistent/decisions.jsonl") == []
    assert inspect_decisions.render([]) == "(no decisions in range)"
