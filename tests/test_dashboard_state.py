"""Unit tests for DashboardAggregator (broker-sourced; no Supabase)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from dashboard.state import (
    DashboardAggregator,
    PanelAccount,
    PanelPositions,
    PanelStatus,
    PanelWorkingOrders,
)


def _orch_mock() -> MagicMock:
    orch = MagicMock(name="Orchestrator")
    orch._paper_account = "DUQ331660"
    orch._instrument = "MNQM6"
    orch.is_halted = MagicMock(return_value=False)
    orch.halt_raised_at = MagicMock(return_value=None)
    orch._safe_server_version = MagicMock(return_value="178")
    orch._ib = MagicMock(name="IBClient")
    orch._ib.get_account_summary = AsyncMock(
        return_value={
            "NetLiquidation": 1000085.80,
            "AvailableFunds": 950000.0,
            "BuyingPower": 4750000.0,
        }
    )
    orch._ib.get_portfolio = AsyncMock(return_value=[])
    orch._ib.get_open_trades = AsyncMock(return_value=[])
    return orch


def _portfolio_item(symbol: str, qty: int, avg: float, mark: float, pnl: float) -> MagicMock:
    item = MagicMock(name=f"PortfolioItem<{symbol}>")
    contract = MagicMock()
    contract.localSymbol = symbol
    contract.symbol = symbol[:3]
    item.contract = contract
    item.position = qty
    item.averageCost = avg
    item.marketPrice = mark
    item.unrealizedPNL = pnl
    return item


def _open_trade(
    order_id: int,
    symbol: str,
    action: str,
    qty: int,
    order_type: str,
    lmt: float | None,
    stp: float | None,
    status: str,
) -> MagicMock:
    trade = MagicMock(name=f"OpenTrade<{order_id}>")
    trade.order = MagicMock()
    trade.order.orderId = order_id
    trade.order.action = action
    trade.order.totalQuantity = qty
    trade.order.orderType = order_type
    trade.order.lmtPrice = lmt
    trade.order.auxPrice = stp
    trade.orderStatus = MagicMock()
    trade.orderStatus.status = status
    contract = MagicMock()
    contract.localSymbol = symbol
    contract.symbol = symbol[:3]
    trade.contract = contract
    return trade


async def test_collect_returns_all_four_panels_with_no_positions():
    orch = _orch_mock()
    agg = DashboardAggregator(orch)

    state = await agg.collect()

    assert isinstance(state.status, PanelStatus)
    assert isinstance(state.account, PanelAccount)
    assert isinstance(state.positions, PanelPositions)
    assert isinstance(state.working_orders, PanelWorkingOrders)
    assert state.errors == {}
    assert state.positions.positions == []
    assert state.working_orders.orders == []


async def test_collect_isolates_account_failure_to_one_panel():
    orch = _orch_mock()
    orch._ib.get_account_summary = AsyncMock(side_effect=RuntimeError("boom"))
    agg = DashboardAggregator(orch)

    state = await agg.collect()

    assert state.account is None
    assert "account" in state.errors
    assert state.status is not None
    assert state.positions is not None
    assert state.working_orders is not None


async def test_collect_isolates_positions_failure_to_one_panel():
    orch = _orch_mock()
    orch._ib.get_portfolio = AsyncMock(side_effect=RuntimeError("portfolio_fail"))
    agg = DashboardAggregator(orch)

    state = await agg.collect()

    assert state.positions is None
    assert "positions" in state.errors
    assert state.account is not None
    assert state.working_orders is not None


async def test_collect_isolates_working_orders_failure_to_one_panel():
    orch = _orch_mock()
    orch._ib.get_open_trades = AsyncMock(side_effect=RuntimeError("trades_fail"))
    agg = DashboardAggregator(orch)

    state = await agg.collect()

    assert state.working_orders is None
    assert "working_orders" in state.errors
    assert state.positions is not None


async def test_status_panel_reflects_halted_true():
    orch = _orch_mock()
    orch.is_halted = MagicMock(return_value=True)
    halt_ts = datetime(2026, 5, 22, 18, 30, tzinfo=UTC)
    orch.halt_raised_at = MagicMock(return_value=halt_ts)
    agg = DashboardAggregator(orch)

    state = await agg.collect()

    assert state.status is not None
    assert state.status.halted is True
    assert state.status.halt_raised_at == halt_ts


async def test_status_panel_reflects_halt_raised_at_propagated():
    orch = _orch_mock()
    ts = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    orch.is_halted = MagicMock(return_value=True)
    orch.halt_raised_at = MagicMock(return_value=ts)
    agg = DashboardAggregator(orch)

    state = await agg.collect()

    assert state.status is not None
    assert state.status.halt_raised_at == ts
    assert state.status.front_month_symbol == "MNQM6"
    assert state.status.account == "DUQ"
    assert state.status.paper_mode is True


async def test_account_panel_parses_netliq_correctly():
    orch = _orch_mock()
    agg = DashboardAggregator(orch)

    state = await agg.collect()

    assert state.account is not None
    assert state.account.net_liquidation == pytest.approx(1000085.80)
    assert state.account.available_funds == pytest.approx(950000.0)
    assert state.account.buying_power == pytest.approx(4750000.0)


async def test_positions_panel_converts_portfolioitem_to_panelposition():
    orch = _orch_mock()
    orch._ib.get_portfolio = AsyncMock(
        return_value=[
            _portfolio_item("MNQM6", 2, 21000.0, 21010.0, 200.0),
            _portfolio_item("MNQU6", -1, 21500.0, 21490.0, 20.0),
            _portfolio_item("MNQZ6", 0, 0.0, 0.0, 0.0),
        ]
    )
    agg = DashboardAggregator(orch)

    state = await agg.collect()

    assert state.positions is not None
    rows = state.positions.positions
    assert len(rows) == 2
    long_row = rows[0]
    assert long_row.symbol == "MNQM6"
    assert long_row.side == "LONG"
    assert long_row.qty == 2
    assert long_row.avg_entry_price == pytest.approx(21000.0)
    assert long_row.mark_price == pytest.approx(21010.0)
    assert long_row.unrealized_pnl == pytest.approx(200.0)
    short_row = rows[1]
    assert short_row.symbol == "MNQU6"
    assert short_row.side == "SHORT"
    assert short_row.qty == 1


async def test_working_orders_panel_converts_trade_to_panelworkingorder():
    orch = _orch_mock()
    orch._ib.get_open_trades = AsyncMock(
        return_value=[
            _open_trade(101, "MNQM6", "SELL", 2, "STP", None, 20925.0, "Submitted"),
            _open_trade(102, "MNQM6", "SELL", 2, "LMT", 21150.0, None, "PreSubmitted"),
        ]
    )
    agg = DashboardAggregator(orch)

    state = await agg.collect()

    assert state.working_orders is not None
    rows = state.working_orders.orders
    assert len(rows) == 2
    assert rows[0].order_id == 101
    assert rows[0].order_type == "STP"
    assert rows[0].stop_price == pytest.approx(20925.0)
    assert rows[0].limit_price is None
    assert rows[1].order_type == "LMT"
    assert rows[1].limit_price == pytest.approx(21150.0)
