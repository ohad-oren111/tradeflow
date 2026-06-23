"""Dynamic MNQ front-month resolution + automatic quarterly roll (PR #170).

PR #167 restored trading by statically pinning ``INSTRUMENT=MNQU6``. That is a
time bomb: when MNQU6 (Sep 2026) expires 2026-09-18 the bot would again silently
subscribe to a dead contract. This module resolves the active front month from
the live IB contract chain (never a hardcoded calendar table) and rolls
automatically, FLAT-only.

Design:
  * ``select_front_month`` (PURE) — given the chain's (symbol, expiry) pairs, pick
    the nearest non-expired expiry that is more than ``buffer_days`` away.
  * ``resolve_front_month`` — calls an injected ``details_fetcher`` (the broker
    ``reqContractDetails`` for ``Future('MNQ','CME','USD')``) and applies the pure
    selector. Read-only against the broker; it places NO orders.
  * ``FrontMonthRoller`` — a daily check with injected dependencies (resolver,
    current-instrument getter, positions getter, restart action, optional override).
    On a due roll while FLAT it triggers a graceful self-restart so the orchestrator
    re-resolves AND re-seeds the warmup buffer through the PROVEN boot path — never
    an in-process buffer hot-swap that could leave the strategy blind. While a
    position is open the roll is DEFERRED to the next flat moment.

Everything here is testable with explicit args / injected callables — no live
broker, no orchestrator coupling.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

LOGGER = logging.getLogger(__name__)

# Resolver target — the MNQ quarterly future on CME. The specific contract month
# is NEVER hardcoded; it is read from the live chain.
MNQ_SYMBOL = "MNQ"
MNQ_EXCHANGE = "CME"
MNQ_CURRENCY = "USD"


def _parse_expiry(raw: str) -> datetime | None:
    """Parse an IB ``lastTradeDateOrContractMonth`` (``YYYYMMDD`` or ``YYYYMM``)."""
    raw = (raw or "").strip()
    for fmt, width in (("%Y%m%d", 8), ("%Y%m", 6)):
        if len(raw) >= width:
            try:
                return datetime.strptime(raw[:width], fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def select_front_month(
    contracts: list[tuple[str, str]],
    *,
    buffer_days: int,
    now: datetime,
) -> tuple[str, int] | None:
    """Pick the front-month ``(local_symbol, days_to_expiry)`` from the chain (PURE).

    ``contracts`` is a list of ``(local_symbol, last_trade_date)`` from the broker
    contract chain. Front month = the nearest expiry whose days-to-expiry is
    STRICTLY GREATER than ``buffer_days`` (so a contract inside the roll window is
    skipped for the next one). Returns ``None`` if nothing qualifies.
    """
    candidates: list[tuple[int, str]] = []
    for symbol, expiry_raw in contracts:
        expiry = _parse_expiry(expiry_raw)
        if expiry is None:
            continue
        dte = (expiry.date() - now.date()).days
        if dte > buffer_days:
            candidates.append((dte, symbol))
    if not candidates:
        return None
    candidates.sort()  # nearest qualifying expiry first
    dte, symbol = candidates[0]
    return symbol, dte


async def resolve_front_month(
    details_fetcher: Callable[[], Awaitable[list]],
    *,
    buffer_days: int,
    now: datetime,
) -> tuple[str, int] | None:
    """Resolve the active MNQ front month from the live chain (read-only).

    ``details_fetcher`` returns the broker ``ContractDetails`` list for the generic
    MNQ future; each item's ``.contract`` carries ``localSymbol`` +
    ``lastTradeDateOrContractMonth``. Places NO orders.
    """
    details = await details_fetcher()
    contracts: list[tuple[str, str]] = []
    for d in details:
        contract = getattr(d, "contract", None)
        if contract is None:
            continue
        local = getattr(contract, "localSymbol", "") or ""
        expiry = getattr(contract, "lastTradeDateOrContractMonth", "") or ""
        if local:
            contracts.append((local, expiry))
    return select_front_month(contracts, buffer_days=buffer_days, now=now)


class FrontMonthRoller:
    """Daily front-month check that rolls FLAT-only via a graceful self-restart.

    All dependencies are injected so the decision logic is unit-testable without a
    live broker or the orchestrator:

      * ``resolver()`` → ``(symbol, dte) | None`` — the live front month.
      * ``current_instrument()`` → the symbol the bot is currently trading.
      * ``get_positions()`` → broker positions (empty ⇒ FLAT).
      * ``request_roll_restart()`` — graceful self-restart (e.g. raise SIGTERM); the
        container's ``restart: unless-stopped`` then re-boots and re-resolves, so the
        warmup buffer is re-seeded through the proven boot path.
      * ``override`` — the ``INSTRUMENT`` env escape hatch; when set, auto-roll is
        disabled (manual control).
    """

    def __init__(
        self,
        *,
        resolver: Callable[[], Awaitable[tuple[str, int] | None]],
        current_instrument: Callable[[], str],
        get_positions: Callable[[], Awaitable[list]],
        request_roll_restart: Callable[[], None],
        override: str | None = None,
    ) -> None:
        self._resolver = resolver
        self._current_instrument = current_instrument
        self._get_positions = get_positions
        self._request_roll_restart = request_roll_restart
        self._override = override

    async def check_once(self) -> str:
        """One roll evaluation. Returns a short status string (also used by tests).

        Status: ``override`` | ``unresolved`` | ``current`` | ``deferred`` | ``rolling``.
        Never raises — a resolution/broker hiccup must not crash the loop.
        """
        if self._override:
            LOGGER.info(
                "[ROLL] override active — INSTRUMENT=%s pinned; auto-roll disabled",
                self._override,
            )
            return "override"
        try:
            resolved = await self._resolver()
        except Exception as exc:  # noqa: BLE001 — a resolve hiccup must not crash the loop
            LOGGER.warning(
                "[ROLL] resolve failed — %s: %s (keeping current contract)",
                type(exc).__name__,
                exc,
            )
            return "unresolved"
        if resolved is None:
            LOGGER.warning("[ROLL] resolve returned no qualifying front month (keeping current)")
            return "unresolved"
        symbol, dte = resolved
        current = self._current_instrument()
        if symbol == current:
            LOGGER.info(
                "[ROLL] resolved front-month=%s dte=%d — no roll (already current)", symbol, dte
            )
            return "current"
        try:
            positions = await self._get_positions()
        except Exception as exc:  # noqa: BLE001 — unknown flatness ⇒ do NOT roll (safe default)
            LOGGER.warning(
                "[ROLL] deferred %s→%s — position check failed (%s: %s); will retry",
                current,
                symbol,
                type(exc).__name__,
                exc,
            )
            return "deferred"
        if positions:
            LOGGER.info(
                "[ROLL] deferred %s→%s — position open; rolling at the next flat moment",
                current,
                symbol,
            )
            return "deferred"
        LOGGER.warning(
            "[ROLL] rolling %s→%s dte=%d — flat book; graceful restart to re-resolve + re-seed",
            current,
            symbol,
            dte,
        )
        self._request_roll_restart()
        return "rolling"
