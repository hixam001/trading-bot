"""
data_providers/new_listings.py — Birdeye SUBSCRIBE_TOKEN_NEW_LISTING feed.

Second discovery lens (Task A): surfaces genuinely NEW tokens that the sticky
trending ranking never shows. Confirmed against Birdeye docs
(docs.birdeye.so/reference/new-token-listing.md):
  wss://public-api.birdeye.so/socket/solana?x-api-key=KEY
  send {"type": "SUBSCRIBE_TOKEN_NEW_LISTING"}
  recv {"type": "TOKEN_NEW_LISTING_DATA",
        "data": {address, decimals, name, symbol,
                 liquidity (str), liquidityAddedAt (unix s)}}

Graceful degradation: if the key's plan rejects the subscription or the
socket cannot connect, this feed disables itself for the session after a
bounded number of attempts; the stack continues trending-only. It never
raises into the tick loop.

Events are buffered (deduped by mint, newest wins) and drained as Candidate
stubs by LiveProviderStack.get_candidates(); Dexscreener enrichment fills the
rule numerics exactly as it does for trending candidates.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import config
from data_providers.base import require_type

log = logging.getLogger(__name__)

_SOCKET_URL = "wss://public-api.birdeye.so/socket/solana"
_MAX_BUFFER = 200
_MAX_CONNECT_ATTEMPTS = 3


class NewListingFeed:
    """Buffered SUBSCRIBE_TOKEN_NEW_LISTING consumer; degrade-gracefully."""

    def __init__(self) -> None:
        self._events: dict[str, dict] = {}   # mint -> payload (dedup, newest wins)
        self._order: list[str] = []          # insertion order for FIFO drain
        self._task: asyncio.Task | None = None
        self._enabled = True                 # flips off permanently on plan/auth errors
        self._stop_evt = asyncio.Event()

    @property
    def available(self) -> bool:
        return self._enabled

    def start(self) -> None:
        if not config.BIRDEYE_API_KEY:
            log.warning("new_listings: no BIRDEYE_API_KEY — lens disabled")
            self._enabled = False
            return
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="new-listing-feed")

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass
            self._task = None

    def drain(self, limit: int) -> list[dict]:
        """
        Pop buffered listing events (oldest first), up to limit. Each item:
        {mint_address, symbol, name, decimals, liquidity_usd, discovered_at}.
        """
        out: list[dict] = []
        while self._order and len(out) < limit:
            mint = self._order.pop(0)
            ev = self._events.pop(mint, None)
            if ev is not None:
                out.append(ev)
        return out

    # ------------------------------------------------------------------ #

    def _buffer(self, data: dict) -> None:
        mint = require_type(data.get("address"), str, "address", "new_listings")
        if not mint:
            return
        liq_raw = data.get("liquidity")
        if isinstance(liq_raw, str):
            try:
                liq_raw = float(liq_raw)
            except ValueError:
                liq_raw = None
        entry = {
            "mint_address": mint,
            "symbol": str(require_type(data.get("symbol"), str, "symbol", "new_listings") or "?"),
            "name": str(require_type(data.get("name"), str, "name", "new_listings") or ""),
            "decimals": require_type(data.get("decimals"), int, "decimals", "new_listings"),
            "liquidity_usd": require_type(liq_raw, (int, float), "liquidity", "new_listings"),
            "discovered_at": time.time(),
        }
        if mint not in self._events:
            self._order.append(mint)
        self._events[mint] = entry
        while len(self._order) > _MAX_BUFFER:      # bound memory
            self._events.pop(self._order.pop(0), None)

    async def _run(self) -> None:
        import websockets

        attempts = 0
        while not self._stop_evt.is_set() and self._enabled:
            try:
                headers = {"Origin": "ws://public-api.birdeye.so"}
                async with websockets.connect(
                    f"{_SOCKET_URL}?x-api-key={config.BIRDEYE_API_KEY}",
                    additional_headers=headers,
                    ping_interval=20,
                ) as ws:
                    attempts = 0                     # reset on successful connect
                    self._enabled = True
                    await ws.send(json.dumps({"type": "SUBSCRIBE_TOKEN_NEW_LISTING"}))
                    log.info("new_listings: subscribed (SUBSCRIBE_TOKEN_NEW_LISTING)")
                    while not self._stop_evt.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=120.0)
                        try:
                            frame = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(frame, dict) \
                                and frame.get("type") == "TOKEN_NEW_LISTING_DATA":
                            self._buffer(frame.get("data") or {})
                        # Auth/plan rejections arrive as error frames or an
                        # immediate close; both fall through to the handlers
                        # below via the normal exception path.
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts += 1
                self._enabled = False
                if attempts >= _MAX_CONNECT_ATTEMPTS:
                    log.warning(
                        "new_listings: disabled for this session after %d "
                        "connect attempts (%s). Discovery continues "
                        "trending-only.", _MAX_CONNECT_ATTEMPTS, exc)
                    return
                wait = 5.0 * attempts
                log.warning("new_listings: connect failed (%s) — retry %d/%d "
                            "in %.0fs", exc, attempts, _MAX_CONNECT_ATTEMPTS, wait)
                try:
                    await asyncio.wait_for(self._stop_evt.wait(), timeout=wait)
                    return
                except asyncio.TimeoutError:
                    self._enabled = True             # retry allowed


