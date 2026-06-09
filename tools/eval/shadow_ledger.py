"""Phase-10 (research, OFFLINE, ADDITIVE) — SeanBot shadow ledger.

Answers ONE question from telemetry that ALREADY exists: **is TradeFlow's regime
gate (and its blind-feed no-bar gaps) too strict versus SeanBot's actual realized
edge?** For every SB LONG entry that TF classified ``MISS-regime`` or
``MISS-NO-BAR`` (i.e. the gate / a feed gap BLOCKED it), we pair the SB entry with
SB's OWN realized exit and tally the realized P&L, win rate, and profit factor of
the trades the gate denied. A gate that blocks losers is doing its job; a gate that
blocks a positive-PF cluster is too strict.

Why this matters (§0.5.220 — never default to "we wait"): the pre-committed live
go/no-go bar (PF>1.2 over 20-30 above-trend trades) takes 6-12 months to fill.
SB posts every trade (wins AND losses) to Telegram in real time, and TF already
captures them — so the gate's opportunity cost accrues a verdict in WEEKS, not
months, off data we are already storing. This tool is the running scorecard.

Sources (READ-ONLY, no live-bot path touched):
  * ``signal_reconciliations`` — TF's per-SB-entry classification
    (AGREE_ENTER / MISS-regime / MISS-NO-BAR / MISS-filter:* / ...), keyed by the
    entry's Telegram ``message_id`` + ``signal_ts``.
  * ``seanbot_signals`` — the raw SB stream; ``type='exit'`` rows carry
    ``pnl_points`` (signed, realized). Entry->exit pairing is FIFO by ``ts``
    (SB has no parent_id; SB is single-position long-only, so FIFO is exact).

CAVEATS (loud, never hidden):
  * n is SMALL and GROWING — this is forward capture, not a backtest. Read it as a
    running tally with a loud noise flag, not a verdict, until n is meaningful.
  * Realized P&L uses SB's reported ``pnl_points`` as the point move, converted at
    the §0.5.97 MNQ multiplier; net applies the SAME friction model as the eval
    engine. WR and PF are sizing-invariant; $ figures are reported at TF's sizing
    (``--qty``, default 1 contract — what TF's gate actually cost), with SB's 2x
    noted.
  * An SB entry with no captured exit yet (still open / capture gap) is counted as
    UNPAIRED and excluded from WR/PF — never silently dropped.

  python -m tools.eval.shadow_ledger [--qty 1] [--from-json] [--out /tmp/shadow_<date>.txt]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from config.instruments import MNQ
from tools.eval.engine import DEFAULT_FRICTION_PTS
from tools.eval.metrics import Stats, compute_stats

# The two classifications this study scores: the regime gate block and the
# blind-feed no-bar miss. MISS-filter:* are strategy-filter misses (a different
# question); they are shown as CONTEXT in the report but not scored here.
BLOCKED_CLASSES: tuple[str, ...] = ("MISS-regime", "MISS-NO-BAR")

SB_SIGNALS_JSON = "/tmp/tf_sb_signals.json"
RECONCILIATIONS_JSON = "/tmp/tf_reconciliations.json"


@dataclass(frozen=True)
class ShadowTrade:
    """A BLOCKED SB entry paired with SB's realized exit. ``net_usd`` is read by
    ``metrics.compute_stats`` (win = net_usd > 0), so WR/PF/expectancy come from the
    same math as the rest of the eval kit."""

    entry_ts: datetime
    message_id: int | None
    classification: str
    pnl_points: float
    qty: int
    gross_usd: float
    net_usd: float  # friction-model net (DEFAULT_FRICTION_PTS) — primary
    net_usd_commission_only: float  # code-exact (matches lifecycles.pnl_net)

    @property
    def is_win(self) -> bool:
        return self.net_usd > 0


def _parse_ts(value: str) -> datetime:
    """Parse a Supabase ISO timestamp to an aware UTC datetime."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _net_from_points(pnl_points: float, qty: int) -> tuple[float, float, float]:
    """(gross_usd, net_friction, net_commission_only) for a realized point move,
    mirroring ``engine._pnl`` exactly so figures are comparable to the backtest."""
    gross_usd = pnl_points * MNQ.multiplier * qty
    net_friction = gross_usd - DEFAULT_FRICTION_PTS * MNQ.multiplier * qty
    net_commission = gross_usd - qty * MNQ.commission_rt_usd
    return gross_usd, net_friction, net_commission


def pair_entries_to_exits(signals: list[dict]) -> dict[int, float]:
    """FIFO-pair SB entries to exits and return ``{entry_message_id: pnl_points}``.

    SB is single-position long-only, so the realized exit for an entry is simply
    the next ``type='exit'`` in time order. ``stop_moved`` rows are ignored.
    Entries still open at the end (no matching exit) are absent from the result
    (caller treats them as UNPAIRED). Pure — no I/O.
    """
    ordered = sorted(
        (s for s in signals if s.get("type") in ("entry", "exit")),
        key=lambda s: _parse_ts(s["ts"]),
    )
    realized: dict[int, float] = {}
    open_entry: dict | None = None
    for sig in ordered:
        if sig["type"] == "entry":
            # A new entry while one is already open (no exit seen) — SB is
            # single-position, so the prior entry's exit was simply not captured;
            # leave it UNPAIRED and track the newer one.
            open_entry = sig
        elif sig["type"] == "exit":
            pnl = sig.get("pnl_points")
            if open_entry is not None and pnl is not None:
                mid = open_entry.get("message_id")
                if mid is not None:
                    realized[int(mid)] = float(pnl)
            open_entry = None
    return realized


def build_shadow_trades(
    reconciliations: list[dict],
    realized_by_msgid: dict[int, float],
    *,
    qty: int = 1,
    classes: tuple[str, ...] = BLOCKED_CLASSES,
) -> tuple[list[ShadowTrade], list[dict]]:
    """Build ShadowTrades for BLOCKED entries that have a realized exit.

    Returns ``(trades, unpaired)`` where ``unpaired`` is the list of blocked
    reconciliation rows that have no captured SB exit yet (reported, never
    dropped). Pure — no I/O.
    """
    trades: list[ShadowTrade] = []
    unpaired: list[dict] = []
    for rec in reconciliations:
        if rec.get("classification") not in classes:
            continue
        mid_raw = rec.get("message_id")
        mid = int(mid_raw) if mid_raw is not None else None
        pnl = realized_by_msgid.get(mid) if mid is not None else None
        if pnl is None:
            unpaired.append(rec)
            continue
        gross, net_fric, net_comm = _net_from_points(pnl, qty)
        trades.append(
            ShadowTrade(
                entry_ts=_parse_ts(rec["signal_ts"]),
                message_id=mid,
                classification=rec["classification"],
                pnl_points=pnl,
                qty=qty,
                gross_usd=gross,
                net_usd=net_fric,
                net_usd_commission_only=net_comm,
            )
        )
    return trades, unpaired


def scorecard(
    reconciliations: list[dict],
    signals: list[dict],
    *,
    qty: int = 1,
) -> dict:
    """Full shadow-ledger scorecard. Returns a JSON-able dict with per-class and
    combined Stats, the gross classification breakdown, and pairing counts.
    Pure — no I/O."""
    realized = pair_entries_to_exits(signals)
    trades, unpaired = build_shadow_trades(reconciliations, realized, qty=qty)

    by_class: dict[str, Stats] = {}
    for cls in BLOCKED_CLASSES:
        cls_trades = [t for t in trades if t.classification == cls]
        by_class[cls] = compute_stats(cls_trades)

    breakdown: dict[str, int] = {}
    for rec in reconciliations:
        c = rec.get("classification") or "UNKNOWN"
        breakdown[c] = breakdown.get(c, 0) + 1

    combined = compute_stats(trades)
    return {
        "qty": qty,
        "classification_breakdown": dict(sorted(breakdown.items())),
        "blocked_total": sum(breakdown.get(c, 0) for c in BLOCKED_CLASSES),
        "paired": len(trades),
        "unpaired": len(unpaired),
        "by_class": {c: s.as_dict() for c, s in by_class.items()},
        "combined": combined.as_dict(),
        "_trades": trades,
        "_unpaired_rows": unpaired,
    }


def _verdict(combined: Stats, n: int) -> str:
    """One-line read of whether the gate looks too strict. Honest about small n."""
    if n == 0:
        return "NO DATA — no blocked SB entry has a captured realized exit yet."
    noise = " (NOISE: n<10 — directional only, not a verdict)" if n < 10 else ""
    pf = combined.profit_factor
    pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
    if combined.net_usd > 0 and (pf == float("inf") or pf >= 1.2):
        return (
            f"GATE MAY BE TOO STRICT{noise}: blocked signals realized +"
            f"${combined.net_usd:.0f} at PF {pf_str} — SB monetized what the gate denied."
        )
    if combined.net_usd <= 0:
        return (
            f"GATE LOOKS CORRECT{noise}: blocked signals realized "
            f"${combined.net_usd:.0f} at PF {pf_str} — the gate filtered net losers."
        )
    return (
        f"INCONCLUSIVE{noise}: blocked signals net +${combined.net_usd:.0f} "
        f"at PF {pf_str} (positive but sub-1.2) — keep accumulating."
    )


def format_report(result: dict, *, generated_at: datetime) -> str:
    qty = result["qty"]
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("SeanBot SHADOW LEDGER — Phase 10 (forward capture; the gate's opportunity cost)")
    lines.append(f"generated: {generated_at.isoformat()}  |  sizing: qty={qty} contract(s)")
    lines.append("=" * 78)
    lines.append("")
    lines.append("Question: is TF's regime gate / blind-feed no-bar miss too strict vs SB's")
    lines.append("realized edge? We score the SB entries TF classified MISS-regime / MISS-NO-BAR")
    lines.append("against SB's OWN realized exits (READ-ONLY telemetry already captured).")
    lines.append("")
    lines.append("Full classification breakdown (all captured SB entries):")
    for cls, cnt in result["classification_breakdown"].items():
        tag = "  <- SCORED" if cls in BLOCKED_CLASSES else ""
        lines.append(f"    {cls:<22} {cnt:>4}{tag}")
    lines.append("")
    lines.append(
        f"Blocked (regime/no-bar): {result['blocked_total']}  |  "
        f"paired w/ realized exit: {result['paired']}  |  "
        f"unpaired (no exit yet): {result['unpaired']}"
    )
    lines.append("")
    lines.append("Per-class realized scorecard (BLOCKED signals, at SB's realized exit):")
    header = (
        f"  {'class':<14} {'n':>3} {'win%':>6} {'expect$':>9} "
        f"{'PF':>6} {'net$':>10} {'maxDD$':>9}"
    )
    lines.append(header)
    lines.append("  " + "-" * 60)
    for cls in BLOCKED_CLASSES:
        s = result["by_class"][cls]
        pf = s["profit_factor"]
        pf_str = pf if isinstance(pf, str) else f"{pf:.2f}"
        lines.append(
            f"  {cls:<14} {s['n']:>3} {s['win_rate']*100:>5.1f}% {s['expectancy_usd']:>9.2f} "
            f"{pf_str:>6} {s['net_usd']:>10.2f} {s['max_drawdown_usd']:>9.2f}"
        )
    c = result["combined"]
    cpf = c["profit_factor"]
    cpf_str = cpf if isinstance(cpf, str) else f"{cpf:.2f}"
    lines.append("  " + "-" * 60)
    lines.append(
        f"  {'COMBINED':<14} {c['n']:>3} {c['win_rate']*100:>5.1f}% {c['expectancy_usd']:>9.2f} "
        f"{cpf_str:>6} {c['net_usd']:>10.2f} {c['max_drawdown_usd']:>9.2f}"
    )
    lines.append("")
    if result["_trades"]:
        lines.append("Paired blocked trades (realized):")
        for t in sorted(result["_trades"], key=lambda x: x.entry_ts):
            lines.append(
                f"    {t.entry_ts.isoformat()}  {t.classification:<14} "
                f"{t.pnl_points:>+7.2f}pt  net=${t.net_usd:>+9.2f} (msg {t.message_id})"
            )
        lines.append("")
    if result["_unpaired_rows"]:
        lines.append(f"Unpaired blocked entries (no captured exit yet): {result['unpaired']}")
        for rec in result["_unpaired_rows"]:
            lines.append(
                f"    {rec.get('signal_ts')}  {rec.get('classification')}  "
                f"price={rec.get('price')} (msg {rec.get('message_id')})"
            )
        lines.append("")
    n = c["n"]
    lines.append("VERDICT: " + _verdict_from_dict(c, n))
    lines.append("")
    lines.append("Caveats: forward capture (n grows weekly); SB ran 2 contracts (WR/PF are")
    lines.append("sizing-invariant, $ shown at qty above); friction = §0.5.97 model; SB long-only.")
    lines.append("=" * 78)
    return "\n".join(lines)


def _verdict_from_dict(combined_dict: dict, n: int) -> str:
    """Verdict from the as_dict() form (PF may be the string 'inf')."""
    pf_raw = combined_dict["profit_factor"]
    pf = float("inf") if pf_raw == "inf" else float(pf_raw)
    stub = Stats(
        n=n,
        wins=0,
        losses=0,
        scratches=0,
        win_rate=combined_dict["win_rate"],
        expectancy_usd=combined_dict["expectancy_usd"],
        profit_factor=pf,
        avg_win_usd=0.0,
        avg_loss_usd=0.0,
        gross_win_usd=0.0,
        gross_loss_usd=0.0,
        net_usd=combined_dict["net_usd"],
        max_drawdown_usd=combined_dict["max_drawdown_usd"],
    )
    return _verdict(stub, n)


async def _load_live() -> tuple[list[dict], list[dict]]:
    from dotenv import load_dotenv

    from src.clients.supabase_client import SupabaseClient

    load_dotenv("/home/tradeflow/.tradeflow-secrets/.env")
    db = SupabaseClient(url=os.environ["SUPABASE_URL"], key=os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    try:
        reconciliations = await db.select(
            "signal_reconciliations",
            filters={"order": "signal_ts.asc"},
            columns="signal_ts,message_id,seanbot_type,direction,price,classification",
        )
        signals = await db.select(
            "seanbot_signals",
            filters={"order": "ts.asc"},
            columns="ts,message_id,type,direction,symbol,price,pnl_points,contracts",
        )
        return reconciliations, signals
    finally:
        await db.close()


def _load(from_json: bool) -> tuple[list[dict], list[dict]]:
    if from_json and os.path.exists(RECONCILIATIONS_JSON) and os.path.exists(SB_SIGNALS_JSON):
        with open(RECONCILIATIONS_JSON) as fh:
            reconciliations = json.load(fh)
        with open(SB_SIGNALS_JSON) as fh:
            signals = json.load(fh)
        return reconciliations, signals
    reconciliations, signals = asyncio.run(_load_live())
    with open(RECONCILIATIONS_JSON, "w") as fh:
        json.dump(reconciliations, fh)
    with open(SB_SIGNALS_JSON, "w") as fh:
        json.dump(signals, fh)
    return reconciliations, signals


def main() -> None:
    ap = argparse.ArgumentParser(description="SeanBot shadow ledger (Phase 10, research)")
    ap.add_argument("--qty", type=int, default=1, help="contracts for $ figures (default 1 = TF)")
    ap.add_argument("--from-json", action="store_true", help="reuse cached pulls, no DB call")
    ap.add_argument("--out", default=None, help="report path (default /tmp/shadow_<date>.txt)")
    args = ap.parse_args()

    generated_at = datetime.now(UTC)
    reconciliations, signals = _load(args.from_json)
    result = scorecard(reconciliations, signals, qty=args.qty)
    report = format_report(result, generated_at=generated_at)

    out = args.out or f"/tmp/shadow_{generated_at.strftime('%Y-%m-%d')}.txt"
    with open(out, "w") as fh:
        fh.write(report + "\n")
    print(report)
    print(f"\n[written] {out}")


if __name__ == "__main__":
    main()
