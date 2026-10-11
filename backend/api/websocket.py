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

from collections import defaultdict
from fastapi import WebSocket, WebSocketDisconnect

import config
from api import db
from api.rate_limiter import get_client_ip

log = logging.getLogger(__name__)

MAX_WS_CLIENTS: int = 32
MAX_WS_PER_IP: int = 4



class FeedBroadcaster:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._clients_by_ip: dict[str, set[WebSocket]] = defaultdict(set)
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
            self.disconnect(ws)

    async def _poll_loop(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                try:
                    if self._clients:
                        async with db.get_db() as conn:
                            if self._last_id == 0:
                                recent = await db.get_feed_events(conn, limit=1, offset=0)
                                if recent:
                                    self._last_id = recent[0]["id"]
                            fresh = await db.get_feed_events_since(
                                conn, since_id=self._last_id, limit=50
                            )
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

    async def connect(self, ws: WebSocket, client_ip: str | None = None) -> bool:
        if client_ip is not None:
            max_per_ip = getattr(config, "MAX_WS_PER_IP", MAX_WS_PER_IP)
            if len(self._clients_by_ip[client_ip]) >= max_per_ip:
                log.warning(
                    "websocket rejected: maximum connections per IP (%d) reached for %s",
                    max_per_ip,
                    client_ip,
                )
                await ws.close(code=1008, reason=f"maximum connections per IP ({max_per_ip}) reached")
                return False

        if len(self._clients) >= MAX_WS_CLIENTS:
            log.warning("websocket rejected: maximum client capacity (%d) reached", MAX_WS_CLIENTS)
            await ws.close(code=1008, reason="maximum connections reached")
            return False

        await ws.accept()
        self._clients.add(ws)
        if client_ip is not None:
            self._clients_by_ip[client_ip].add(ws)
        return True

    def disconnect(self, ws: WebSocket, client_ip: str | None = None) -> None:
        self._clients.discard(ws)
        if client_ip:
            self._clients_by_ip[client_ip].discard(ws)
            if not self._clients_by_ip[client_ip]:
                self._clients_by_ip.pop(client_ip, None)
        else:
            for ip in list(self._clients_by_ip.keys()):
                self._clients_by_ip[ip].discard(ws)
                if not self._clients_by_ip[ip]:
                    self._clients_by_ip.pop(ip, None)


async def websocket_endpoint(ws: WebSocket, broadcaster: FeedBroadcaster) -> None:
    client_ip = get_client_ip(ws)

    # SEC-04: Cross-Site WebSocket Hijacking (CSWSH) origin validation
    origin = ws.headers.get("origin")
    if origin:
        allowed = set(config.FRONTEND_ORIGINS)
        if origin not in allowed:
            log.warning("websocket rejected from unallowed origin: %s", origin)
            await ws.close(code=1008, reason="origin not allowed")
            return

    connected = await broadcaster.connect(ws, client_ip=client_ip)
    if not connected:
        return
    try:
        while True:
            try:
                # 30s server-side ping/timeout to reap dead half-open connections (§77 E3)
                await asyncio.wait_for(ws.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # Verify socket liveness with zero-byte frame
                await ws.send_bytes(b"")
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        broadcaster.disconnect(ws, client_ip=client_ip)

