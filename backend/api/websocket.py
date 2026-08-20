"""
api/websocket.py — WebSocket live feed broadcaster.

Architecture: The tick loop process writes feed_events to SQLite. This module
runs inside the FastAPI process as an asyncio background task. It polls SQLite
for new events every WS_POLL_INTERVAL_SECONDS and fans them out to all
connected WebSocket clients.

This "SQLite polling" approach avoids any IPC mechanism between the tick loop
and the API process while still delivering near-real-time updates (the default
poll interval is 2 seconds, well under any 60-second tick interval).

No event is lost: we track the highest-seen event ID and query for events
strictly greater than that ID on each poll.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import WebSocket

import config
from api import db

log = logging.getLogger(__name__)


class FeedBroadcaster:
    """
    Manages connected WebSocket clients and broadcasts new feed events.

    Usage:
        broadcaster = FeedBroadcaster()
        asyncio.create_task(broadcaster.poll_and_broadcast())

        # In the WS endpoint:
        await broadcaster.connect(websocket)
        try:
            ...
        finally:
            broadcaster.disconnect(websocket)
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._last_event_id: int = 0
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        log.info("WS client connected (%d total)", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        log.info("WS client disconnected (%d remaining)", len(self._clients))

    async def _broadcast(self, message: str) -> None:
        """Send a message to all connected clients. Disconnects dead ones."""
        dead: list[WebSocket] = []
        async with self._lock:
            clients = list(self._clients)

        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)

        async with self._lock:
            for ws in dead:
                self._clients.discard(ws)

    async def poll_and_broadcast(self) -> None:
        """
        Background task: polls SQLite for new feed events and broadcasts them.
        Runs indefinitely until cancelled (called via asyncio.create_task).
        """
        log.info("WS broadcaster started (poll_interval=%.1fs)", config.WS_POLL_INTERVAL_SECONDS)

        # Initialise the watermark to the current max ID so we don't replay
        # all historical events to newly connecting clients on startup.
        try:
            async with db.get_db() as conn:
                self._last_event_id = await db.get_max_feed_event_id(conn)
            log.info("WS broadcaster: starting from event id=%d", self._last_event_id)
        except Exception as exc:
            log.warning("WS broadcaster: could not get initial event ID: %s", exc)
            self._last_event_id = 0

        while True:
            try:
                await asyncio.sleep(config.WS_POLL_INTERVAL_SECONDS)
                await self._check_and_broadcast()
            except asyncio.CancelledError:
                log.info("WS broadcaster cancelled")
                break
            except Exception as exc:
                log.error("WS broadcaster error: %s", exc, exc_info=True)
                # Don't crash the broadcaster — sleep and retry
                await asyncio.sleep(config.WS_POLL_INTERVAL_SECONDS)

    async def _check_and_broadcast(self) -> None:
        if not self._clients:
            return  # no clients — skip DB query

        try:
            async with db.get_db() as conn:
                new_events = await db.get_feed_events_since(conn, self._last_event_id)
        except Exception as exc:
            log.error("WS poll DB error: %s", exc)
            return

        for event in new_events:
            if event.id is not None and event.id > self._last_event_id:
                self._last_event_id = event.id

            payload = json.dumps({
                "type": "feed_event",
                "data": event.to_dict(),
            })
            await self._broadcast(payload)

        if new_events:
            log.debug("WS: broadcast %d new events to %d clients", len(new_events), len(self._clients))


# Module-level singleton — imported by api/main.py and the WS route
broadcaster = FeedBroadcaster()
