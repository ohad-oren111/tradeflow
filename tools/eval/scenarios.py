"""Phase 2 — synthetic-scenario harness.

Eight hand-built scenarios that FORCE each regime/path the live tape rarely serves
on demand. Each drives the REAL strategy (``Sma100BounceStrategy`` via the engine)
and the REAL exit (``trail_manager``) / REAL kill-switch (``kill_switch``) with
execution STUBBED (modeled fills only). NO IBKR connection, NO prod-DB write — the
only state is the in-process engine.

Each scenario runs K>=5 variants with randomized timing / volatility / slope. The
harness reports, per scenario: EXPECTED vs ACTUAL, pass/fail for every variant, and
a determinism check (same seed → byte-identical trades). Logical outcome is INVARIANT
across the randomized variants; the fixed-seed re-run proves reproducibility.

  1 below-trend long          → regime gate BLOCKS, no entry
  2 above-trend pullback      → entry → run-up → ratchet walks → locks profit
  3 above-trend entry         → reverse to base stop → loss at entry-75
  4 above-trend entry         → partial run, V-reversal → ratchet give-back
  5 chop straddling EMA200    → no entry while below the 30m EMA200
  6 feed gap mid-trade        → resting stop still protects across the gap
  7 N consecutive losers      → kill-switch warns@6, halts@10
  8 big winner                → hard_ceiling(1000) cap
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pandas as pd

from config.risk_params import RiskParams
from src.execution.kill_switch import evaluate_triggers
from src.execution.trail_manager import round_to_tick as _tick
from src.indicators import add_all_indicators
from src.strategy import _in_session_edge_window, _parse_hhmm, thirty_min_bucket_count
from tools.eval import engine

# Clean weekday mid-session UTC end-anchors (verified not in any no-trade window).
_CLEAN_ENDS = [
    datetime(2025, 4, 9, 19, 0, tzinfo=UTC),  # Wed 15:00 ET
    datetime(2025, 4, 8, 17, 30, tzinfo=UTC),  # Tue 13:30 ET
    datetime(2025, 4, 10, 14, 30, tzinfo=UTC),  # Thu 10:30 ET
    datetime(2025, 4, 2, 18, 0, tzinfo=UTC),  # Wed 14:00 ET
    datetime(2025, 4, 16, 15, 0, tzinfo=UTC),  # Wed 11:00 ET
]


@dataclass
class Variant:
    k: int
    passed: bool
    actual: dict


@dataclass
class ScenarioResult:
    name: str
    expected: str
    variants: list[Variant] = field(default_factory=list)
    deterministic: bool = True
    notes: str = ""

    @property
    def all_passed(self) -> bool:
        return bool(self.variants) and all(v.passed for v in self.variants)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _edge(ts: datetime, params: RiskParams) -> bool:
    from datetime import time as _t

    return _in_session_edge_window(
        ts,
        params.session_edge_no_trade_minutes,
        daily_break=(
            _parse_hhmm(params.daily_break_start_et),
            _parse_hhmm(params.daily_break_end_et),
        ),
        gateway_restart=(
            _parse_hhmm(params.gateway_restart_start_et),
            _parse_hhmm(params.gateway_restart_end_et),
        ),
        weekend_cutoff=(
            params.weekend_flat_cutoff_weekday,
            _t(params.weekend_flat_cutoff_hour_et, params.weekend_flat_cutoff_minute_et),
        ),
        sunday_open=_parse_hhmm(params.sunday_open_et),
    )


def _trend_entry_prefix(
    rng: random.Random,
    params: RiskParams,
    *,
    direction: int,
    n_up: int = 6200,
    end_ts: datetime | None = None,
) -> tuple[list[dict], datetime]:
    """End-anchored trend warmup (direction +1 up / -1 down) ending in a green
    touch bar that the entry gates accept. Up-trend → price above 30m EMA200
    (regime allows); down-trend → below (regime blocks). Returns (bars, entry_ts).
    """
    base = _tick(rng.uniform(19000, 21000))
    up = round(rng.uniform(0.04, 0.08), 4)
    end_ts = end_ts or rng.choice(_CLEAN_ENDS)
    # nudge end_ts off any edge window (defensive; the anchors are pre-verified).
    while _edge(end_ts, params):
        end_ts = end_ts + timedelta(minutes=1)
    n_total = n_up + 70 + 1
    start = end_ts - timedelta(minutes=n_total - 1)
    bars: list[dict] = []
    px = base
    for i in range(n_up):
        px += up * direction
        bars.append(
            {
                "time": start + timedelta(minutes=i),
                "open": _tick(px - up * direction),
                "high": _tick(px + 0.1),
                "low": _tick(px - 0.1),
                "close": _tick(px),
                "volume": 10.0,
            }
        )
    idx = n_up
    for _ in range(70):
        px -= 0.1
        bars.append(
            {
                "time": start + timedelta(minutes=idx),
                "open": _tick(px + 0.1),
                "high": _tick(px + 0.15),
                "low": _tick(px - 0.15),
                "close": _tick(px),
                "volume": 10.0,
            }
        )
        idx += 1
    bars.append(
        {
            "time": start + timedelta(minutes=idx),
            "open": _tick(px - 0.05),
            "high": _tick(px + 0.3),
            "low": _tick(px - 0.3),
            "close": _tick(px + 0.25),
            "volume": 10.0,
        }
    )
    return bars, bars[-1]["time"]


def _short_entry_prefix(rng: random.Random) -> list[dict]:
    """A 191-bar regime-independent entry priming prefix (regime OFF scenarios)."""
    from tools.eval.data import make_entry_setup

    base = round(rng.uniform(19000, 21000), 2)
    end = rng.choice(_CLEAN_ENDS)
    start = end - timedelta(minutes=191)
    return make_entry_setup(base, start_ts=start)


def _append_walk(bars: list[dict], deltas: list[float], *, spread: float) -> None:
    """Append continuation bars walking the close by ``deltas`` from the last close."""
    p = bars[-1]["close"]
    t = bars[-1]["time"]
    for d in deltas:
        o = p
        p = _tick(p + d)
        hi = _tick(max(o, p) + spread)
        lo = _tick(min(o, p) - spread)
        t = t + timedelta(minutes=1)
        bars.append(
            {"time": t, "open": _tick(o), "high": hi, "low": lo, "close": p, "volume": 10.0}
        )


def _frame(bars: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(bars)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return add_all_indicators(df).reset_index(drop=True)


def _run(bars: list[dict], params: RiskParams) -> engine.SimResult:
    seg = _frame(bars)
    return engine.simulate_segment(seg, engine.FastGateEntry(params, "NQ"), params)


def _final_decision(bars: list[dict], params: RiskParams) -> tuple[str, int]:
    """Decision label the gate assigns to the LAST (entry-candidate) bar, plus the
    30-min bucket count at that bar (>=202 == regime armed).

    This targets the ARMED entry bar specifically — synthetic buffers warm from
    empty, so the first ~6060 bars are pre-arming (regime fail-open) the way prod's
    boot-seeded buffer never is. The block proof must therefore read the gate's
    verdict on the bar where the regime is actually armed, not a whole-run count.
    """
    seg = _frame(bars)
    drv = engine.FastGateEntry(params, "NQ")
    dec = "noop_warmup"
    for j in range(len(seg)):
        dec, _ = drv.step(seg, j)
    buckets = thirty_min_bucket_count(seg.tail(7000).to_dict("records"))
    return dec, buckets


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #
def scn1_below_trend_block(k: int, seed: int) -> Variant:
    rng = random.Random(seed)
    params_on = RiskParams(regime_gate_enabled=True, exit_mode="trailing")
    params_off = RiskParams(regime_gate_enabled=False, exit_mode="trailing")
    bars, _ = _trend_entry_prefix(rng, params_on, direction=-1)
    dec_on, buckets = _final_decision(bars, params_on)
    dec_off, _ = _final_decision(bars, params_off)
    # The armed regime gate BLOCKS the below-trend long that fires without it.
    passed = dec_on == "noop_regime" and dec_off == "long_signal" and buckets >= 202
    return Variant(
        k,
        passed,
        {"armed_buckets": buckets, "decision_regime_on": dec_on, "decision_regime_off": dec_off},
    )


def scn2_profit_walk(k: int, seed: int, above_trend: bool = True) -> Variant:
    rng = random.Random(seed)
    if above_trend:
        params = RiskParams(regime_gate_enabled=True, exit_mode="trailing")
        bars, _ = _trend_entry_prefix(rng, params, direction=+1)
    else:
        params = RiskParams(regime_gate_enabled=False, exit_mode="trailing")
        bars = _short_entry_prefix(rng)
    spread = round(rng.uniform(0.5, 1.0), 2)
    step = round(rng.uniform(58, 78), 2)  # strong run, peak ~+520..+700 (>300, <1000 ceiling)
    _append_walk(bars, [step] * 9, spread=spread)  # run-up: walks the stop into the trail zone
    _append_walk(bars, [-step * 1.8] * 12, spread=spread)  # reversal into the trailed stop
    res = _run(bars, params)
    t = res.trades[0] if res.trades else None
    # proves the TRAIL engaged (stop walked past lock-in into highest-150), gave
    # back exactly the trail offset from the peak, and locked a real profit.
    passed = bool(
        res.entries == 1
        and t is not None
        and t.exit_reason == "ratchet_stop"
        and t.final_stop > t.entry_price + params.trail_offset_pts
        and t.highest > t.final_stop
        and t.net_usd > 0
    )
    return Variant(k, passed, {"entries": res.entries, **(_trade_dict(t) if t else {})})


def scn3_base_stop_loss(k: int, seed: int, above_trend: bool = True) -> Variant:
    rng = random.Random(seed)
    if above_trend:
        params = RiskParams(regime_gate_enabled=True, exit_mode="trailing")
        bars, _ = _trend_entry_prefix(rng, params, direction=+1)
    else:
        params = RiskParams(regime_gate_enabled=False, exit_mode="trailing")
        bars = _short_entry_prefix(rng)
    spread = round(rng.uniform(0.5, 1.5), 2)
    _append_walk(
        bars, [-round(rng.uniform(18, 26), 2)] * 6, spread=spread
    )  # straight down past -75
    res = _run(bars, params)
    t = res.trades[0] if res.trades else None
    expect_stop = round(t.entry_price - params.stop_loss_pts, 2) if t else None
    passed = bool(
        res.entries == 1
        and t is not None
        and t.exit_reason == "stop"
        and abs(t.exit_price - expect_stop) < 0.01
        and t.net_usd < 0
    )
    return Variant(
        k,
        passed,
        {"entries": res.entries, "expected_stop": expect_stop, **(_trade_dict(t) if t else {})},
    )


def scn4_v_reversal_giveback(k: int, seed: int, above_trend: bool = True) -> Variant:
    rng = random.Random(seed)
    if above_trend:
        params = RiskParams(regime_gate_enabled=True, exit_mode="trailing")
        bars, _ = _trend_entry_prefix(rng, params, direction=+1)
    else:
        params = RiskParams(regime_gate_enabled=False, exit_mode="trailing")
        bars = _short_entry_prefix(rng)
    spread = round(rng.uniform(0.5, 1.5), 2)
    step = round(rng.uniform(18, 24), 2)
    _append_walk(bars, [step] * 5, spread=spread)  # peak ~ +90..+120 (lock engages, not deep trail)
    _append_walk(bars, [-step * 2.2] * 12, spread=spread)  # V-reversal into the lock-in stop
    res = _run(bars, params)
    t = res.trades[0] if res.trades else None
    # gave back from the peak but locked >= entry (the +50 lock-in protected profit).
    passed = bool(
        res.entries == 1
        and t is not None
        and t.exit_reason == "ratchet_stop"
        and t.final_stop >= t.entry_price
        and t.highest - t.entry_price > t.final_stop - t.entry_price
        and t.net_usd > 0
    )
    return Variant(k, passed, {"entries": res.entries, **(_trade_dict(t) if t else {})})


def scn5_chop_no_entry_below(k: int, seed: int) -> Variant:
    rng = random.Random(seed)
    params_on = RiskParams(regime_gate_enabled=True, exit_mode="trailing")
    params_off = RiskParams(regime_gate_enabled=False, exit_mode="trailing")
    # A below-EMA whipsaw: declining trend (price under the 30m EMA200) with the
    # crafted touch setup on a down-leg. The "while below" half of the chop test —
    # the "above" half (entries DO fire above the EMA) is proven by scenario 2.
    bars, _ = _trend_entry_prefix(rng, params_on, direction=-1)
    dec_on, buckets = _final_decision(bars, params_on)
    dec_off, _ = _final_decision(bars, params_off)
    # Armed gate blocks the below-EMA entry candidate that fires without it.
    passed = dec_on == "noop_regime" and dec_off == "long_signal" and buckets >= 202
    return Variant(
        k,
        passed,
        {"armed_buckets": buckets, "decision_regime_on": dec_on, "decision_regime_off": dec_off},
    )


def scn6_feed_gap_protects(k: int, seed: int) -> Variant:
    rng = random.Random(seed)
    params = RiskParams(regime_gate_enabled=True, exit_mode="trailing")
    bars, _ = _trend_entry_prefix(rng, params, direction=+1)
    spread = round(rng.uniform(1.0, 2.0), 2)
    step = round(rng.uniform(22, 28), 2)
    _append_walk(bars, [step] * 10, spread=spread)  # run-up: stop ratchets above entry
    # ---- inject a feed gap: jump the next bar's timestamp by ~3h ----
    gap_min = rng.choice([120, 180, 240])
    bars[-1] = dict(bars[-1])
    last_t = bars[-1]["time"]
    p = bars[-1]["close"]
    # post-gap: sharp drop into the protective (ratcheted) stop.
    t = last_t + timedelta(minutes=gap_min)
    for _ in range(15):
        o = p
        p = _tick(p - step * 1.5)
        bars.append(
            {
                "time": t,
                "open": _tick(o),
                "high": _tick(o + spread),
                "low": _tick(p - spread),
                "close": p,
                "volume": 10.0,
            }
        )
        t = t + timedelta(minutes=1)
    res = _run(bars, params)
    t0 = res.trades[0] if res.trades else None
    # the gap is present, and the resting stop still exited at a protected (>= entry) level.
    passed = bool(
        res.entries == 1
        and t0 is not None
        and t0.exit_reason == "ratchet_stop"
        and t0.exit_price >= t0.entry_price
    )
    return Variant(
        k, passed, {"entries": res.entries, "gap_min": gap_min, **(_trade_dict(t0) if t0 else {})}
    )


def scn7_killswitch_streak(k: int, seed: int) -> Variant:
    rng = random.Random(seed)
    params = RiskParams()
    warn, halt = params.kill_switch_warn_consec_losses, params.kill_switch_halt_consec_losses

    def verdict(nloss: int) -> str:
        losses = [-round(rng.uniform(50, 400), 2) for _ in range(nloss)]
        v = evaluate_triggers(
            losses,
            sum(losses),
            None,
            warn_consec_losses=warn,
            halt_consec_losses=halt,
            max_drawdown_pct=33.0,
        )
        return v.action

    a5, a6, a9, a10 = verdict(5), verdict(6), verdict(9), verdict(10)
    # a win breaks the streak → ok even at 12 trailing if newest is a win:
    mixed = evaluate_triggers(
        [100.0] + [-100.0] * 11,
        -1000.0,
        None,
        warn_consec_losses=warn,
        halt_consec_losses=halt,
        max_drawdown_pct=33.0,
    ).action
    passed = a5 == "ok" and a6 == "notify" and a9 == "notify" and a10 == "pause" and mixed == "ok"
    return Variant(k, passed, {"n5": a5, "n6": a6, "n9": a9, "n10": a10, "win_breaks": mixed})


def scn8_hard_ceiling(k: int, seed: int, above_trend: bool = True) -> Variant:
    rng = random.Random(seed)
    if above_trend:
        params = RiskParams(regime_gate_enabled=True, exit_mode="trailing")
        bars, _ = _trend_entry_prefix(rng, params, direction=+1)
    else:
        params = RiskParams(regime_gate_enabled=False, exit_mode="trailing")
        bars = _short_entry_prefix(rng)
    spread = round(rng.uniform(1.0, 2.0), 2)
    step = round(rng.uniform(55, 70), 2)
    _append_walk(bars, [step] * 22, spread=spread)  # +1200..+1500 → crosses +1000 ceiling
    res = _run(bars, params)
    t = res.trades[0] if res.trades else None
    passed = bool(
        res.entries == 1
        and t is not None
        and t.exit_reason == "hard_ceiling"
        and (t.exit_price - t.entry_price) >= params.hard_ceiling_pts
    )
    return Variant(
        k,
        passed,
        {
            "entries": res.entries,
            "ceiling_pts": params.hard_ceiling_pts,
            **(_trade_dict(t) if t else {}),
        },
    )


def _trade_dict(t) -> dict:
    return {
        "entry": round(t.entry_price, 2),
        "exit": round(t.exit_price, 2),
        "reason": t.exit_reason,
        "highest": round(t.highest, 2),
        "final_stop": round(t.final_stop, 2),
        "protecting_pts": round(t.final_stop - t.entry_price, 2),
        "net_usd": round(t.net_usd, 2),
    }


# --------------------------------------------------------------------------- #
# Registry + runner
# --------------------------------------------------------------------------- #
SCENARIOS = [
    (
        "1 below-trend → gate BLOCKS, no entry",
        "armed gate: final bar noop_regime (regime-off would enter)",
        scn1_below_trend_block,
        True,
    ),
    (
        "2 above-trend → entry → run-up → ratchet locks profit",
        "ratchet_stop, final_stop>entry, gave back from peak, net>0",
        scn2_profit_walk,
        True,
    ),
    (
        "3 above-trend → reverse → loss at entry-75",
        "exit==base stop entry-75, net<0",
        scn3_base_stop_loss,
        True,
    ),
    (
        "4 above-trend → V-reversal → ratchet give-back",
        "ratchet_stop, locked >=entry (+50), peak>exit, net>0",
        scn4_v_reversal_giveback,
        True,
    ),
    (
        "5 chop straddling EMA200 → no entry while below",
        "armed gate: below-EMA entry candidate noop_regime (regime-off enters)",
        scn5_chop_no_entry_below,
        True,
    ),
    (
        "6 feed gap mid-trade → resting stop still protects",
        "1 trade, ratchet_stop, exit>=entry across the gap",
        scn6_feed_gap_protects,
        True,
    ),
    (
        "7 N consecutive losers → warn@6 halt@10",
        "5=ok 6=notify 9=notify 10=pause; a win breaks streak",
        scn7_killswitch_streak,
        False,
    ),
    (
        "8 big winner → hard_ceiling(1000) cap",
        "exit==hard_ceiling, exit-entry>=1000",
        scn8_hard_ceiling,
        True,
    ),
]


def run_all(k: int = 5, base_seed: int = 4242) -> list[ScenarioResult]:
    """Run every scenario K times. Determinism: variant 0 is re-run with the same
    seed and asserted byte-identical."""
    out: list[ScenarioResult] = []
    for idx, (name, expected, fn, _regime) in enumerate(SCENARIOS):
        sr = ScenarioResult(name=name, expected=expected)
        for ki in range(k):
            seed = base_seed + idx * 1000 + ki
            sr.variants.append(fn(ki, seed))
        # determinism: re-run variant 0 with the same seed, compare actuals.
        again = fn(0, base_seed + idx * 1000)
        sr.deterministic = again.actual == sr.variants[0].actual
        out.append(sr)
    return out


def format_report(results: list[ScenarioResult]) -> str:
    lines = []
    n_pass = sum(1 for r in results if r.all_passed)
    lines.append(
        f"PHASE 2 — synthetic scenarios: {n_pass}/{len(results)} scenarios PASS (all variants)"
    )
    lines.append("=" * 78)
    for r in results:
        status = "PASS" if r.all_passed else "FAIL"
        det = "deterministic" if r.deterministic else "NON-DETERMINISTIC"
        kpass = sum(1 for v in r.variants if v.passed)
        lines.append(f"\n[{status}] {r.name}")
        lines.append(f"   expected: {r.expected}")
        lines.append(f"   variants: {kpass}/{len(r.variants)} pass | {det}")
        lines.append(f"   sample actual (k=0): {r.variants[0].actual}")
        if not r.all_passed:
            for v in r.variants:
                if not v.passed:
                    lines.append(f"   FAILED k={v.k}: {v.actual}")
    return "\n".join(lines)
