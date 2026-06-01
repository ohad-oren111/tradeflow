"""Dashboard FastAPI server.

Launched as an asyncio task by Orchestrator. Binds 127.0.0.1:8080 by default
(configurable via DASHBOARD_BIND); docker-compose maps host loopback. HTTP
Basic auth via DASHBOARD_USERNAME + DASHBOARD_PASSWORD. Fail-fast on missing
credentials — operator sees the error in logs and adds the env vars.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard.scoreboard import ScoreboardAggregator
from dashboard.state import DashboardAggregator
from dashboard.trades import TradesAggregator

if TYPE_CHECKING:
    from src.orchestrator import Orchestrator

LOGGER = logging.getLogger(__name__)

_DASHBOARD_DIR = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_DASHBOARD_DIR / "templates"))
_security = HTTPBasic()


def load_credentials() -> tuple[str, str]:
    user = os.environ.get("DASHBOARD_USERNAME", "").strip()
    pw = os.environ.get("DASHBOARD_PASSWORD", "").strip()
    if not user or not pw:
        raise RuntimeError(
            "Dashboard requires DASHBOARD_USERNAME and DASHBOARD_PASSWORD env vars. "
            "Add them to ~/.tradeflow-secrets/.env (use `openssl rand -base64 32` "
            "for the password) and rebuild the container."
        )
    return user, pw


def create_app(orchestrator: Orchestrator) -> FastAPI:
    username, password = load_credentials()
    user_b = username.encode("utf-8")
    pass_b = password.encode("utf-8")

    def verify(credentials: Annotated[HTTPBasicCredentials, Depends(_security)]) -> str:
        u_ok = secrets.compare_digest(credentials.username.encode("utf-8"), user_b)
        p_ok = secrets.compare_digest(credentials.password.encode("utf-8"), pass_b)
        if not (u_ok and p_ok):
            LOGGER.warning("[DASH] auth: failed — username=%s", credentials.username)
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials.username

    app = FastAPI(
        title="TradeFlow Dashboard",
        docs_url=None,
        redoc_url=None,
        dependencies=[Depends(verify)],
    )
    aggregator = DashboardAggregator(orchestrator)
    trades_aggregator = TradesAggregator(orchestrator)
    scoreboard_aggregator = ScoreboardAggregator(orchestrator)

    app.mount(
        "/static",
        StaticFiles(directory=str(_DASHBOARD_DIR / "static")),
        name="static",
    )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        state = await aggregator.collect()
        return _TEMPLATES.TemplateResponse(request, "index.html", {"state": state})

    @app.get("/panel/status", response_class=HTMLResponse)
    async def panel_status(request: Request) -> HTMLResponse:
        state = await aggregator.collect()
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/status.html",
            {"panel": state.status, "errors": state.errors},
        )

    @app.get("/panel/account", response_class=HTMLResponse)
    async def panel_account(request: Request) -> HTMLResponse:
        state = await aggregator.collect()
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/account.html",
            {"panel": state.account, "errors": state.errors},
        )

    @app.get("/panel/positions", response_class=HTMLResponse)
    async def panel_positions(request: Request) -> HTMLResponse:
        state = await aggregator.collect()
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/positions.html",
            {"panel": state.positions, "errors": state.errors},
        )

    @app.get("/panel/working_orders", response_class=HTMLResponse)
    async def panel_working_orders(request: Request) -> HTMLResponse:
        state = await aggregator.collect()
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/working_orders.html",
            {"panel": state.working_orders, "errors": state.errors},
        )

    @app.get("/trades", response_class=HTMLResponse)
    async def trades(request: Request) -> HTMLResponse:
        log = await trades_aggregator.trade_log()
        return _TEMPLATES.TemplateResponse(request, "trades.html", {"log": log})

    @app.get("/pnl", response_class=HTMLResponse)
    async def pnl(request: Request) -> HTMLResponse:
        daily = await trades_aggregator.daily_pnl()
        return _TEMPLATES.TemplateResponse(request, "pnl.html", {"daily": daily})

    @app.get("/scoreboard", response_class=HTMLResponse)
    async def scoreboard(request: Request) -> HTMLResponse:
        board = await scoreboard_aggregator.scoreboard()
        return _TEMPLATES.TemplateResponse(request, "scoreboard.html", {"board": board})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


async def run_uvicorn(orchestrator: Orchestrator) -> None:
    """Launch uvicorn in-process. Awaited as a task by Orchestrator."""
    host = os.environ.get("DASHBOARD_BIND", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    app = create_app(orchestrator)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    LOGGER.info("[DASH] server: starting — host=%s port=%s", host, port)
    try:
        await server.serve()
    except Exception:
        LOGGER.exception("[DASH] server: crashed")
        raise
    finally:
        LOGGER.info("[DASH] server: stopped")
