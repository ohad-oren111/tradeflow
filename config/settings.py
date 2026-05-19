"""Environment-driven settings loader.

Loads from `.env` via python-dotenv and emits a frozen, validated `Settings`
object. The module-level `settings` singleton is constructed at import time.

Tests that need to mutate the environment should `monkeypatch.setenv(...)`
and reload this module via `importlib.reload(config.settings)`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

TradingMode = Literal["paper", "live"]

_LIVE_REQUIRED_KEYS: tuple[str, ...] = (
    "IBKR_USERNAME",
    "IBKR_PASSWORD",
    "IBKR_LIVE_ACCOUNT",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
)


def _get_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return int(raw)


def _get_str(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass(frozen=True)
class Settings:
    # IBKR
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4002
    ibkr_client_id: int = 10
    ibkr_username: str = ""
    ibkr_password: str = ""
    ibkr_totp_secret: str = ""
    ibkr_paper_account: str = ""
    ibkr_live_account: str = ""

    # Mode
    trading_mode: TradingMode = "paper"

    # Supabase
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_anon_key: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Logging
    log_level: str = "INFO"

    # Derived
    missing_live_keys: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    @property
    def is_paper(self) -> bool:
        return self.trading_mode == "paper"


def _load() -> Settings:
    mode_raw = _get_str("TRADING_MODE", "paper").strip().lower()
    if mode_raw not in ("paper", "live"):
        raise RuntimeError(f"Invalid TRADING_MODE={mode_raw!r}; must be 'paper' or 'live'.")
    mode: TradingMode = "live" if mode_raw == "live" else "paper"

    port = _get_int("IBKR_PORT", 4002)
    expected_port = 4001 if mode == "live" else 4002
    if port != expected_port:
        raise RuntimeError(
            f"IBKR_PORT={port} does not match TRADING_MODE={mode!r} "
            f"(expected {expected_port}: paper=4002, live=4001)."
        )

    missing: list[str] = []
    if mode == "live":
        for key in _LIVE_REQUIRED_KEYS:
            if not os.getenv(key):
                missing.append(key)
        if missing:
            raise RuntimeError(f"Missing required env vars for live mode: {', '.join(missing)}")

    return Settings(
        ibkr_host=_get_str("IBKR_HOST", "127.0.0.1"),
        ibkr_port=port,
        ibkr_client_id=_get_int("IBKR_CLIENT_ID", 10),
        ibkr_username=_get_str("IBKR_USERNAME"),
        ibkr_password=_get_str("IBKR_PASSWORD"),
        ibkr_totp_secret=_get_str("IBKR_TOTP_SECRET"),
        ibkr_paper_account=_get_str("IBKR_PAPER_ACCOUNT"),
        ibkr_live_account=_get_str("IBKR_LIVE_ACCOUNT"),
        trading_mode=mode,
        supabase_url=_get_str("SUPABASE_URL"),
        supabase_service_role_key=_get_str("SUPABASE_SERVICE_ROLE_KEY"),
        supabase_anon_key=_get_str("SUPABASE_ANON_KEY"),
        telegram_bot_token=_get_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_get_str("TELEGRAM_CHAT_ID"),
        log_level=_get_str("LOG_LEVEL", "INFO"),
        missing_live_keys=tuple(missing),
    )


settings: Settings = _load()
