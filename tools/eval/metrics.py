"""Pure expectancy / PF / win-rate / drawdown + per-month segmentation.

No I/O, no globals — takes a list of engine ``Trade`` objects (or any objects with
``.net_usd`` / ``.is_win`` / ``.entry_ts``) and returns plain dicts. Kept separate
from the engine so the same math serves the backtest, the synthetic harness, and
the consolidated report.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass
class Stats:
    n: int
    wins: int
    losses: int
    scratches: int
    win_rate: float
    expectancy_usd: float  # mean net P&L per trade
    profit_factor: float  # sum(wins) / abs(sum(losses)); inf if no losses
    avg_win_usd: float
    avg_loss_usd: float
    gross_win_usd: float
    gross_loss_usd: float
    net_usd: float  # sum of net P&L
    max_drawdown_usd: float  # worst peak-to-trough on the cumulative net curve

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "wins": self.wins,
            "losses": self.losses,
            "scratches": self.scratches,
            "win_rate": round(self.win_rate, 4),
            "expectancy_usd": round(self.expectancy_usd, 2),
            "profit_factor": (
                round(self.profit_factor, 3) if self.profit_factor != float("inf") else "inf"
            ),
            "avg_win_usd": round(self.avg_win_usd, 2),
            "avg_loss_usd": round(self.avg_loss_usd, 2),
            "net_usd": round(self.net_usd, 2),
            "max_drawdown_usd": round(self.max_drawdown_usd, 2),
        }


def _pnls(trades: Sequence) -> list[float]:
    return [float(t.net_usd) for t in trades]


def max_drawdown(pnls: Sequence[float]) -> float:
    """Worst peak-to-trough drop on the cumulative equity curve (>= 0)."""
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def compute_stats(trades: Sequence) -> Stats:
    pnls = _pnls(trades)
    n = len(pnls)
    if n == 0:
        return Stats(0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    scratches = [p for p in pnls if p == 0]
    gross_win = sum(wins)
    gross_loss = sum(losses)  # negative
    pf = (gross_win / abs(gross_loss)) if gross_loss != 0 else float("inf")
    return Stats(
        n=n,
        wins=len(wins),
        losses=len(losses),
        scratches=len(scratches),
        win_rate=len(wins) / n,
        expectancy_usd=sum(pnls) / n,
        profit_factor=pf,
        avg_win_usd=(gross_win / len(wins)) if wins else 0.0,
        avg_loss_usd=(gross_loss / len(losses)) if losses else 0.0,
        gross_win_usd=gross_win,
        gross_loss_usd=gross_loss,
        net_usd=sum(pnls),
        max_drawdown_usd=max_drawdown(pnls),
    )


def segment_by_month(trades: Sequence) -> OrderedDict[str, Stats]:
    """Group trades by entry-month (YYYY-MM) and compute per-month Stats.

    Months with n < 10 trades should be flagged as noise by the caller.
    """
    buckets: OrderedDict[str, list] = OrderedDict()
    for t in sorted(trades, key=lambda x: x.entry_ts):
        key = t.entry_ts.strftime("%Y-%m")
        buckets.setdefault(key, []).append(t)
    return OrderedDict((k, compute_stats(v)) for k, v in buckets.items())


def format_month_table(monthly: OrderedDict[str, Stats], noise_threshold: int = 10) -> str:
    """A fixed-width per-month table; rows with n < threshold are tagged (noise)."""
    header = (
        f"{'month':<8} {'n':>4} {'win%':>6} {'expect$':>9} "
        f"{'PF':>6} {'net$':>10} {'maxDD$':>9}  flag"
    )
    lines = [header, "-" * 70]
    for month, s in monthly.items():
        pf = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "inf"
        flag = "(noise n<10)" if s.n < noise_threshold else ""
        lines.append(
            f"{month:<8} {s.n:>4} {s.win_rate*100:>5.1f}% {s.expectancy_usd:>9.2f} "
            f"{pf:>6} {s.net_usd:>10.2f} {s.max_drawdown_usd:>9.2f}  {flag}"
        )
    return "\n".join(lines)
