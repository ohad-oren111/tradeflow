"""Dashboard state aggregator.

Pulls live broker state via Orchestrator's IBClient reference. Never touches
Supabase in PR #18 (deferred to #19). Each panel is independently try/except'd
— one failing panel doesn't bring down the page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.orchestrator import Orchestrator

LOGGER = logging.getLogger(__name__)


@dataclass
class PanelStatus:
    halted: bool
    halt_raised_at: datetime | None
    paper_mode: bool
    account: str
    server_version: str | None
    last_heartbeat_at: datetime | None
    front_month_symbol: str


@dataclass
class PanelAccount:
    net_liquidation: float | None
    available_funds: float | None
    buying_power: float | None
    fetched_at: datetime


@dataclass
class PanelPosition:
    symbol: str
    side: str
    qty: int
    avg_entry_price: float | None
    mark_price: float | None
    unrealized_pnl: float | None


@dataclass
class PanelPositions:
    positions: list[PanelPosition]
    fetched_at: datetime


@dataclass
class PanelWorkingOrder:
    order_id: int
    symbol: str
    action: str
    qty: int
    order_type: str
    limit_price: float | None
    stop_price: float | None
    status: str


@dataclass
class PanelWorkingOrders:
    orders: list[PanelWorkingOrder]
    fetched_at: datetime


@dataclass
class DashboardState:
    status: PanelStatus | None
    account: PanelAccount | None
    positions: PanelPositions | None
    working_orders: PanelWorkingOrders | None
    errors: dict[str, str] = field(default_factory=dict)


class DashboardAggregator:
    """Per-panel try/except. Errors captured in DashboardState.errors[panel]."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orch = orchestrator

    async def collect(self) -> DashboardState:
        errors: dict[str, str] = {}
        status = await self._safe(self._collect_status, "status", errors)
        account = await self._safe(self._collect_account, "account", errors)
        positions = await self._safe(self._collect_positions, "positions", errors)
        working_orders = await self._safe(self._collect_working_orders, "working_orders", errors)
        return DashboardState(
            status=status,
            account=account,
            positions=positions,
            working_orders=working_orders,
            errors=errors,
        )

    async def _safe(self, fn, name, errors):
        try:
            return await fn()
        except Exception as exc:
            LOGGER.warning("[DASH] panel: %s aggregate_failed — %r", name, exc)
            errors[name] = f"{type(exc).__name__}: {exc}"
            return None

    async def _collect_status(self) -> PanelStatus:
        orch = self._orch
        account_raw = getattr(orch, "_paper_account", "") or ""
        paper_mode = account_raw.startswith("D")
        server_version_str = orch._safe_server_version()
        sv: str | None = None if server_version_str == "unknown" else server_version_str
        instrument = getattr(orch, "_instrument", "?")
        return PanelStatus(
            halted=orch.is_halted(),
            halt_raised_at=orch.halt_raised_at(),
            paper_mode=paper_mode,
            account=account_raw[:3] if account_raw else "?",
            server_version=sv,
            last_heartbeat_at=None,
            front_month_symbol=instrument,
        )

    async def _collect_account(self) -> PanelAccount:
        account = getattr(self._orch, "_paper_account", "") or ""
        summary = await self._orch._ib.get_account_summary(account)
        return PanelAccount(
            net_liquidation=summary.get("NetLiquidation"),
            available_funds=summary.get("AvailableFunds"),
            buying_power=summary.get("BuyingPower"),
            fetched_at=datetime.now(UTC),
        )

    async def _collect_positions(self) -> PanelPositions:
        items = await self._orch._ib.get_portfolio()
        rows: list[PanelPosition] = []
        for item in items:
            qty = int(getattr(item, "position", 0) or 0)
            if qty == 0:
                continue
            contract = getattr(item, "contract", None)
            symbol = getattr(contract, "localSymbol", None) or getattr(contract, "symbol", "?")
            side = "LONG" if qty > 0 else "SHORT"
            rows.append(
                PanelPosition(
                    symbol=str(symbol),
                    side=side,
                    qty=abs(qty),
                    avg_entry_price=_safe_float(getattr(item, "averageCost", None)),
                    mark_price=_safe_float(getattr(item, "marketPrice", None)),
                    unrealized_pnl=_safe_float(getattr(item, "unrealizedPNL", None)),
                )
            )
        return PanelPositions(positions=rows, fetched_at=datetime.now(UTC))

    async def _collect_working_orders(self) -> PanelWorkingOrders:
        trades = await self._orch._ib.get_open_trades()
        rows: list[PanelWorkingOrder] = []
        for trade in trades:
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            order_status = getattr(trade, "orderStatus", None)
            symbol = getattr(contract, "localSymbol", None) or getattr(contract, "symbol", "?")
            rows.append(
                PanelWorkingOrder(
                    order_id=int(getattr(order, "orderId", 0) or 0),
                    symbol=str(symbol),
                    action=str(getattr(order, "action", "?") or "?"),
                    qty=int(getattr(order, "totalQuantity", 0) or 0),
                    order_type=str(getattr(order, "orderType", "?") or "?"),
                    limit_price=_safe_float(getattr(order, "lmtPrice", None)),
                    stop_price=_safe_float(getattr(order, "auxPrice", None)),
                    status=str(getattr(order_status, "status", "?") or "?"),
                )
            )
        return PanelWorkingOrders(orders=rows, fetched_at=datetime.now(UTC))


def _safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
