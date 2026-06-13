"""Tests for src.journal_rotation — size-based JSONL rotation (W-S14.2 Track 6b)."""

from __future__ import annotations

from src.journal_rotation import rotate_jsonl_if_large


def test_no_rotation_below_threshold(tmp_path):
    p = tmp_path / "decisions.jsonl"
    p.write_text("x\n")
    assert rotate_jsonl_if_large(p, max_bytes=1024) is False
    assert p.exists()
    assert not (tmp_path / "decisions.jsonl.1").exists()


def test_rotation_at_or_above_threshold(tmp_path):
    p = tmp_path / "decisions.jsonl"
    p.write_text("a" * 2048)
    assert rotate_jsonl_if_large(p, max_bytes=1024) is True
    # original rolled to the single .1 backup; live path is now gone (recreated
    # by the next append in the caller).
    assert not p.exists()
    backup = tmp_path / "decisions.jsonl.1"
    assert backup.exists()
    assert backup.read_text() == "a" * 2048


def test_rotation_replaces_existing_backup(tmp_path):
    p = tmp_path / "reconciliations.jsonl"
    backup = tmp_path / "reconciliations.jsonl.1"
    backup.write_text("OLD")
    p.write_text("b" * 2048)
    assert rotate_jsonl_if_large(p, max_bytes=1024) is True
    assert backup.read_text() == "b" * 2048  # prior backup overwritten, one kept


def test_missing_file_is_noop(tmp_path):
    assert rotate_jsonl_if_large(tmp_path / "absent.jsonl", max_bytes=1) is False


def test_record_decision_rotates_when_large(tmp_path, monkeypatch):
    """_record_decision triggers rotation once the journal exceeds the threshold."""
    import src.orchestrator as orch_mod

    path = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(orch_mod, "_DECISION_JSONL_PATH", str(path))
    monkeypatch.setattr(orch_mod, "rotate_jsonl_if_large", lambda p: _tiny_rotate(p))

    orch = orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)
    from collections import deque

    orch._decision_journal = deque(maxlen=10)
    # Pre-fill the journal file past the tiny threshold.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("c" * 4096)
    orch._record_decision({"ts": "2026-05-29T15:00:00+00:00", "decision": "long_signal"})
    # rotation moved the big file aside; the new append created a fresh small one.
    assert (tmp_path / "decisions.jsonl.1").exists()
    assert path.exists() and path.stat().st_size < 4096


def _tiny_rotate(p):
    from src.journal_rotation import rotate_jsonl_if_large as _r

    return _r(p, max_bytes=1024)
