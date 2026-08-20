"""
api/main.py — FastAPI application entry point.

Runs the REST API and WebSocket server. The tick loop runs as a SEPARATE
process (python main.py) and communicates via shared SQLite. This process
only reads data (except for the /api/ingest endpoint which writes to the
knowledge_base/ directory, not the DB).

Startup: initialises DB schema (idempotent), starts WS broadcaster background task,
         schedules daily learning loop analysis.

CORS is configured for the local frontend dev server. In production (serving
the built frontend from the same origin), CORS is not needed — adjust as required.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI

import config
from api import db
from api.routes import (
    feed,
    holdings,
    journal,
    knowledge_base_route,
    promotion_gate_route,
    stats,
    system_status,
)
from api.websocket import broadcaster

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("api.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("api")


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    log.info("API starting up | backend=%s | paper_only=%s", config.DATA_BACKEND, config.PAPER_TRADING_ONLY)

    # Initialise DB (idempotent)
    await db.init_db()

    # Start WS broadcaster background task
    broadcaster_task = asyncio.create_task(
        broadcaster.poll_and_broadcast(),
        name="ws_broadcaster",
    )

    # Daily learning loop via APScheduler
    scheduler = AsyncIOScheduler()
    try:
        from learning_loop import run_daily_analysis
        scheduler.add_job(run_daily_analysis, "cron", hour=0, minute=5, id="daily_analysis")
        scheduler.start()
        log.info("APScheduler: daily analysis scheduled at 00:05")
    except Exception as exc:
        log.warning("Could not schedule daily analysis: %s", exc)

    log.info("API ready at http://%s:%d", config.API_HOST, config.API_PORT)

    yield  # ← application runs here

    # ── Shutdown ─────────────────────────────────────────────────────────────
    log.info("API shutting down")
    broadcaster_task.cancel()
    try:
        await broadcaster_task
    except asyncio.CancelledError:
        pass
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Trading Bot API",
    description="AI-assisted Solana memecoin paper-trading system. PAPER TRADING ONLY — no real funds.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow the Vite dev server and any built frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[config.FRONTEND_ORIGIN, "http://localhost:5173", "http://localhost:4173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# REST routes
# ---------------------------------------------------------------------------

api_router = APIRouter(prefix="/api")
api_router.include_router(feed.router)
api_router.include_router(holdings.router)
api_router.include_router(journal.router)
api_router.include_router(stats.router)
api_router.include_router(knowledge_base_route.router)
api_router.include_router(promotion_gate_route.router)
api_router.include_router(system_status.router)

app.include_router(api_router)


# ---------------------------------------------------------------------------
# WebSocket endpoint (FR-13)
# ---------------------------------------------------------------------------

@app.websocket("/ws/feed")
async def websocket_feed(ws: WebSocket):
    """
    WebSocket endpoint for real-time feed events.
    The client receives JSON messages of the form:
      { "type": "feed_event", "data": { ...FeedEvent fields... } }
    """
    await broadcaster.connect(ws)
    try:
        # Keep connection alive — client sends nothing, we push to it
        while True:
            try:
                await ws.receive_text()  # absorbs any pings/control frames
            except WebSocketDisconnect:
                break
    finally:
        broadcaster.disconnect(ws)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "paper_trading_only": config.PAPER_TRADING_ONLY}
