"""Parse SeanBot Telegram messages from the "Trading NQ Triggers" channel.

Three known shapes (verbatim from operator-supplied screenshot, PR-D3b brief):

ENTRY:
    🟢 ENTRY — MNQLong @ 29,977.00
    🛑 Stop: 29,902.00 (−75 pt)
    🎯 Target: 30,127.00 (+150 pt)
    Bot size: 2 contracts

EXIT:
    💰 EXIT (profit) — MNQClosed @ 30,026.75 · +50 pt
    Reason: trail stop
    Bot P&L (2 ct): $+198.38

STOP_MOVED:
    🔒 STOP MOVED — MNQ (long @ 29,973.50)
    Stop raised: 29,898.50 → 30,023.50
    Now protecting +50 pt (~$200 on 2 ct)

Anything else → ``type="unknown"`` with ``parsed_ok=False``; raw_text is
preserved upstream so we never drop a message — debugging benefits from a
full audit trail.
"""

from __future__ import annotations

import re
from typing import TypedDict


class ParsedSignal(TypedDict, total=False):
    type: str  # "entry" | "exit" | "stop_moved" | "unknown"
    direction: str | None  # "long" | "short" | None
    symbol: str | None
    price: float | None
    stop_price: float | None
    target_price: float | None
    pnl_points: float | None
    contracts: int | None
    parsed_ok: bool


_NUM = r"[\d,]+(?:\.\d+)?"


def _to_float(s: str | None) -> float | None:
    if s is None:
        return None
    return float(s.replace(",", ""))


def _empty() -> ParsedSignal:
    return ParsedSignal(
        type="unknown",
        direction=None,
        symbol=None,
        price=None,
        stop_price=None,
        target_price=None,
        pnl_points=None,
        contracts=None,
        parsed_ok=False,
    )


# The symbol and the "Long @ <price>" clause may be on the same line
# (old concatenated form "MNQLong @ …") or split across a newline
# (current form "MNQ\nLong @ …"). ``\s*`` between them tolerates both —
# ``\s`` matches the newline, zero-width matches the concatenated form.
_ENTRY_HEAD_RE = re.compile(
    r"ENTRY\s*[—\-]\s*(?P<symbol>[A-Z]{2,5})\s*(?P<direction>Long|Short)\s*@\s*(?P<price>"
    + _NUM
    + r")",
    re.IGNORECASE,
)
_STOP_LINE_RE = re.compile(r"Stop[:\s]+(?P<stop>" + _NUM + r")", re.IGNORECASE)
_TARGET_LINE_RE = re.compile(r"Target[:\s]+(?P<target>" + _NUM + r")", re.IGNORECASE)
_BOT_SIZE_RE = re.compile(r"Bot\s+size[:\s]+(?P<contracts>\d+)\s*contract", re.IGNORECASE)

# ``\s*`` before "Closed" tolerates both "MNQClosed @ …" (old) and
# "MNQ\nClosed @ …" (current newline-split) forms, same as the entry head.
_EXIT_HEAD_RE = re.compile(
    r"EXIT.*?(?P<symbol>[A-Z]{2,5})\s*Closed\s*@\s*(?P<price>" + _NUM + r")"
    r"(?:[^+\-−]*(?P<pnl_sign>[+\-−])\s*(?P<pnl>\d+(?:\.\d+)?)\s*pt)?",
    re.IGNORECASE | re.DOTALL,
)
_EXIT_CONTRACTS_RE = re.compile(r"Bot\s+P&L\s*\((?P<contracts>\d+)\s*ct\)", re.IGNORECASE)

_STOPMOVED_HEAD_RE = re.compile(
    r"STOP\s+MOVED\s*[—\-]\s*(?P<symbol>[A-Z]{2,5})\s*\(\s*(?P<direction>long|short)\s*@\s*(?P<price>"
    + _NUM
    + r")\s*\)",
    re.IGNORECASE,
)
_STOPMOVED_NEW_STOP_RE = re.compile(
    r"Stop\s+(?:raised|lowered)[:\s]+" + _NUM + r"\s*(?:→|->)\s*(?P<new_stop>" + _NUM + r")",
    re.IGNORECASE,
)
_STOPMOVED_PROTECT_RE = re.compile(
    r"protecting\s*(?P<sign>[+\-−])\s*(?P<pts>\d+(?:\.\d+)?)\s*pt",
    re.IGNORECASE,
)
_STOPMOVED_CONTRACTS_RE = re.compile(r"on\s+(?P<contracts>\d+)\s*ct", re.IGNORECASE)


def parse_seanbot_message(text: str) -> ParsedSignal:
    """Classify and extract fields from a SeanBot message.

    Always returns a ParsedSignal — never raises. ``parsed_ok=False`` for
    unknown / malformed shapes; the caller still writes the row with
    raw_text so debugging has the full corpus.
    """
    if not text:
        return _empty()

    if "ENTRY" in text.upper() and "STOP MOVED" not in text.upper():
        return _parse_entry(text)
    if "EXIT" in text.upper():
        return _parse_exit(text)
    if "STOP MOVED" in text.upper():
        return _parse_stop_moved(text)
    return _empty()


def _parse_entry(text: str) -> ParsedSignal:
    out = _empty()
    out["type"] = "entry"

    head = _ENTRY_HEAD_RE.search(text)
    if not head:
        return out

    out["symbol"] = head.group("symbol").upper()
    out["direction"] = head.group("direction").lower()
    out["price"] = _to_float(head.group("price"))

    if (m := _STOP_LINE_RE.search(text)) is not None:
        out["stop_price"] = _to_float(m.group("stop"))
    if (m := _TARGET_LINE_RE.search(text)) is not None:
        out["target_price"] = _to_float(m.group("target"))
    if (m := _BOT_SIZE_RE.search(text)) is not None:
        out["contracts"] = int(m.group("contracts"))

    out["parsed_ok"] = (
        out["price"] is not None
        and out["stop_price"] is not None
        and out["target_price"] is not None
    )
    return out


def _parse_exit(text: str) -> ParsedSignal:
    out = _empty()
    out["type"] = "exit"

    head = _EXIT_HEAD_RE.search(text)
    if not head:
        return out

    out["symbol"] = head.group("symbol").upper()
    out["price"] = _to_float(head.group("price"))

    pnl_sign = head.group("pnl_sign")
    pnl_raw = head.group("pnl")
    if pnl_raw is not None:
        val = float(pnl_raw)
        if pnl_sign and pnl_sign in ("-", "−"):
            val = -val
        out["pnl_points"] = val

    if (m := _EXIT_CONTRACTS_RE.search(text)) is not None:
        out["contracts"] = int(m.group("contracts"))

    out["parsed_ok"] = out["price"] is not None
    return out


def _parse_stop_moved(text: str) -> ParsedSignal:
    out = _empty()
    out["type"] = "stop_moved"

    head = _STOPMOVED_HEAD_RE.search(text)
    if not head:
        return out

    out["symbol"] = head.group("symbol").upper()
    out["direction"] = head.group("direction").lower()
    out["price"] = _to_float(head.group("price"))

    if (m := _STOPMOVED_NEW_STOP_RE.search(text)) is not None:
        out["stop_price"] = _to_float(m.group("new_stop"))
    if (m := _STOPMOVED_PROTECT_RE.search(text)) is not None:
        val = float(m.group("pts"))
        if m.group("sign") in ("-", "−"):
            val = -val
        out["pnl_points"] = val
    if (m := _STOPMOVED_CONTRACTS_RE.search(text)) is not None:
        out["contracts"] = int(m.group("contracts"))

    out["parsed_ok"] = out["price"] is not None and out["stop_price"] is not None
    return out
