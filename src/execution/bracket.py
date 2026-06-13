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

    - ``exit_mode='fixed'`` → returns ``(parent, stop_child, tp_child)``: a fixed STP
      @ ``stop_price`` + a fixed LMT @ ``target_price``, both carrying ``oca_group``
      with ``ocaType=1`` (one fill cancels the sibling broker-side). The fixed STP is
      never ratcheted, so its OCA membership is safe.
    - ``exit_mode='trailing'`` → returns ``(parent, None, None)``: the entry is the
      parent ALONE (transmit=True) and the protective STP is placed STANDALONE
      (parentId=0, ungrouped) post-fill by ``OrderRouter`` — NOT a bracket child.
      STABILIZE-5: a parentId-linked bracket child is auto-OCA'd by the gateway
      (ocaType=3), and an OCA-grouped order cannot be modified (Error 10326 → cancel),
      which would silently break the bar-close ratchet (``ratchet_stop_on_bar`` →
      ``trail_manager``: +50 lock, +200 trail, +1000 hard cap). A standalone STP is
      freely modifiable. No native ``TRAIL`` is used (Error 328 designed out,
      §0.5.190); the STP is stop-MARKET, modified in place.

    Transmit chaining: ``parent.transmit=False`` and the LAST placed leg has
    ``transmit=True`` — fixed mode chains parent → stop → tp (tp transmits). In
    trailing mode the parent is the sole leg and transmits itself. Either way a
    failure before the final leg leaves nothing live (never a naked parent). For
    fixed mode the caller MUST set each child's ``parentId`` to the parent's
    broker-assigned orderId between the parent placement and each child placement;
    in trailing mode there is no child to stitch.
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

    if exit_mode == "trailing":
        # STABILIZE-5 — in trailing mode the entry is the parent ALONE; the
        # protective STP is NOT a native bracket child. Returning ``stop_child=None``
        # tells :meth:`OrderRouter.place_entry` to place a STANDALONE STP (parentId=0,
        # ungrouped) inside the parent fillEvent handler (§0.5.T2 option-γ), via
        # :func:`build_protective_stop` / :func:`ensure_protective_stop`.
        #
        # Why not a bracket child: a parentId-linked STP is AUTO-OCA'd by the IB
        # gateway (ocaType=3, ocaGroup = parent permId) even when the code sets no
        # ocaGroup — and an OCA-grouped order cannot be modified (IBKR Error 10326
        # "OCA group revision is not allowed" → the gateway CANCELS it). That silently
        # destroys the bar-close ratchet's modify-in-place and leaves the position on
        # the stale base stop. STABILIZE-3 tried to fix this by not setting an ocaGroup
        # in code, but the gateway re-introduces it for ANY bracket child. The only
        # robust cure is to never make the protective stop a bracket child. A
        # standalone parentId=0 STP is never auto-OCA'd, so the ratchet walks it freely.
        #
        # Never-naked holds via the synchronous post-fill placement + the reconciler
        # leg-heal backstop; never-orphan (cancel the lone STP when the position closes
        # by any other path) is OrderRouter._cancel_sibling_legs + the reconciler's
        # _cancel_open_legs. There is no native TRAIL (Error 328, §0.5.190); the STP is
        # stop-MARKET so a fast move always exits (Harris Ch.4 — guaranteed exit beats
        # guaranteed price for the protective leg). ``trail_offset`` / ``entry_ref_price``
        # are validated/retained above for API stability.
        parent.transmit = True
        return parent, None, None

    # Fixed protective stop @ stop_price — it never moves in fixed mode. The STP shares
    # an OCA group with the LMT take-profit so a fill on one cancels the other
    # broker-side. (Fixed-mode STPs are never ratcheted, so the OCA grouping is safe —
    # no modify → no Error 10326. Only the trailing STP must stay ungrouped.)
    stop_child = Order()
    stop_child.action = exit_action
    stop_child.totalQuantity = qty
    stop_child.orderType = "STP"
    stop_child.auxPrice = float(stop_price)
    stop_child.tif = "GTC"
    stop_child.outsideRth = True
    stop_child.ocaGroup = oca_group
    stop_child.ocaType = 1

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
