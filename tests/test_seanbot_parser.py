"""Parser tests using verbatim fixtures from the operator-supplied screenshot.

These exact strings are what the SeanBot channel emits — keeping them
character-for-character (incl. the U+2212 MINUS SIGN, the em-dash, and the
emoji) is the whole point. Any drift means real messages stop parsing.
"""

from __future__ import annotations

from src.listeners.seanbot_parser import parse_seanbot_message

ENTRY_MSG = (
    "🟢 ENTRY — MNQLong @ 29,977.00\n"
    "🛑 Stop: 29,902.00 (−75 pt)\n"
    "🎯 Target: 30,127.00 (+150 pt)\n"
    "Bot size: 2 contracts"
)

EXIT_MSG = (
    "💰 EXIT (profit) — MNQClosed @ 30,026.75 · +50 pt\n"
    "Reason: trail stop\n"
    "Bot P&L (2 ct): $+198.38"
)

EXIT_LOSS_MSG = (
    "💸 EXIT (loss) — MNQClosed @ 29,902.00 · −75 pt\n"
    "Reason: stop hit\n"
    "Bot P&L (2 ct): $-301.62"
)

STOP_MOVED_MSG = (
    "🔒 STOP MOVED — MNQ (long @ 29,973.50)\n"
    "Stop raised: 29,898.50 → 30,023.50\n"
    "Now protecting +50 pt (~$200 on 2 ct)"
)


def test_entry_parses_full_fields():
    p = parse_seanbot_message(ENTRY_MSG)
    assert p["type"] == "entry"
    assert p["direction"] == "long"
    assert p["symbol"] == "MNQ"
    assert p["price"] == 29977.00
    assert p["stop_price"] == 29902.00
    assert p["target_price"] == 30127.00
    assert p["contracts"] == 2
    assert p["parsed_ok"] is True


def test_exit_profit_parses_pnl_positive():
    p = parse_seanbot_message(EXIT_MSG)
    assert p["type"] == "exit"
    assert p["symbol"] == "MNQ"
    assert p["price"] == 30026.75
    assert p["pnl_points"] == 50.0
    assert p["contracts"] == 2
    assert p["parsed_ok"] is True


def test_exit_loss_uses_unicode_minus():
    p = parse_seanbot_message(EXIT_LOSS_MSG)
    assert p["type"] == "exit"
    assert p["price"] == 29902.00
    assert p["pnl_points"] == -75.0
    assert p["contracts"] == 2
    assert p["parsed_ok"] is True


def test_stop_moved_parses_new_stop_and_protection():
    p = parse_seanbot_message(STOP_MOVED_MSG)
    assert p["type"] == "stop_moved"
    assert p["direction"] == "long"
    assert p["symbol"] == "MNQ"
    assert p["price"] == 29973.50
    assert p["stop_price"] == 30023.50  # the NEW stop, not the previous
    assert p["pnl_points"] == 50.0
    assert p["contracts"] == 2
    assert p["parsed_ok"] is True


def test_empty_string_returns_unknown():
    p = parse_seanbot_message("")
    assert p["type"] == "unknown"
    assert p["parsed_ok"] is False


def test_garbage_text_returns_unknown():
    p = parse_seanbot_message("good morning everyone, market looks choppy today")
    assert p["type"] == "unknown"
    assert p["parsed_ok"] is False


def test_entry_shape_with_missing_fields_marks_parsed_ok_false():
    # Has ENTRY keyword and a head match, but no Stop/Target lines — type
    # classification still works, but parsed_ok must be False so the row is
    # flagged for manual review.
    partial = "🟢 ENTRY — MNQLong @ 29,977.00"
    p = parse_seanbot_message(partial)
    assert p["type"] == "entry"
    assert p["price"] == 29977.00
    assert p["stop_price"] is None
    assert p["target_price"] is None
    assert p["parsed_ok"] is False


def test_stop_moved_with_lowered_direction():
    # Sanity: parser accepts "Stop lowered:" (short side) as well as raised.
    short_msg = (
        "🔒 STOP MOVED — MNQ (short @ 30,100.00)\n"
        "Stop lowered: 30,200.00 → 30,050.00\n"
        "Now protecting +50 pt (~$200 on 2 ct)"
    )
    p = parse_seanbot_message(short_msg)
    assert p["type"] == "stop_moved"
    assert p["direction"] == "short"
    assert p["stop_price"] == 30050.00
    assert p["parsed_ok"] is True


def test_thousands_separator_stripped():
    # Explicit guard against future "29977.00" formatting drift removing commas.
    p = parse_seanbot_message(ENTRY_MSG)
    assert isinstance(p["price"], float)
    assert p["price"] == 29977.0


# --------------------------------------------------------------------------
# Current channel format (W-S13.3): the symbol sits on the header line and the
# "Long @"/"Closed @" price clause is on the NEXT line. The old fixtures above
# concatenated them ("MNQLong @"). Both must parse — the parser tolerates the
# whitespace/newline between symbol and clause. These three strings are
# verbatim from the operator's 2026-05-28 screenshots; live msg_id 89/90/92
# (exits) failed to parse before this fix.

ENTRY_MULTILINE_MSG = (
    "🟢 ENTRY — MNQ\n"
    "Long @ 30,301.75\n"
    "🔴 Stop: 30,226.75 (−75 pt)\n"
    "🎯 Target: 30,451.75 (+150 pt)\n"
    "Bot size: 2 contracts"
)

EXIT_MULTILINE_MSG = (
    "💰 EXIT (profit) — MNQ\n"
    "Closed @ 30,348.00 · +46 pt\n"
    "Reason: trail stop\n"
    "Bot P&L (2 ct): $+184.38"
)

STOP_MOVED_MULTILINE_MSG = (
    "🔒 STOP MOVED — MNQ (long @ 30,306.50)\n"
    "Stop raised: 30,231.50 → 30,356.50\n"
    "Now protecting +50 pt (~$200 on 2 ct)"
)


def test_entry_multiline_header_parses_full_fields():
    p = parse_seanbot_message(ENTRY_MULTILINE_MSG)
    assert p["type"] == "entry"
    assert p["direction"] == "long"
    assert p["symbol"] == "MNQ"
    assert p["price"] == 30301.75
    assert p["stop_price"] == 30226.75
    assert p["target_price"] == 30451.75
    assert p["contracts"] == 2
    assert p["parsed_ok"] is True


def test_exit_multiline_header_parses_price_and_pnl():
    p = parse_seanbot_message(EXIT_MULTILINE_MSG)
    assert p["type"] == "exit"
    assert p["symbol"] == "MNQ"
    assert p["price"] == 30348.00
    assert p["pnl_points"] == 46.0
    assert p["contracts"] == 2
    assert p["parsed_ok"] is True


def test_stop_moved_multiline_parses_new_stop():
    p = parse_seanbot_message(STOP_MOVED_MULTILINE_MSG)
    assert p["type"] == "stop_moved"
    assert p["direction"] == "long"
    assert p["symbol"] == "MNQ"
    assert p["price"] == 30306.50
    assert p["stop_price"] == 30356.50  # the NEW stop
    assert p["parsed_ok"] is True


def test_garbage_multiline_does_not_crash_and_is_unknown():
    # A non-signal multi-line message must classify unknown, never raise.
    junk = "hey team\nanyone watching the open?\nlooks like chop 🤔"
    p = parse_seanbot_message(junk)
    assert p["type"] == "unknown"
    assert p["parsed_ok"] is False
