"""Dashboard TF-vs-SeanBot daily P&L scoreboard (PR #71, read-only).

Compares each bot's OWN realized P&L on its OWN book — TF from ``lifecycles``
(``pnl_net``), SeanBot from his operator-trusted daily figures where available,
falling back to a deduped per-exit *estimate* from ``seanbot_signals``. This is
"who made more money running their own strategy", NOT a same-trade comparison:
TF and SeanBot trade different entries (TF is more selective and only went live
2026-06-01). Reads via the orchestrator's service-role ``SupabaseClient``;
SELECT only, no secret rendered.

SeanBot P&L source (DASH-FIX): SeanBot posts no capturable daily-summary line,
and the listener only began mid-2026-05-28, so the per-exit reconstruction is
lossy and cannot reproduce the operator's trusted anchors. The authoritative
figures therefore live in :mod:`dashboard.seanbot_authoritative`; this module
prefers them per day and flags any estimate-fallback day explicitly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from config.instruments import MNQ
from dashboard.seanbot_authoritative import (
    AUTHORITATIVE_SEANBOT_DAILY_PNL,
    authoritative_pnl,
)
from dashboard.trades import DB_VIEW_CAVEAT, TradesAggregator

if TYPE_CHECKING:
    from src.orchestrator import Orchestrator

LOGGER = logging.getLogger(__name__)

SCOREBOARD_CAVEAT = (
    "Different books — each bot's own realized P&L, not same-trade. "
    "Small sample; TF newly live (2026-06-01). "
    "SeanBot P&L is the operator's trusted daily figure where available; days "
    "marked “est.” fall back to a lossy per-exit reconstruction (SeanBot posts "
    "no daily-summary alert, and capture began mid-2026-05-28). " + DB_VIEW_CAVEAT
)


@dataclass
class ScoreRow:
    day: str
    tf_pnl: float
    sb_pnl: float
    delta: float  # tf - sb (positive → TF made more / lost less that day)
    winner: str  # "TF" | "SeanBot" | "tie"
    tf_cum: float
    sb_cum: float
    delta_cum: float
    sb_is_estimate: bool  # True → SeanBot value is the lossy per-exit estimate


@dataclass
class ChartData:
    """Pre-computed inline-SVG geometry for the TF-vs-SeanBot cumulative chart.

    All coordinates are in the SVG user space (viewBox ``0 0 width height``).
    Built in Python so the template stays declarative — see ``_build_chart``.
    """

    width: int
    height: int
    tf_polyline: str  # "x,y x,y ..." for the TF cumulative curve
    sb_polyline: str  # "x,y x,y ..." for the SeanBot cumulative curve
    tf_points: list[tuple[float, float, float]]  # (x, y, cum$) markers
    sb_points: list[tuple[float, float, float, bool]]  # (x, y, cum$, is_estimate)
    x_labels: list[tuple[float, str]]  # (x, "MM-DD")
    y_labels: list[tuple[float, str]]  # (y, "$N")
    zero_y: float


@dataclass
class Scoreboard:
    rows: list[ScoreRow]  # newest day first
    tf_total: float
    sb_total: float
    delta_total: float
    leader: str  # "TF" | "SeanBot" | "tie"
    headline: str
    chart: ChartData | None = None
    has_estimate: bool = False  # any day used the estimate fallback
    caveat: str = SCOREBOARD_CAVEAT
    error: str | None = None


class ScoreboardAggregator:
    """TF-vs-SeanBot daily realized-P&L comparison, read-only."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orch = orchestrator
        self._trades = TradesAggregator(orchestrator)

    async def scoreboard(self) -> Scoreboard:
        tf = await self._trades.daily_pnl()
        if tf.error:
            return _error_board(f"TF read failed: {tf.error}")
        try:
            sb_rows = await self._orch._db.select(
                "seanbot_signals",
                filters={"type": "eq.exit", "order": "ts.desc"},
                columns="pnl_points,contracts,ts,type,price,message_id",
            )
        except Exception as exc:  # noqa: BLE001 — surface as a view error, never 500
            LOGGER.warning("[DASH] scoreboard: seanbot read_failed — %r", exc)
            return _error_board(f"SeanBot read failed: {type(exc).__name__}: {exc}")

        tf_by_day = {d.day: d.pnl_net for d in tf.days}
        sb_estimate_by_day = _aggregate_seanbot_daily(sb_rows)
        return _build_scoreboard(tf_by_day, sb_estimate_by_day)


# SeanBot's book is a fixed 2-contract position: it can close at most ONCE at a
# given price. Multiple ``exit`` rows at the SAME price within this window are the
# same close re-announced by SeanBot's reconciler (each post recomputes P&L, so
# the points differ — e.g. the 2026-06-01 13:22:18/13:22:53/13:22:53 @30365.25
# trio, or the 06-02 19:57:45/19:58:05 @30694.25 exact +49/+49 pair). Summing all
# of them over-captured realized P&L ~3.3× and produced the bogus "TF leads"
# headline (§0.5.197 / §7.3). We collapse each same-price burst to one event.
_DEDUP_WINDOW_SECONDS = 120.0


def _parse_ts(value: Any) -> datetime | None:
    """Parse a ``seanbot_signals.ts`` ISO string (``...+00:00``) → aware datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _dedup_seanbot_exits(
    rows: list[dict[str, Any]],
    window_seconds: float = _DEDUP_WINDOW_SECONDS,
) -> list[dict[str, Any]]:
    """Collapse same-close re-announcements: ``exit`` rows sharing a price whose
    timestamps fall within ``window_seconds`` of the prior kept row at that price
    are the SAME close. Keep the LAST (latest-ts) row — SeanBot's settled recompute.

    Rows with no parseable price/ts are passed through unchanged (they can't be
    proven duplicates and the caller's value-guard drops malformed ones anyway).
    Pure + deterministic; logs which row won each collapse (handoff §11).
    """
    exits = [r for r in rows if str(r.get("type") or "") == "exit"]
    exits.sort(key=lambda r: str(r.get("ts") or ""))  # oldest first

    kept: list[dict[str, Any]] = []
    last_idx_by_price: dict[float, int] = {}
    for row in exits:
        price = _f(row.get("price"))
        ts = _parse_ts(row.get("ts"))
        if price is None or ts is None:
            kept.append(row)
            continue
        prev_idx = last_idx_by_price.get(price)
        if prev_idx is not None:
            prev_ts = _parse_ts(kept[prev_idx].get("ts"))
            if prev_ts is not None and (ts - prev_ts).total_seconds() <= window_seconds:
                LOGGER.debug(
                    "[DASH] scoreboard dedup: collapsed re-announced exit — "
                    "price=%s dropped_msg=%s dropped_pnl_pts=%s kept_msg=%s kept_pnl_pts=%s",
                    price,
                    kept[prev_idx].get("message_id"),
                    kept[prev_idx].get("pnl_points"),
                    row.get("message_id"),
                    row.get("pnl_points"),
                )
                kept[prev_idx] = row  # keep the later (settled) recompute
                continue
        last_idx_by_price[price] = len(kept)
        kept.append(row)
    return kept


def _aggregate_seanbot_daily(rows: list[dict[str, Any]]) -> dict[str, float]:
    """SeanBot realized $ per UTC day: sum(pnl_points × multiplier × contracts),
    after collapsing same-close re-announcements so each exit counts ONCE."""
    by_day: dict[str, float] = {}
    for row in _dedup_seanbot_exits(rows):
        ts = row.get("ts")
        pts = _f(row.get("pnl_points"))
        contracts = _i(row.get("contracts"))
        if not ts or pts is None or not contracts:
            continue
        day = str(ts)[:10]  # YYYY-MM-DD; seanbot_signals.ts is UTC (+00:00)
        by_day[day] = by_day.get(day, 0.0) + pts * MNQ.multiplier * contracts
    return by_day


def _build_scoreboard(
    tf_by_day: dict[str, float], sb_estimate_by_day: dict[str, float]
) -> Scoreboard:
    """Build the scoreboard. SeanBot's value per day is the operator-trusted
    authoritative figure when one exists; otherwise the lossy per-exit estimate,
    flagged via ``sb_is_estimate``. A day appears if EITHER bot has data OR an
    authoritative SeanBot anchor exists (the 2026-05-26/27 anchors have no TF
    rows and no captures, so they would otherwise be invisible)."""
    rows: list[ScoreRow] = []
    tf_cum = 0.0
    sb_cum = 0.0
    has_estimate = False
    all_days = set(tf_by_day) | set(sb_estimate_by_day) | set(AUTHORITATIVE_SEANBOT_DAILY_PNL)
    for day in sorted(all_days):  # oldest first for cumulatives
        tf_p = round(tf_by_day.get(day, 0.0), 2)
        sb_auth = authoritative_pnl(day)
        if sb_auth is not None:
            sb_p = round(sb_auth, 2)
            is_estimate = False
        else:
            sb_p = round(sb_estimate_by_day.get(day, 0.0), 2)
            is_estimate = True
            has_estimate = True
        tf_cum = round(tf_cum + tf_p, 2)
        sb_cum = round(sb_cum + sb_p, 2)
        delta = round(tf_p - sb_p, 2)
        rows.append(
            ScoreRow(
                day=day,
                tf_pnl=tf_p,
                sb_pnl=sb_p,
                delta=delta,
                winner=_winner(delta),
                tf_cum=tf_cum,
                sb_cum=sb_cum,
                delta_cum=round(tf_cum - sb_cum, 2),
                sb_is_estimate=is_estimate,
            )
        )
    rows.reverse()  # newest day first for display
    delta_total = round(tf_cum - sb_cum, 2)
    leader = _winner(delta_total)
    return Scoreboard(
        rows=rows,
        tf_total=tf_cum,
        sb_total=sb_cum,
        delta_total=delta_total,
        leader=leader,
        headline=_headline(leader, delta_total),
        chart=_build_chart(rows),
        has_estimate=has_estimate,
    )


# ---- Cumulative-P&L comparison chart (dependency-light inline SVG) ----------
# Hand-rolled SVG so the dashboard pulls no frontend deps. Geometry is computed
# here (unit-testable) and the template only renders the polylines + labels.
_CHART_W = 760
_CHART_H = 340
_PAD_L = 68  # left padding for the $ axis labels
_PAD_R = 16
_PAD_T = 16
_PAD_B = 40  # bottom padding for the date labels


def _build_chart(rows: list[ScoreRow]) -> ChartData | None:
    """Compute inline-SVG geometry for the TF-vs-SeanBot cumulative curves.

    ``rows`` is newest-first (display order); we plot oldest→newest left→right.
    Y is scaled to the combined range of both cumulative series and always
    includes $0 so the zero line is meaningful. Returns ``None`` for no rows.
    """
    if not rows:
        return None
    series = list(reversed(rows))  # oldest → newest for left→right plotting
    days = [r.day for r in series]
    tf_vals = [r.tf_cum for r in series]
    sb_vals = [r.sb_cum for r in series]

    plot_w = _CHART_W - _PAD_L - _PAD_R
    plot_h = _CHART_H - _PAD_T - _PAD_B

    lo = min([0.0, *tf_vals, *sb_vals])
    hi = max([0.0, *tf_vals, *sb_vals])
    if hi == lo:
        hi = lo + 1.0  # avoid div-by-zero on a flat single point

    n = len(series)

    def x_of(i: int) -> float:
        if n == 1:
            return _PAD_L + plot_w / 2
        return _PAD_L + plot_w * i / (n - 1)

    def y_of(v: float) -> float:
        return _PAD_T + plot_h * (hi - v) / (hi - lo)

    tf_points = [(x_of(i), y_of(tf_vals[i]), tf_vals[i]) for i in range(n)]
    sb_points = [
        (x_of(i), y_of(sb_vals[i]), sb_vals[i], series[i].sb_is_estimate) for i in range(n)
    ]
    tf_poly = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in tf_points)
    sb_poly = " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in sb_points)

    # x labels: up to ~8 evenly spaced dates as MM-DD, always including the last.
    step = max(1, n // 8)
    x_labels = [(x_of(i), days[i][5:]) for i in range(0, n, step)]
    if (n - 1) % step != 0:
        x_labels.append((x_of(n - 1), days[n - 1][5:]))

    # y gridlines: 5 evenly spaced dollar levels across [lo, hi].
    y_labels = [(y_of(lo + (hi - lo) * k / 4), f"${lo + (hi - lo) * k / 4:,.0f}") for k in range(5)]

    return ChartData(
        width=_CHART_W,
        height=_CHART_H,
        tf_polyline=tf_poly,
        sb_polyline=sb_poly,
        tf_points=tf_points,
        sb_points=sb_points,
        x_labels=x_labels,
        y_labels=y_labels,
        zero_y=y_of(0.0),
    )


def _winner(delta: float) -> str:
    if delta > 0:
        return "TF"
    if delta < 0:
        return "SeanBot"
    return "tie"


def _headline(leader: str, delta_total: float) -> str:
    if leader == "tie":
        return "TF and SeanBot are even"
    return f"{leader} leads by ${abs(delta_total):,.2f}"


def _error_board(msg: str) -> Scoreboard:
    return Scoreboard(
        rows=[], tf_total=0.0, sb_total=0.0, delta_total=0.0, leader="tie", headline="—", error=msg
    )


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
