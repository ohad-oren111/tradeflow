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


def build_entry_oca_bracket(
    *,
    direction: Direction,
    qty: int,
    entry_type: Literal["MKT", "LMT"],
    entry_lmt_price: float | None,
    stop_price: float,
    target_price: float,
    exit_mode: Literal["trailing", "fixed"],
    trail_offset: float,
    entry_ref_price: float,
    oca_group: str,
) -> tuple[Order, Order, Order | None]:
    """Return ``(parent, stop_child, tp_child)`` — a native server-side OCA bracket.

    PR B — supersedes the §0.5.T2 standalone-STP design for the *entry* path. The
    protective STP is now a bracket child sharing ``parentId`` with the entry and
    an OCA group (``ocaType=1``) with the take-profit leg, so IBKR holds the exit
    pair as a native bracket that SURVIVES a client disconnect / container
    redeploy. This is the root-cause fix for the client-simulated-stop-cancelled-
    on-disconnect incident (Task E.1, #80/#81). The #80 reconciler self-heal and
    the post-fill :func:`ensure_protective_stop` stay as backstops for the
    residual window between the parent fill and the OCA group activating.

    - ``stop_child`` is ALWAYS a fixed STP @ ``stop_price`` — it NEVER trails.
    - ``tp_child`` is a fixed LMT @ ``target_price`` when ``exit_mode='fixed'``
      (the legacy take-profit, no regression). When ``exit_mode='trailing'`` there
      is NO tp child at entry (``tp_child is None``): the fixed STP is the sole exit
      leg of the entry bracket AND the protective floor, and the orchestrator's
      bar-close hook (``OrderRouter.ratchet_stop_on_bar`` → ``trail_manager``) walks
      that STP UP on each closed bar — the SeanBot V3/V12 ratchet (+50 lock, +200
      trail, +1000 hard cap). No native ``TRAIL`` order is used (Error 328 designed
      out, §0.5.190); the resting STP is modified in place.
    - Children are GTC, ``outsideRth=True`` (live overnight Globex), and carry the
      ``oca_group`` with ``ocaType=1`` (one fill cancels the sibling broker-side);
      in trailing mode the lone STP keeps the group (harmless single-member OCA)
      OCA-join it during the never-naked handoff.

    Transmit chaining: ``parent.transmit=False`` and the LAST placed leg has
    ``transmit=True`` — fixed mode chains parent → stop → tp (tp transmits);
    trailing mode chains parent → stop (the STP transmits, as it is the last leg).
    Either way a failure before the final leg leaves nothing live (never a naked
    parent). The caller MUST set each child's ``parentId`` to the parent's
    broker-assigned orderId between the parent placement and each child placement.
    """
    if qty <= 0:
        raise ValueError(f"qty must be positive, got {qty}")
    if entry_type == "LMT" and entry_lmt_price is None:
        raise ValueError("entry_type='LMT' requires entry_lmt_price")
    if entry_type not in ("MKT", "LMT"):
        raise ValueError(f"unsupported entry_type={entry_type!r}")
    if exit_mode not in ("trailing", "fixed"):
        raise ValueError(f"unsupported exit_mode={exit_mode!r}")
    if exit_mode == "trailing" and trail_offset <= 0:
        raise ValueError(f"trail_offset must be positive, got {trail_offset}")

    exit_action = _exit_action(direction)

    parent = Order()
    parent.action = _entry_action(direction)
    parent.totalQuantity = qty
    parent.orderType = entry_type
    if entry_type == "LMT":
        parent.lmtPrice = float(entry_lmt_price)  # type: ignore[arg-type]
    parent.transmit = False
    parent.tif = "DAY"

    # Fixed protective stop @ stop_price. In fixed mode it never moves; in trailing
    # mode it is the floor the bar-close ratchet walks UP (router/trail_manager).
    # Carries the OCA group in both modes (harmless single-member group in trailing).
    stop_child = Order()
    stop_child.action = exit_action
    stop_child.totalQuantity = qty
    stop_child.orderType = "STP"
    stop_child.auxPrice = float(stop_price)
    stop_child.tif = "GTC"
    stop_child.outsideRth = True
    stop_child.ocaGroup = oca_group
    stop_child.ocaType = 1

    if exit_mode == "trailing":
        # NO tp child at entry — the STP is the sole exit leg AND the last leg, so it
        # transmits the bracket. No native TRAIL order is used (a TRAIL cannot be a
        # child of a MKT parent: Error 328, §0.5.190); instead the bar-close ratchet
        # (router/trail_manager) walks this STP UP each bar. ``trail_offset`` /
        # ``entry_ref_price`` are validated/retained for API stability.
        stop_child.transmit = True
        return parent, stop_child, None

    # Fixed mode — legacy LMT take-profit child (no regression). The STP is a
    # non-transmitting middle leg; the TP child is the last leg and transmits the
    # whole bracket atomically.
    stop_child.transmit = False
    tp_child = Order()
    tp_child.action = exit_action
    tp_child.totalQuantity = qty
    tp_child.tif = "GTC"
    tp_child.outsideRth = True
    tp_child.ocaGroup = oca_group
    tp_child.ocaType = 1
    tp_child.transmit = True
    tp_child.orderType = "LMT"
    tp_child.lmtPrice = float(target_price)

    return parent, stop_child, tp_child


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
