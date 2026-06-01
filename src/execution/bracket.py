"""Pure bracket / protective-stop order construction per CLAUDE.md §0.5.T2.

§0.5.T2 — "Long-only bracket-child SL is DISABLED by default; use separate GTC
stop." This selects option γ: MKT (or LMT) entry parent + LMT take-profit child
+ a deferred STP-GTC placed after the parent fill is confirmed (parentId=0, so
it stands alone and the EOD/manual cancel paths don't have to know about a
parent linkage).

Trade-off: there is an unprotected latency window between parent fill and STP
arming. The OrderRouter must place the STP synchronously inside the parent
fillEvent callback so the window is bounded by IB round-trip time. §0.5.T5
(never leave a futures position without a GTC stop on IBKR) is satisfied
end-to-end by that protocol, not by the bracket leg itself.

Pure functions: no IB calls, no side effects, no globals. Caller is responsible
for stitching ``tp_child.parentId`` to the parent's broker-assigned orderId.
"""

from __future__ import annotations

from typing import Literal

from ib_async import Order

from src.state_machine import Direction


def _entry_action(direction: Direction) -> str:
    return "BUY" if direction is Direction.LONG else "SELL"


def _exit_action(direction: Direction) -> str:
    return "SELL" if direction is Direction.LONG else "BUY"


def build_bracket(
    *,
    direction: Direction,
    qty: int,
    entry_type: Literal["MKT", "LMT"],
    entry_lmt_price: float | None,
    target_price: float,
) -> tuple[Order, Order]:
    """Return ``(parent, tp_child)`` for the option-γ entry pair.

    ``parent.transmit`` is False — placing the parent alone is a no-op at the
    exchange. ``tp_child.transmit`` is True; placing it submits the pair
    atomically. Caller MUST set ``tp_child.parentId = parent.orderId`` after the
    parent ``Trade`` returns from ``IB.placeOrder``.
    """
    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty}")
    if entry_type == "LMT" and entry_lmt_price is None:
        raise ValueError("entry_type='LMT' requires entry_lmt_price")
    if entry_type not in ("MKT", "LMT"):
        raise ValueError(f"unsupported entry_type={entry_type!r}")

    parent = Order()
    parent.action = _entry_action(direction)
    parent.totalQuantity = qty
    parent.orderType = entry_type
    if entry_type == "LMT":
        parent.lmtPrice = float(entry_lmt_price)  # type: ignore[arg-type]
    parent.transmit = False
    parent.tif = "DAY"

    tp_child = Order()
    tp_child.action = _exit_action(direction)
    tp_child.totalQuantity = qty
    tp_child.orderType = "LMT"
    tp_child.lmtPrice = float(target_price)
    tp_child.transmit = True
    # GTC so the TP leg survives the daily CME maintenance break and overnight
    # gap window under 24/5 trading — paired with the GTC STP placed after
    # parent fill in :func:`build_protective_stop`.
    tp_child.tif = "GTC"
    # outsideRth=True so the leg is eligible during the overnight Globex session,
    # not only the RTH window (09:30-16:00 ET). A 24/5 bot routinely holds
    # positions overnight; an RTH-only protective leg is dormant exactly when an
    # overnight move would hit it (see build_protective_stop for the incident).
    tp_child.outsideRth = True
    # parentId is left as the default 0 — OrderRouter stitches it after
    # IB.placeOrder assigns the parent orderId.

    return parent, tp_child


def build_protective_stop(
    *,
    direction: Direction,
    qty: int,
    stop_price: float,
) -> Order:
    """Standalone STP order. Placed after parent fill confirmation, per §0.5.T2.

    ``parentId=0`` keeps it independent of the bracket pair — cancelling the
    bracket does not cancel the stop, and EOD force-close cancels by lifecycle
    rather than by parent linkage. ``tif='GTC'`` ensures the stop persists
    across the IBC nightly restart window if a trade straddles it.
    """
    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty}")

    stp = Order()
    stp.action = _exit_action(direction)
    stp.totalQuantity = qty
    stp.orderType = "STP"
    stp.auxPrice = float(stop_price)
    stp.tif = "GTC"
    stp.parentId = 0
    stp.transmit = True
    # outsideRth=True is load-bearing for a 24/5 Globex bot. With the ib_async
    # default (False) the STP only triggers during RTH (09:30-16:00 ET). On
    # 2026-06-01 price breached a 30483.50 stop at 08:09 UTC overnight; the
    # RTH-only STP could not fire and only armed at the 13:30 UTC RTH open,
    # filling ~155pt late (-$1.85k vs ~-$600 for a clean stop). §0.5.T5.
    stp.outsideRth = True
    return stp
