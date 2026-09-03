"""
api/websocket.py — WS /ws/feed: real-time push of new feed events (H3).

A single broadcaster task polls SQLite for new rows (cheap indexed query on
the WAL database) and fans out to all connected websocket clients. The tick
loop never touches websockets directly — a slow LLM call can't stall the API.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

import config
from api import db

log = logging.getLogger(__name__)

MAX_WS_CLIENTS: int = 32



class FeedBroadcaster:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._task: asyncio.Task | None = None
        self._last_id = 0

    async def _broadcast(self, payload: dict) -> None:
        dead: list[WebSocket] = []
        data = json.dumps(payload)
        for ws in list(self._clients):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def _poll_loop(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                try:
                    if self._clients:
                        async with db.get_db() as conn:
                            events = await db.get_feed_events(
                                conn, limit=100, offset=0
                            )
                        fresh = [e for e in reversed(events) if e["id"] > self._last_id]
                        for e in fresh:
                            self._last_id = max(self._last_id, e["id"])
                            await self._broadcast(e)
                except Exception:
                    log.warning("broadcaster poll failed", exc_info=True)
                await asyncio.sleep(2.0)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            log.info("feed broadcaster started")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def connect(self, ws: WebSocket) -> bool:
        if len(self._clients) >= MAX_WS_CLIENTS:
            log.warning("websocket rejected: maximum client capacity (%d) reached", MAX_WS_CLIENTS)
            await ws.close(code=1008, reason="maximum connections reached")
            return False
        await ws.accept()
        self._clients.add(ws)
        return True

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)


async def websocket_endpoint(ws: WebSocket, broadcaster: FeedBroadcaster) -> None:
    # SEC-04: Cross-Site WebSocket Hijacking (CSWSH) origin validation
    origin = ws.headers.get("origin")
    if origin:
        allowed = set(config.FRONTEND_ORIGINS)
        if origin not in allowed:
            log.warning("websocket rejected from unallowed origin: %s", origin)
            await ws.close(code=1008, reason="origin not allowed")
            return

    connected = await broadcaster.connect(ws)
    if not connected:
        return
    try:
        while True:
            await ws.receive_text()   # keepalive; ignore client messages
    except WebSocketDisconnect:
        broadcaster.disconnect(ws)

