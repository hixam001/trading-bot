"""
backend/alerts.py — Outbound alerting subsystem (§79 Phase D).

Provides multi-channel outbound alerts (ntfy, Telegram) for critical trading events:
kill-switch engagement, daily-loss breaker trips, guard/simulation refusals,
stale heartbeat, low SOL/USDC reserves, unexplained balance drops, login failure bursts, etc.

Key invariants:
1. Non-blocking & fail-safe: Enqueues to an in-process asyncio.Queue(maxsize=100).
   Trading code is never blocked and alert failures NEVER raise into execution logic.
2. Secret protection: Every outbound message is sanitized against secret_registry
   and config secrets before transmission. Secret tokens are registered on load.
3. Cooldown & flood control:
   - Per-key cooldown: 15 min default, 5 min for CRITICAL.
   - Global flood cap: 20 messages/hour, with a suppressed count summary.
   - Refusal aggregation: first refusal fires immediately; subsequent refusals in a
     30m window aggregate into a single digest.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import logging
import os
import re
import time
from typing import Any, Callable, Optional
import httpx

from secret_registry import get_runtime_secrets, register_secret

log = logging.getLogger("alerts")

# ---------------------------------------------------------------------------
# Configuration & Secret Registration
# ---------------------------------------------------------------------------
ALERT_NTFY_URL = os.getenv("ALERT_NTFY_URL", "https://ntfy.sh").rstrip("/")
ALERT_NTFY_TOPIC = os.getenv("ALERT_NTFY_TOPIC", "").strip()
ALERT_NTFY_TOKEN = os.getenv("ALERT_NTFY_TOKEN", "").strip()

ALERT_TELEGRAM_BOT_TOKEN = os.getenv("ALERT_TELEGRAM_BOT_TOKEN", "").strip()
ALERT_TELEGRAM_CHAT_ID = os.getenv("ALERT_TELEGRAM_CHAT_ID", "").strip()

# Register channel tokens immediately to ensure they are redacted from logs and messages
if ALERT_NTFY_TOKEN:
    register_secret(ALERT_NTFY_TOKEN)
if ALERT_TELEGRAM_BOT_TOKEN:
    register_secret(ALERT_TELEGRAM_BOT_TOKEN)


# Guessable / shared default topics that must be refused on public ntfy without auth
FORBIDDEN_NTFY_TOPICS: frozenset[str] = frozenset({
    "trading-bot",
    "trading-bot-alerts",
    "alerts",
    "tradingbot",
    "default",
    "test",
})


def is_ntfy_misconfigured() -> bool:
    """Return True if ntfy topic is guessable on unauthenticated/public server (§81 Phase H-1)."""
    topic = os.getenv("ALERT_NTFY_TOPIC", ALERT_NTFY_TOPIC).strip()
    url = os.getenv("ALERT_NTFY_URL", ALERT_NTFY_URL).rstrip("/")
    token = os.getenv("ALERT_NTFY_TOKEN", ALERT_NTFY_TOKEN).strip()
    if not topic:
        return False
    # If topic is guessable on public ntfy server without authentication
    if ("ntfy.sh" in url or not token) and topic.lower() in FORBIDDEN_NTFY_TOPICS:
        return True
    return False


def _alert_info_enabled() -> bool:
    return os.getenv("ALERT_INFO", "0").lower() in ("1", "true", "yes", "on")


def get_configured_channels() -> list[str]:
    """Return list of active outbound alert channels based on current env."""
    topic = os.getenv("ALERT_NTFY_TOPIC", ALERT_NTFY_TOPIC).strip()
    tg_token = os.getenv("ALERT_TELEGRAM_BOT_TOKEN", ALERT_TELEGRAM_BOT_TOKEN).strip()
    tg_chat = os.getenv("ALERT_TELEGRAM_CHAT_ID", ALERT_TELEGRAM_CHAT_ID).strip()
    channels = []
    if topic:
        if is_ntfy_misconfigured():
            log.error(
                "ntfy alert channel misconfigured: topic '%s' is an insecure/guessable default name on public ntfy. "
                "Refusing to broadcast alerts to public topic. Generate a private topic with 'openssl rand -hex 12'.",
                topic,
            )
        else:
            channels.append("ntfy")
    if tg_token and tg_chat:
        channels.append("telegram")
    return channels


# ---------------------------------------------------------------------------
# Event Types & Priorities
# ---------------------------------------------------------------------------
class AlertPriority(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    INFO = "INFO"


class AlertEvent(str, Enum):
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    DAILY_LOSS_BREAKER = "DAILY_LOSS_BREAKER"
    SIM_REFUSAL = "SIM_REFUSAL"
    GUARD_REFUSAL = "GUARD_REFUSAL"
    EXIT_UNRESOLVED = "EXIT_UNRESOLVED"
    SIM_SINGLE_RPC_SELL = "SIM_SINGLE_RPC_SELL"
    SELL_NO_REF_PRICE = "SELL_NO_REF_PRICE"
    RPC_NOT_DIVERSE = "RPC_NOT_DIVERSE"
    LIVE_BOOT = "LIVE_BOOT"
    HEARTBEAT_STALE = "HEARTBEAT_STALE"
    HEARTBEAT_RECOVERED = "HEARTBEAT_RECOVERED"
    LOW_SOL = "LOW_SOL"
    LOW_USDC = "LOW_USDC"
    WALLET_DROP = "WALLET_DROP"
    LOGIN_FAILURE_BURST = "LOGIN_FAILURE_BURST"
    TEST = "TEST"
    INFO = "INFO"


EVENT_DEFAULT_PRIORITY: dict[AlertEvent, AlertPriority] = {
    AlertEvent.KILL_SWITCH_ENGAGED: AlertPriority.CRITICAL,
    AlertEvent.DAILY_LOSS_BREAKER: AlertPriority.CRITICAL,
    AlertEvent.WALLET_DROP: AlertPriority.CRITICAL,
    AlertEvent.HEARTBEAT_STALE: AlertPriority.CRITICAL,
    AlertEvent.SIM_REFUSAL: AlertPriority.HIGH,
    AlertEvent.GUARD_REFUSAL: AlertPriority.HIGH,
    AlertEvent.EXIT_UNRESOLVED: AlertPriority.HIGH,
    AlertEvent.RPC_NOT_DIVERSE: AlertPriority.HIGH,
    AlertEvent.LOW_SOL: AlertPriority.HIGH,
    AlertEvent.LOW_USDC: AlertPriority.HIGH,
    AlertEvent.SIM_SINGLE_RPC_SELL: AlertPriority.HIGH,
    AlertEvent.SELL_NO_REF_PRICE: AlertPriority.HIGH,
    AlertEvent.LOGIN_FAILURE_BURST: AlertPriority.HIGH,
    AlertEvent.LIVE_BOOT: AlertPriority.INFO,
    AlertEvent.HEARTBEAT_RECOVERED: AlertPriority.INFO,
    AlertEvent.TEST: AlertPriority.INFO,
    AlertEvent.INFO: AlertPriority.INFO,
}

# ---------------------------------------------------------------------------
# Outbound Sanitizer
# ---------------------------------------------------------------------------
_SOLANA_KEYPAIR_RE = re.compile(r"\[\s*\d+(?:\s*,\s*\d+){40,}\s*\]")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.~+/]+=*")
_BASIC_AUTH_URL_RE = re.compile(r"(https?://)([^:\s/]+):([^@\s/]+)@")
_JSON_SENSITIVE_RE = re.compile(
    r"""(?i)(['"](?:api[_-]?key|token|secret|password)['"]\s*:\s*)['"]([^'"]+)['"]"""
)
_SENSITIVE_PARAM_RE = re.compile(
    r"""(?i)([?&](?:api[_-]?key|token|secret|password)=)[^&#\s]+"""
)


def sanitize_alert_text(text: str) -> str:
    """Sanitize message content to ensure no secrets or keys leak in push notifications."""
    if not isinstance(text, str):
        return str(text)

    sanitized = text

    # Mask secrets from runtime registry
    try:
        for sec in get_runtime_secrets():
            if sec and len(sec) >= 6:
                sanitized = sanitized.replace(sec, "<REDACTED>")
    except Exception:
        pass

    # Mask config secrets
    try:
        import config
        for attr in dir(config):
            if any(attr.endswith(suffix) for suffix in ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD")) and not attr.startswith("_"):
                val = getattr(config, attr, None)
                if val and isinstance(val, str) and len(val.strip()) >= 6:
                    sanitized = sanitized.replace(val.strip(), "<REDACTED>")
    except Exception:
        pass

    # Mask explicit channel tokens from current environment
    curr_ntfy_tok = os.getenv("ALERT_NTFY_TOKEN", ALERT_NTFY_TOKEN)
    if curr_ntfy_tok and len(curr_ntfy_tok) >= 6:
        sanitized = sanitized.replace(curr_ntfy_tok, "<REDACTED>")
    curr_tg_tok = os.getenv("ALERT_TELEGRAM_BOT_TOKEN", ALERT_TELEGRAM_BOT_TOKEN)
    if curr_tg_tok and len(curr_tg_tok) >= 6:
        sanitized = sanitized.replace(curr_tg_tok, "<REDACTED>")

    # Mask structural patterns
    sanitized = _SOLANA_KEYPAIR_RE.sub("[<REDACTED-KEYPAIR>]", sanitized)
    sanitized = _BEARER_RE.sub("Bearer <REDACTED>", sanitized)
    sanitized = _BASIC_AUTH_URL_RE.sub(r"\1***:***@", sanitized)
    sanitized = _JSON_SENSITIVE_RE.sub(r'\1"<REDACTED>"', sanitized)
    sanitized = _SENSITIVE_PARAM_RE.sub(r"\1=<REDACTED>", sanitized)
    return sanitized


# ---------------------------------------------------------------------------
# In-Memory Queue, State & Worker
# ---------------------------------------------------------------------------
@dataclass
class AlertItem:
    event: AlertEvent
    message: str
    key: str
    priority: AlertPriority
    timestamp: float
    fields: dict[str, Any] = field(default_factory=dict)


_pending_alerts: deque[AlertItem] = deque(maxlen=100)
_queue: Optional[asyncio.Queue[AlertItem]] = None
_worker_task: Optional[asyncio.Task] = None

# Metrics and observability
_dropped_count: int = 0
_suppressed_count: int = 0
_last_sent_at: Optional[str] = None
_sent_timestamps: deque[float] = deque(maxlen=100)

# Cooldown and aggregation state
_key_last_sent: dict[str, float] = {}
_refusal_window_start: float = 0.0
_refusal_count: int = 0
_refusal_latest_msg: str = ""

# Pluggable transport hook for tests
_custom_transport_hook: Optional[Callable[[str, str, dict, str], Any]] = None


def get_alerts_status() -> dict[str, Any]:
    """Expose alert subsystem status for /api/safety (never secrets)."""
    return {
        "channels": get_configured_channels(),
        "alerts": "misconfigured" if is_ntfy_misconfigured() else "ok",
        "last_sent_at": _last_sent_at,
        "queued": (_queue.qsize() if _queue is not None else len(_pending_alerts)),
        "dropped": _dropped_count,
        "suppressed": _suppressed_count,
    }


def _ensure_worker_running() -> None:
    global _queue, _worker_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    if _queue is not None:
        try:
            q_loop = getattr(_queue, "_loop", None)
            if q_loop is None and hasattr(_queue, "_get_loop"):
                q_loop = _queue._get_loop()
            if q_loop is not None and q_loop is not loop:
                _queue = asyncio.Queue(maxsize=100)
                _worker_task = None
        except Exception:
            _queue = asyncio.Queue(maxsize=100)
            _worker_task = None

    if _queue is None:
        _queue = asyncio.Queue(maxsize=100)

    # Flush any pending alerts from cold start
    while _pending_alerts:
        item = _pending_alerts.popleft()
        if not _queue.full():
            _queue.put_nowait(item)
        else:
            global _dropped_count
            _dropped_count += 1

    if _worker_task is None or _worker_task.done():
        _worker_task = loop.create_task(_alert_worker_loop())


async def _dispatch_to_ntfy(client: httpx.AsyncClient, item: AlertItem, text: str) -> None:
    topic = os.getenv("ALERT_NTFY_TOPIC", ALERT_NTFY_TOPIC).strip()
    if not topic:
        return
    if is_ntfy_misconfigured():
        log.error("ntfy dispatch skipped: topic '%s' is an insecure/guessable default name on public ntfy", topic)
        return
    url = f"{os.getenv('ALERT_NTFY_URL', ALERT_NTFY_URL).rstrip('/')}/{topic}"
    token = os.getenv("ALERT_NTFY_TOKEN", ALERT_NTFY_TOKEN).strip()
    headers = {
        "Title": f"[{item.priority.value}] {item.event.value}",
        "Priority": "urgent" if item.priority == AlertPriority.CRITICAL else ("high" if item.priority == AlertPriority.HIGH else "default"),
        "Tags": "rotating_light" if item.priority == AlertPriority.CRITICAL else "warning",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if _custom_transport_hook:
        res = _custom_transport_hook("ntfy", url, headers, text)
        if asyncio.iscoroutine(res):
            await res
        return

    resp = await client.post(url, content=text.encode("utf-8"), headers=headers, timeout=5.0)
    resp.raise_for_status()


async def _dispatch_to_telegram(client: httpx.AsyncClient, item: AlertItem, text: str) -> None:
    bot_token = os.getenv("ALERT_TELEGRAM_BOT_TOKEN", ALERT_TELEGRAM_BOT_TOKEN).strip()
    chat_id = os.getenv("ALERT_TELEGRAM_CHAT_ID", ALERT_TELEGRAM_CHAT_ID).strip()
    if not bot_token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": f"🚨 *[{item.priority.value}] {item.event.value}*\n\n{text}",
        "parse_mode": "Markdown",
    }

    if _custom_transport_hook:
        res = _custom_transport_hook("telegram", url, payload, text)
        if asyncio.iscoroutine(res):
            await res
        return

    try:
        resp = await client.post(url, json=payload, timeout=5.0)
        resp.raise_for_status()
    except Exception:
        # Retry with plain text in case markdown parsing failed on special characters
        payload.pop("parse_mode", None)
        payload["text"] = f"[{item.priority.value}] {item.event.value}\n\n{text}"
        resp = await client.post(url, json=payload, timeout=5.0)
        resp.raise_for_status()


async def _send_item(item: AlertItem) -> bool:
    channels = get_configured_channels()
    safe_text = sanitize_alert_text(item.message)
    if not channels and not _custom_transport_hook:
        log.info("ALERT [%s] %s: %s (no outbound channels configured)", item.priority.value, item.event.value, safe_text)
        return True

    async with httpx.AsyncClient(timeout=5.0) as client:
        tasks = []
        if "ntfy" in channels or _custom_transport_hook:
            tasks.append(_dispatch_to_ntfy(client, item, safe_text))
        if "telegram" in channels or _custom_transport_hook:
            tasks.append(_dispatch_to_telegram(client, item, safe_text))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        any_success = False
        for r in results:
            if isinstance(r, Exception):
                log.error("Outbound alert delivery failed: %s", r)
            else:
                any_success = True
        return any_success or bool(_custom_transport_hook)


async def _alert_worker_loop() -> None:
    """Single background worker loop draining _queue."""
    global _last_sent_at, _suppressed_count, _refusal_window_start, _refusal_count, _refusal_latest_msg
    while True:
        assert _queue is not None
        item = await _queue.get()
        try:
            now = time.time()

            # 1. INFO level filter
            if item.priority == AlertPriority.INFO and not _alert_info_enabled() and item.event != AlertEvent.TEST:
                log.info("ALERT [%s] %s: %s (INFO suppressed)", item.priority.value, item.event.value, item.message)
                continue

            # 2. Refusal aggregation logic (SIM_REFUSAL / GUARD_REFUSAL)
            if item.event in (AlertEvent.SIM_REFUSAL, AlertEvent.GUARD_REFUSAL):
                if _refusal_window_start == 0.0 or (now - _refusal_window_start >= 1800.0):
                    if _refusal_count > 1 and (now - _refusal_window_start >= 1800.0):
                        digest_msg = f"Refusal digest: {_refusal_count} refusals in last 30m. Latest: {_refusal_latest_msg}"
                        digest_item = AlertItem(
                            event=item.event,
                            message=digest_msg,
                            key="refusal_digest",
                            priority=AlertPriority.HIGH,
                            timestamp=now,
                        )
                        ok = await _send_item(digest_item)
                        if ok:
                            _last_sent_at = datetime.now(timezone.utc).isoformat()
                            _sent_timestamps.append(now)

                    _refusal_window_start = now
                    _refusal_count = 1
                    _refusal_latest_msg = item.message
                    # Send this first refusal immediately
                else:
                    _refusal_count += 1
                    _refusal_latest_msg = item.message
                    _suppressed_count += 1
                    continue

            # 3. Per-key cooldown deduplication
            cooldown_sec = 300.0 if item.priority == AlertPriority.CRITICAL else 900.0
            last_sent = _key_last_sent.get(item.key, 0.0)
            if item.event != AlertEvent.TEST and (now - last_sent < cooldown_sec):
                _suppressed_count += 1
                log.debug("Alert key %s suppressed by cooldown (%.1fs < %.1fs)", item.key, now - last_sent, cooldown_sec)
                continue

            # 4. Global flood cap: 20 messages / rolling hour
            while _sent_timestamps and _sent_timestamps[0] < now - 3600.0:
                _sent_timestamps.popleft()

            if len(_sent_timestamps) >= 20 and item.event != AlertEvent.TEST:
                _suppressed_count += 1
                log.warning("Alert flood cap reached (20/hr); alert %s suppressed", item.event.value)
                continue

            # 5. Dispatch
            ok = await _send_item(item)
            if ok:
                _last_sent_at = datetime.now(timezone.utc).isoformat()
                _sent_timestamps.append(now)
                _key_last_sent[item.key] = now

        except Exception as exc:
            # Alert worker must NEVER crash
            log.exception("Unexpected error in alert worker loop: %s", exc)
        finally:
            _queue.task_done()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def alert_nowait(
    event: AlertEvent,
    message: str,
    *,
    key: Optional[str] = None,
    priority: Optional[AlertPriority | str] = None,
    **fields: Any,
) -> None:
    """Enqueue an alert synchronously. Never raises and never blocks trading."""
    try:
        prio = (
            AlertPriority(priority)
            if priority is not None
            else EVENT_DEFAULT_PRIORITY.get(event, AlertPriority.HIGH)
        )
        dedupe_key = str(key) if key is not None else event.value
        item = AlertItem(
            event=event,
            message=str(message),
            key=dedupe_key,
            priority=prio,
            timestamp=time.time(),
            fields=fields,
        )

        global _dropped_count
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            global _queue
            if _queue is None:
                _queue = asyncio.Queue(maxsize=100)
            if _queue.full():
                try:
                    _queue.get_nowait()
                    _dropped_count += 1
                except Exception:
                    pass
            try:
                _queue.put_nowait(item)
            except Exception:
                pass
            _ensure_worker_running()
        else:
            if len(_pending_alerts) >= 100:
                _pending_alerts.popleft()
                _dropped_count += 1
            _pending_alerts.append(item)
    except Exception as exc:
        # Alerting failure must NEVER raise into caller
        log.error("Failed to enqueue alert: %s", exc)


async def alert(
    event: AlertEvent,
    message: str,
    *,
    key: Optional[str] = None,
    priority: Optional[AlertPriority | str] = None,
    **fields: Any,
) -> None:
    """Async variant of alert_nowait. Returns immediately without blocking."""
    alert_nowait(event, message, key=key, priority=priority, **fields)


async def drain(timeout: float = 5.0) -> None:
    """Drain queue (testing helper)."""
    _ensure_worker_running()
    if _queue is not None:
        try:
            await asyncio.wait_for(_queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            pass


async def send_test_alert(channel: Optional[str] = None) -> dict[str, Any]:
    """Send a test alert and return delivery results (used by scripts/send_test_alert.py)."""
    channels = [channel] if channel else get_configured_channels()
    if not channels:
        return {"status": "no_channels_configured", "channels": []}

    item = AlertItem(
        event=AlertEvent.TEST,
        message="This is a test alert from trading-bot §79 outbound alerting.",
        key="test_alert",
        priority=AlertPriority.INFO,
        timestamp=time.time(),
    )
    safe_text = sanitize_alert_text(item.message)
    results = {}

    async with httpx.AsyncClient(timeout=5.0) as client:
        if "ntfy" in channels:
            topic = os.getenv("ALERT_NTFY_TOPIC", ALERT_NTFY_TOPIC).strip()
            url = f"{os.getenv('ALERT_NTFY_URL', ALERT_NTFY_URL).rstrip('/')}/{topic}"
            token = os.getenv("ALERT_NTFY_TOKEN", ALERT_NTFY_TOKEN).strip()
            headers = {"Title": "[TEST] trading-bot Alert", "Priority": "default", "Tags": "white_check_mark"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                r = await client.post(url, content=safe_text.encode("utf-8"), headers=headers)
                results["ntfy"] = {"ok": r.is_success, "status_code": r.status_code}
            except Exception as e:
                results["ntfy"] = {"ok": False, "error": sanitize_alert_text(str(e))}

        if "telegram" in channels:
            bot_token = os.getenv("ALERT_TELEGRAM_BOT_TOKEN", ALERT_TELEGRAM_BOT_TOKEN).strip()
            chat_id = os.getenv("ALERT_TELEGRAM_CHAT_ID", ALERT_TELEGRAM_CHAT_ID).strip()
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {"chat_id": chat_id, "text": f"✅ *[TEST] trading-bot Alert*\n\n{safe_text}", "parse_mode": "Markdown"}
            try:
                r = await client.post(url, json=payload)
                results["telegram"] = {"ok": r.is_success, "status_code": r.status_code}
            except Exception as e:
                results["telegram"] = {"ok": False, "error": sanitize_alert_text(str(e))}

    return results


# ---------------------------------------------------------------------------
# Background Heartbeat Watcher
# ---------------------------------------------------------------------------
async def heartbeat_watcher(
    state_dir: Optional[Any] = None,
    check_interval: float = 60.0,
    stale_threshold: float = 300.0,
    stop_event: Optional[asyncio.Event] = None,
    now_fn: Callable[[], float] = time.time,
) -> None:
    """Watcher task for the API process checking heartbeat.txt freshness."""
    from pathlib import Path
    try:
        from live_execution import config as live_config
        target_dir = Path(state_dir or live_config.STATE_DIR)
    except Exception:
        target_dir = Path(state_dir or "/tmp/trading_state")

    hb_file = target_dir / "heartbeat.txt"
    was_stale = False
    last_stale_alert_ts = 0.0

    while True:
        try:
            now = now_fn()
            stale = True
            age = float("inf")
            if hb_file.is_file():
                try:
                    content = hb_file.read_text().strip()
                    mtime = float(content)
                except (ValueError, OSError):
                    try:
                        mtime = hb_file.stat().st_mtime
                    except OSError:
                        mtime = 0.0
                age = now - mtime
                stale = (age > stale_threshold)

            if stale:
                if not was_stale:
                    was_stale = True
                    last_stale_alert_ts = now
                    age_str = f"{age:.0f}s" if age != float("inf") else "missing"
                    alert_nowait(
                        AlertEvent.HEARTBEAT_STALE,
                        f"Live trading cycle heartbeat is stale ({age_str} > {stale_threshold:.0f}s)",
                        key="heartbeat_stale",
                    )
                else:
                    if now - last_stale_alert_ts >= 1800.0:
                        last_stale_alert_ts = now
                        age_str = f"{age:.0f}s" if age != float("inf") else "missing"
                        alert_nowait(
                            AlertEvent.HEARTBEAT_STALE,
                            f"Live trading cycle heartbeat remains stale ({age_str} > {stale_threshold:.0f}s)",
                            key="heartbeat_stale",
                        )
            else:
                if was_stale:
                    was_stale = False
                    alert_nowait(
                        AlertEvent.HEARTBEAT_RECOVERED,
                        f"Live trading cycle heartbeat recovered (age {age:.0f}s)",
                        key="heartbeat_stale",
                    )
        except Exception as exc:
            log.warning("Heartbeat watcher check failed: %s", exc)

        if stop_event and stop_event.is_set():
            break

        try:
            await asyncio.sleep(check_interval)
        except asyncio.CancelledError:
            break
