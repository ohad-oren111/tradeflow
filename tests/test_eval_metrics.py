"""Unit tests for tools/eval/metrics.py — expectancy / PF / drawdown / segmentation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from tools.eval.metrics import compute_stats, max_drawdown, segment_by_month


@dataclass
class _T:
    net_usd: float
    entry_ts: datetime

    @property
    def is_win(self) -> bool:
        return self.net_usd > 0


def _t(pnl: float, month: int = 1) -> _T:
    return _T(pnl, datetime(2025, month, 1, tzinfo=UTC))


def test_empty():
    s = compute_stats([])
    assert s.n == 0 and s.profit_factor == float("inf") or s.n == 0


def test_basic_stats():
    trades = [_t(100), _t(-50), _t(200), _t(-150)]
    s = compute_stats(trades)
    assert s.n == 4
    assert s.wins == 2 and s.losses == 2
    assert s.win_rate == 0.5
    assert s.net_usd == 100.0
    assert s.expectancy_usd == 25.0
    assert abs(s.profit_factor - (300 / 200)) < 1e-9
    assert s.avg_win_usd == 150.0
    assert s.avg_loss_usd == -100.0


def test_profit_factor_infinite_when_no_losses():
    s = compute_stats([_t(10), _t(20)])
    assert s.profit_factor == float("inf")


def test_max_drawdown():
    # equity: +100, +50, +250, +50 → peak 250 then 50 = dd 200; also dip to 50 early.
    assert max_drawdown([100, -50, 200, -200]) == 200.0
    assert max_drawdown([-100, -100]) == 200.0
    assert max_drawdown([100, 100]) == 0.0


def test_segment_by_month():
    trades = [_t(100, 1), _t(-50, 1), _t(30, 2)]
    monthly = segment_by_month(trades)
    assert list(monthly.keys()) == ["2025-01", "2025-02"]
    assert monthly["2025-01"].n == 2
    assert monthly["2025-02"].n == 1
    assert monthly["2025-02"].net_usd == 30.0
