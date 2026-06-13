"""Telegram listener: capture SeanBot signals into Supabase.

Joins the channel named by ``TELEGRAM_SOURCE_CHANNEL`` using a user-account
session (Telethon ``StringSession``), parses each new message via
:mod:`src.listeners.seanbot_parser`, and writes a row to ``seanbot_signals``.

This runs as its own docker-compose service (``telegram-listener``). It does
NOT share a process with the trading orchestrator — crashes here must not
take down the bot, and vice versa. See PR-D3b brief.

Bootstrap: see ``scripts/bootstrap_telegram_session.py``. The session string
goes into ``.tradeflow-secrets/.env`` as ``TELEGRAM_SESSION_STRING=...`` once,
interactively, over SSH.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from src.clients.supabase_client import SupabaseClient
from src.listeners.seanbot_parser import parse_seanbot_message

LOGGER = logging.getLogger(__name__)

IDLE_SECONDS_WHEN_NO_SESSION = 60


def _env_int(name: str) -> int:
    return int(os.environ[name])


async def _write_signal(
    supabase: SupabaseClient,
    *,
    ts: datetime,
    channel: str,
    message_id: int,
    raw_text: str,
    parsed: dict[str, Any],
) -> None:
    row = {
        "ts": ts.astimezone(UTC).isoformat(),
        "channel": channel,
        "message_id": message_id,
        "type": parsed.get("type", "unknown"),
        "direction": parsed.get("direction"),
        "symbol": parsed.get("symbol"),
        "price": parsed.get("price"),
        "stop_price": parsed.get("stop_price"),
        "target_price": parsed.get("target_price"),
        "pnl_points": parsed.get("pnl_points"),
        "contracts": parsed.get("contracts"),
        "raw_text": raw_text,
        "parsed_ok": parsed.get("parsed_ok", False),
    }
    # Idempotent on the (channel, message_id) unique key — listener restart
    # or Telethon backfill won't duplicate rows.
    await supabase.upsert("seanbot_signals", row, on_conflict="channel,message_id")


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("ORCH_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    session_string = os.environ.get("TELEGRAM_SESSION_STRING", "").strip()
    if not session_string:
        LOGGER.error(
            "[TG_LISTENER] no session — run scripts/bootstrap_telegram_session.py"
            " and paste the result into .tradeflow-secrets/.env as TELEGRAM_SESSION_STRING"
        )
        # Stay alive but idle so the container does not crash-loop; operator
        # adds the session string and runs `docker restart` to pick it up.
        await asyncio.sleep(IDLE_SECONDS_WHEN_NO_SESSION)
        return

    api_id = _env_int("TELEGRAM_API_ID")
    api_hash = os.environ["TELEGRAM_API_HASH"]
    channel = os.environ["TELEGRAM_SOURCE_CHANNEL"]

    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not supabase_key:
        LOGGER.error("[TG_LISTENER] missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        await asyncio.sleep(IDLE_SECONDS_WHEN_NO_SESSION)
        return

    supabase = SupabaseClient(url=supabase_url, key=supabase_key)
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.start()

    # Telethon's get_entity() resolves a display-name string ("Trading NQ
    # Triggers") only against the dialog cache, which is empty on a first-run
    # StringSession. Walk dialogs once so a private-channel title resolves
    # without forcing operators to look up the numeric peer id. Cheap even
    # for accounts with hundreds of chats; only runs at startup.
    n_dialogs = 0
    async for _ in client.iter_dialogs():
        n_dialogs += 1
    LOGGER.info("[TG_LISTENER] dialog cache warmed — count=%d", n_dialogs)

    entity = await client.get_entity(channel)
    LOGGER.info("[TG_LISTENER] connected — channel=%s id=%s", channel, entity.id)

    @client.on(events.NewMessage(chats=entity))
    async def _on_msg(event: Any) -> None:
        raw_text = event.raw_text or ""
        parsed = parse_seanbot_message(raw_text)
        try:
            await _write_signal(
                supabase,
                ts=event.date,
                channel=channel,
                message_id=event.id,
                raw_text=raw_text,
                parsed=parsed,
            )
        except httpx.HTTPError as exc:
            # Log and continue — losing one row is better than the listener
            # crashing on transient Supabase issues. UNIQUE key means a
            # backfill rerun would dedupe anyway.
            LOGGER.exception(
                "[TG_LISTENER] supabase write failed — msg_id=%s err=%s", event.id, exc
            )
            return
        LOGGER.info(
            "[TG_LISTENER] signal — type=%s parsed_ok=%s msg_id=%s",
            parsed.get("type"),
            parsed.get("parsed_ok"),
            event.id,
        )

    try:
        await client.run_until_disconnected()
    finally:
        await supabase.close()


if __name__ == "__main__":
    asyncio.run(main())
