"""One-time interactive Telethon session bootstrap.

Run this ONCE over SSH on the VPS. It will prompt for the Telegram phone
number, then for the auth code Telegram sends. On success, it prints a
``StringSession`` blob — paste that into ``/home/tradeflow/.tradeflow-secrets/.env``
as ``TELEGRAM_SESSION_STRING=...`` and ``docker compose restart
telegram-listener``.

Why string sessions: no on-disk session file means no docker volume to
manage, and restart semantics are clean — the listener just re-reads the
string from env on boot.
"""

from __future__ import annotations

import os

from telethon.sessions import StringSession
from telethon.sync import TelegramClient


def main() -> None:
    api_id_raw = input("API_ID (enter to read from env TELEGRAM_API_ID): ").strip()
    api_id = int(api_id_raw or os.environ["TELEGRAM_API_ID"])

    api_hash = input("API_HASH (enter to read from env TELEGRAM_API_HASH): ").strip()
    api_hash = api_hash or os.environ["TELEGRAM_API_HASH"]

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("\n=== SESSION STRING (paste into .tradeflow-secrets/.env) ===")
        print(f"TELEGRAM_SESSION_STRING={client.session.save()}")
        print("=== END ===\n")


if __name__ == "__main__":
    main()
