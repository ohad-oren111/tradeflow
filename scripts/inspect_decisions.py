"""Replay / inspect the strategy decision journal (Track 3 — "test what the bot did").

Reads the JSONL decision journal that the orchestrator appends one line per bar
(``/app/logs/decisions.jsonl`` inside the container) and renders the decisions
human-readably, one row per bar with all gate values and the failing filter
highlighted. This is the operator's after-the-fact "why didn't the bot enter?"
tool — no log grepping or replay of the strategy required.

Usage (inside the container, or pointed at a copied-out file):

    python -m scripts.inspect_decisions --last 20
    python -m scripts.inspect_decisions --since 2026-05-28T22:00:00
    python -m scripts.inspect_decisions --around 2026-05-28T22:15:00 --window 5
    python -m scripts.inspect_decisions --file /tmp/decisions.jsonl --last 50

From the host:

    docker exec tradeflow-app python -m scripts.inspect_decisions --last 20
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta

_DEFAULT_PATH = "/app/logs/decisions.jsonl"


def load_decisions(path: str) -> list[dict]:
    """Read the JSONL journal into a list of decision dicts.

    Malformed lines are skipped (best-effort) so one bad write never blanks the
    whole replay. Returns [] if the file is missing.
    """
    rows: list[dict] = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return []
    return rows


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def filter_decisions(
    rows: list[dict],
    *,
    since: str | None = None,
    around: str | None = None,
    window_min: float = 5.0,
    last: int | None = None,
) -> list[dict]:
    """Apply --since / --around+--window / --last filters in that precedence."""
    out = rows
    since_dt = _parse_ts(since)
    around_dt = _parse_ts(around)
    if since_dt is not None:
        out = [r for r in out if (_parse_ts(r.get("ts")) or datetime.min) >= since_dt]
    if around_dt is not None:
        lo = around_dt - timedelta(minutes=window_min)
        hi = around_dt + timedelta(minutes=window_min)
        out = [r for r in out if (ts := _parse_ts(r.get("ts"))) is not None and lo <= ts <= hi]
    if last is not None and last > 0:
        out = out[-last:]
    return out


def _fmt_gate(name: str, value: object, failed: str | None) -> str:
    """Render one gate; mark the failing filter with a '<<' arrow."""
    mark = "  <<FAILED" if failed is not None and name == failed else ""
    return f"{name}={value}{mark}"


def render(rows: list[dict]) -> str:
    """Render decisions one per line, newest decisions last (chronological)."""
    if not rows:
        return "(no decisions in range)"
    lines: list[str] = []
    for r in rows:
        failed = r.get("failed")
        gates = " ".join(
            _fmt_gate(g, r.get(g), failed)
            for g in ("regime_ok", "touch_ok", "ma_order_ok", "bullish_ok", "gap_ok")
        )
        failed_str = f" failed={failed}" if failed else ""
        lines.append(
            f"{r.get('ts')}  decision={r.get('decision')}{failed_str}  "
            f"close={r.get('close')} sma100={r.get('sma100')}  | {gates}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Inspect the strategy decision journal.")
    ap.add_argument("--file", default=_DEFAULT_PATH, help="JSONL path (default: %(default)s)")
    ap.add_argument("--since", help="UTC ISO ts — only decisions at/after this time")
    ap.add_argument("--around", help="UTC ISO ts — center of a +/- window")
    ap.add_argument("--window", type=float, default=5.0, help="minutes each side of --around")
    ap.add_argument("--last", type=int, help="only the last N decisions")
    args = ap.parse_args(argv)

    rows = load_decisions(args.file)
    rows = filter_decisions(
        rows, since=args.since, around=args.around, window_min=args.window, last=args.last
    )
    print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
