"""
api/main.py — FastAPI app (H1–H5).

ENTIRELY READ-ONLY with respect to trading decisions and safety flags:
every endpoint reports state; none can open, close, or modify a trade, or
change PAPER_TRADING_ONLY. The only POST is knowledge-base ingestion, which
touches no trade state.

The tick loop normally runs as a separate process (`python main.py`) sharing
the SQLite store; set TICK_LOOP_IN_PROCESS=1 to also run it inside this app
(convenient for local demo/e2e).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import config

# live_execution is a sibling package inside backend/ (single deployable
# module), so the sanctioned function-local optional imports of live_execution
# (proof.py, disclosure.py, live_book.py) resolve natively. They stay
# try/except ImportError at every call site: a paper-only checkout still boots.
from api.routes import (
    feed,
    holdings,
    journal,
    knowledge_base,
    market_regime,
    promotion_gate,
    stats,
    system_status,
)
from api.websocket import FeedBroadcaster, websocket_endpoint
from data_providers import build_provider
from llm.narrator import Narrator

log = logging.getLogger(__name__)

broadcaster = FeedBroadcaster()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    from api import db
    await db.init_db()
    app.state.provider = build_provider()
    app.state.narrator = Narrator()
    broadcaster.start()
    tick_task: asyncio.Task | None = None
    if os.getenv("TICK_LOOP_IN_PROCESS", "0") == "1":
        import main as tick_loop
        log.info("starting in-process tick loop (TICK_LOOP_IN_PROCESS=1)")
        tick_task = asyncio.create_task(tick_loop.main())
    yield
    if tick_task is not None:
        tick_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tick_task
    await app.state.narrator.aclose()
    await broadcaster.stop()


app = FastAPI(title="trading-bot", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    # Split deployments (dashboard on Vercel, API elsewhere) may need more
    # than one origin: FRONTEND_ORIGIN accepts a comma-separated list.
    allow_origins=config.FRONTEND_ORIGINS,
    # §38 F6: only the verbs/headers the dashboard actually uses.
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)



@app.middleware("http")
async def security_headers(request, call_next):
    """§38 F5: baseline security headers on every response.

    The dashboard is served same-origin and never renders raw HTML, so these
    are defense-in-depth: nosniff stops MIME-confusion, DENY blocks click-
    jacking framing, no-referrer keeps local URLs out of third-party logs,
    and no-store keeps book/wallet data out of any cache.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault(
            "Cache-Control", "no-store")
    return response

for module in (feed, holdings, journal, stats, market_regime,
               promotion_gate, knowledge_base, system_status):
    app.include_router(module.router)

try:
    from api.routes.proof import router as proof_router
    app.include_router(proof_router)
except ImportError:
    pass  # proof endpoints optional; decision_commits table may not exist yet

try:
    from api.routes.disclosure import router as disclosure_router
    app.include_router(disclosure_router)
except ImportError:
    pass  # REF-R6 disclosure endpoints

try:
    from api.routes.admin import router as admin_router
    app.include_router(admin_router)
except ImportError:
    pass  # operator admin/reset endpoint

try:
    from api.routes.live_book import router as live_book_router
    app.include_router(live_book_router)
except ImportError:
    pass  # live book surface (read-only view of the real wallet/ledger)


@app.websocket("/ws/feed")
async def ws_feed(ws: WebSocket):
    await websocket_endpoint(ws, broadcaster)


# ---------------------------------------------------------------------------
# Optional: serve the built React dashboard from this same origin/port, so a
# single process (and a single click) exposes the whole application.
# ---------------------------------------------------------------------------
FRONTEND_DIST = config.BASE_DIR.parent / "frontend" / "dist"


@app.get("/")
async def root():
    # When the built frontend exists it is served by the SPA catch-all below;
    # this JSON root only shows when no frontend build is present.
    if not FRONTEND_DIST.exists():
        return {
            "service": "trading-bot",
            "paper_trading_only": True,
            "note": "Read-only research API. No endpoint can open, close, or "
                    "modify a trade.",
        }
    return FileResponse(FRONTEND_DIST / "index.html")


if FRONTEND_DIST.exists():
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        candidate = FRONTEND_DIST / full_path
        # Serve real files (favicon etc.); everything else gets the SPA shell.
        if full_path and ".." not in full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
