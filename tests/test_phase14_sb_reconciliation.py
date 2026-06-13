"""Phase-14 fill-reconciliation invariants — deterministic, no DB / no IBKR.

Pins the pure pieces the verdict rests on: FIFO round-trip pairing + clean tagging,
the price-delta + timestamp-shift scan, the real-EMA200 regime classification, the
re-pricing on the real tape (drives the REAL `_process_exit_bar`), the friction
math, and the pre-committed verdict bar — plus an end-to-end run on a synthetic
real tape long enough to warm the 30m-EMA200, forcing each verdict. Follows the
8 test-safety guardrails: hand-built tapes/telemetry, no schema mocks, no shared
mutable state, asserts the real return shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tools.eval.phase14_sb_reconciliation import (
    BUCKET_FRAC_BAR,
    DELTA_DIVERGENT_PT,
    SB_BRACKET,
    attribute_contract,
    build_round_trips,
    build_tape,
    classify_entry,
    decide_verdict,
    deltas_at_shift,
    distribution,
    net_usd,
    reprice_on_real_tape,
    run_study,
    shift_scan,
)

BASE = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)


def _iso(minutes: int) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat()


def _bar(minutes: int, close: float, *, high=None, low=None) -> dict:
    return {
        "time": _iso(minutes),
        "open": close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "volume": 1.0,
    }


def _entry(msg_id: int, minutes: int, price: float, contracts: int = 2) -> dict:
    return {
        "ts": _iso(minutes),
        "message_id": msg_id,
        "type": "entry",
        "direction": "long",
        "symbol": "MNQ",
        "price": price,
        "stop_price": price - 75.0,
        "target_price": price + 150.0,
        "pnl_points": None,
        "contracts": contracts,
        "parsed_ok": True,
    }


def _exit(minutes: int, pnl_points: float, price: float = 0.0) -> dict:
    return {
        "ts": _iso(minutes),
        "message_id": None,
        "type": "exit",
        "direction": None,
        "symbol": "MNQ",
        "price": price,
        "pnl_points": pnl_points,
        "contracts": 2,
        "parsed_ok": True,
    }


# --------------------------------------------------------------- distribution
def test_distribution_basic():
    d = distribution([1.0, 2.0, 3.0, 4.0, -1.0])
    assert d["n"] == 5
    assert d["median"] == 2.0
    assert d["frac_pos"] == pytest.approx(0.8)


def test_distribution_empty():
    d = distribution([])
    assert d["n"] == 0 and d["median"] is None


# ------------------------------------------------------------- round-trips
def test_round_trips_fifo_and_clean_tag():
    signals = [
        _entry(10, 0, 30000.0),
        _exit(20, -75.0, price=29925.0),
        _entry(11, 30, 30100.0),
        _exit(40, 41.0, price=30141.0),
    ]
    trips = build_round_trips(signals)
    assert len(trips) == 2
    assert all(t.clean for t in trips)
    assert trips[0].pnl_points == -75.0
    assert trips[0].sb_entry_px == 30000.0
    assert trips[1].pnl_points == 41.0


def test_round_trip_unpaired_entry_is_not_clean():
    # entry with no following exit => exit fields None => not clean (never dropped)
    trips = build_round_trips([_entry(10, 0, 30000.0)])
    assert len(trips) == 1
    assert trips[0].clean is False
    assert trips[0].exit_ts is None


def test_round_trip_back_to_back_entries_leave_first_unpaired():
    signals = [_entry(10, 0, 30000.0), _entry(11, 5, 30050.0), _exit(10, 10.0)]
    trips = build_round_trips(signals)
    assert len(trips) == 2
    assert trips[0].clean is False  # first entry never got an exit
    assert trips[1].clean is True


# ------------------------------------------------------- delta + shift scan
def test_delta_and_best_shift_recovers_offset():
    # real tape: close = 100 at minute 0, 110 at minute 1. SB posts 110 at ts in
    # minute 1, so delta at t-1 == 0 (SB posts the *just-closed prior* bar's close).
    bars = [_bar(0, 100.0), _bar(1, 110.0), _bar(2, 120.0)]
    tape = build_tape(bars)
    # SB entry timestamped in minute 2 but priced at minute-1 close (110).
    trips = build_round_trips([_entry(1, 2, 110.0), _exit(3, 5.0)])
    assert deltas_at_shift(trips, tape, 0) == [110.0 - 120.0]  # vs minute-2 close
    assert deltas_at_shift(trips, tape, -1) == [110.0 - 110.0]  # vs minute-1 close
    scan = shift_scan(trips, tape, shifts=(-1, 0, 1))
    assert scan["best_shift"] == -1  # |delta| minimized one bar back


# --------------------------------------------------------- classify_entry
def test_classify_buckets():
    assert classify_entry(100.0, 100.0, None) == "insufficient_history"
    assert classify_entry(110.0, 115.0, 105.0) == "i_legit_above"
    assert classify_entry(95.0, 115.0, 105.0) == "ii_divergent_feed"
    assert classify_entry(95.0, 100.0, 105.0) == "iii_gate_failed_open"
    assert classify_entry(110.0, 100.0, 105.0) == "iv_sb_below_real_above"


# ------------------------------------------------- contract attribution (roll)
def test_attribute_contract_resolves_roll():
    # June close 30000, Sept close 30200 (a ~200pt calendar carry). SB posts 30205
    # => attributed to Sept (nearer), delta 5pt — NOT a 205pt "divergent feed".
    june = build_tape([_bar(0, 30000.0)])
    sept = build_tape([_bar(0, 30200.0)])
    ts = datetime.fromisoformat(_iso(0))
    name, _idx, rc, delta = attribute_contract(ts, 30205.0, {"MNQM6": june, "MNQU6": sept}, shift=0)
    assert name == "MNQU6"
    assert rc == 30200.0
    assert delta == pytest.approx(5.0)


def test_attribute_contract_none_when_no_bar():
    june = build_tape([_bar(0, 30000.0)])
    ts = datetime.fromisoformat(_iso(500))  # no bar at that minute
    assert attribute_contract(ts, 30000.0, {"MNQM6": june}, shift=0) is None


# ------------------------------------------------------------- re-pricing
def test_reprice_hits_target_at_plus_150():
    # entry 100, tape rises through +150 (close 250). Bracket hard-ceiling fires.
    bars = [_bar(i, 100.0 + i) for i in range(0, 200)]  # 100 -> 299
    tape = build_tape(bars)
    res = reprice_on_real_tape(tape, 0, 100.0, SB_BRACKET)
    assert res is not None
    exit_px, reason, _bars = res
    assert reason == "hard_ceiling"
    assert exit_px >= 250.0  # first close at/above entry+150


def test_reprice_hits_protective_stop_at_minus_75():
    # entry 100, next bar low 24 (< entry-75 = 25) => stop exit at 25.
    bars = [_bar(0, 100.0), _bar(1, 90.0, high=90.0, low=24.0)]
    tape = build_tape(bars)
    res = reprice_on_real_tape(tape, 0, 100.0, SB_BRACKET)
    assert res is not None
    exit_px, reason, _bars = res
    assert exit_px == 25.0
    assert reason == "stop"


def test_reprice_unresolved_returns_none():
    bars = [_bar(0, 100.0), _bar(1, 100.0), _bar(2, 100.0)]
    tape = build_tape(bars)
    assert reprice_on_real_tape(tape, 0, 100.0, SB_BRACKET) is None


# --------------------------------------------------------------- friction
def test_net_usd_friction_model():
    # gross 0 pts, qty 2: net = -(slippage 1.0pt*$2*2) - (commission $1.24*2)
    expected = -(1.0 * 2.0 * 2) - (1.24 * 2)
    assert net_usd(0.0, 2) == pytest.approx(expected)


# --------------------------------------------------------------- verdict bar
def test_verdict_feed_divergent():
    v, _ = decide_verdict(median_abs_delta=15.0, frac_ii=0.7, frac_iii=0.1)
    assert v == "FEED-DIVERGENT"


def test_verdict_gate_inert():
    v, _ = decide_verdict(median_abs_delta=2.0, frac_ii=0.1, frac_iii=0.7)
    assert v == "GATE-INERT"


def test_verdict_lead_real():
    v, _ = decide_verdict(median_abs_delta=2.0, frac_ii=0.1, frac_iii=0.2)
    assert v == "LEAD-REAL"


def test_verdict_high_delta_but_low_ii_is_not_feed_divergent():
    # big delta but divergent-feed bucket not dominant -> NOT feed-divergent.
    v, _ = decide_verdict(median_abs_delta=20.0, frac_ii=0.3, frac_iii=0.2)
    assert v == "LEAD-REAL"


def test_verdict_bars_are_the_committed_constants():
    assert DELTA_DIVERGENT_PT == 10.0
    assert BUCKET_FRAC_BAR == 0.60


# ------------------------------------------------- end-to-end forced verdicts
# Place entries well past minute 6060 (>202 30-min buckets) so the EMA200 is warm.
_WARM_N = 7000
_ENTRY_MIN0 = 6600


def _warm_const_tape(price: float, n: int = _WARM_N) -> list[dict]:
    """A flat tape long enough (~116h) to warm the 30m-EMA200 (>=202 buckets)."""
    return [_bar(i, price) for i in range(n)]


def test_run_study_feed_divergent_end_to_end():
    # Flat real tape at 100 => EMA200 ~ 100, real_close 100 <= EMA (below). SB posts
    # 130 (>EMA) => bucket (ii) divergent feed, delta = +30 (>10) on every entry.
    bars = _warm_const_tape(100.0)
    signals: list[dict] = []
    for k in range(6):
        m = _ENTRY_MIN0 + k * 5
        signals.append(_entry(900 + k, m, 130.0))
        signals.append(_exit(m + 2, 10.0, price=140.0))
    study = run_study(signals, {"MNQM6": bars})
    assert study["classified"] >= 1
    assert study["frac_ii"] > BUCKET_FRAC_BAR
    assert study["delta_abs"]["median"] > DELTA_DIVERGENT_PT
    assert study["verdict"] == "FEED-DIVERGENT"


def test_run_study_gate_inert_end_to_end():
    # Flat tape at 100; SB posts 100 (== EMA, treated <= => below) => both below =>
    # bucket (iii) gate-failed-open, delta ~ 0 (small).
    bars = _warm_const_tape(100.0)
    signals: list[dict] = []
    for k in range(6):
        m = _ENTRY_MIN0 + k * 5
        signals.append(_entry(800 + k, m, 100.0))
        signals.append(_exit(m + 2, -5.0, price=95.0))
    study = run_study(signals, {"MNQM6": bars})
    assert study["classified"] >= 1
    assert study["frac_iii"] > BUCKET_FRAC_BAR
    assert study["delta_abs"]["median"] <= DELTA_DIVERGENT_PT
    assert study["verdict"] == "GATE-INERT"


def test_run_study_lead_real_end_to_end():
    # Rising tape: last close sits ABOVE the lagging EMA200; SB posts ~ real close
    # (small delta) => bucket (i) legit above-trend => LEAD-REAL.
    bars = [_bar(i, 100.0 + i * 0.05) for i in range(_WARM_N)]  # gentle ramp
    signals: list[dict] = []
    for k in range(6):
        m = _ENTRY_MIN0 + k
        px = 100.0 + m * 0.05
        signals.append(_entry(700 + k, m, px))
        signals.append(_exit(m + 2, 8.0, price=px + 8.0))
    study = run_study(signals, {"MNQM6": bars})
    assert study["classified"] >= 1
    assert study["bucket_counts"].get("i_legit_above", 0) >= 1
    assert study["verdict"] == "LEAD-REAL"
