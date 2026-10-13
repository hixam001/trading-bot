"""
backend/api/security_monitor.py — Real-time security event tracking and monitoring.

Guarantees:
1. Structured Security Events:
   - Tracks authentication failures, brute-force lockouts, rate limit violations,
     bot triggers, blocked static file probes, suspicious uploads, and 500 server errors.
2. Zero Secret Leakage:
   - Every event detail string is filtered through sanitize_error_detail before logging
     or storing. Secrets, passwords, bearer tokens, and keys are NEVER retained.
3. In-Memory Ring Buffer & Aggregates:
   - Fixed-size ring buffer for real-time operator queries via /api/admin/monitoring/security-events.
   - Aggregate statistics: event counts by type, unique flagged IPs, and top attacker IPs.
4. Dedicated Security Logging:
   - Dispatches formatted log lines to logger "security", writing directly to logs/security.log.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import html
import logging
from pathlib import Path
import secrets
import threading
import time
from typing import Any

from api.error_handler import sanitize_error_detail
from api.logging_config import get_log_paths

log = logging.getLogger("security")


class SecurityEventType(str, Enum):
    AUTH_FAILURE = "auth_failure"
    AUTH_LOCKOUT = "auth_lockout"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    BOT_DETECTED = "bot_detected"
    BLOCKED_PATH_PROBE = "blocked_path_probe"
    SUSPICIOUS_UPLOAD = "suspicious_upload"
    INTERNAL_ERROR = "internal_error"
    INJECTION_PROBE = "injection_probe"


@dataclass
class SecurityEvent:
    event_id: str
    timestamp: str
    event_type: str
    severity: str
    client_ip: str
    path: str
    method: str
    detail: str
    error_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SecurityMonitor:
    """Thread-safe security event tracker with in-memory ring buffer and metrics."""

    def __init__(self, max_events: int = 300) -> None:
        self._max_events = max_events
        self._events: deque[SecurityEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()
        self._type_counts: defaultdict[str, int] = defaultdict(int)
        self._ip_counts: defaultdict[str, int] = defaultdict(int)

    def record_event(
        self,
        event_type: str | SecurityEventType,
        client_ip: str = "unknown",
        path: str = "",
        method: str = "GET",
        detail: str = "",
        severity: str = "WARNING",
        error_id: str | None = None,
    ) -> SecurityEvent:
        """Record and log a security event while scrubbing all secrets."""
        type_str = event_type.value if isinstance(event_type, SecurityEventType) else str(event_type)
        safe_detail = str(sanitize_error_detail(detail or ""))
        safe_path = html.escape(str(path or "")[:200])
        safe_ip = str(client_ip or "unknown")[:64]
        safe_method = str(method or "GET").upper()[:10]

        event = SecurityEvent(
            event_id="sec_" + secrets.token_hex(6),
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type=type_str,
            severity=severity.upper(),
            client_ip=safe_ip,
            path=safe_path,
            method=safe_method,
            detail=safe_detail,
            error_id=error_id,
        )

        with self._lock:
            self._events.append(event)
            self._type_counts[type_str] += 1
            self._ip_counts[safe_ip] += 1
            if not hasattr(self, "_auth_failures_by_ip"):
                self._auth_failures_by_ip = defaultdict(deque)
            if type_str in ("auth_failure", "auth_lockout") and safe_ip != "unknown":
                now_ts = time.time()
                q = self._auth_failures_by_ip[safe_ip]
                while q and q[0] < now_ts - 600.0:
                    q.popleft()
                q.append(now_ts)
                if len(q) >= 5:
                    try:
                        from alerts import alert_nowait, AlertEvent
                        alert_nowait(
                            AlertEvent.LOGIN_FAILURE_BURST,
                            f"Login failure burst detected: {len(q)} auth failures in 10m from IP {safe_ip}",
                            key=f"login_burst:{safe_ip}",
                        )
                    except Exception:
                        pass

        # Format and log to security logger
        log_line = f"[{type_str.upper()}] ip={safe_ip} method={safe_method} path={safe_path} - {safe_detail}"
        if error_id:
            log_line += f" [error_id={error_id}]"

        if severity.upper() in ("ERROR", "CRITICAL"):
            log.error(log_line)
        else:
            log.warning(log_line)

        return event

    def get_summary(self) -> dict[str, Any]:
        """Return aggregated security summary metrics and log file statuses."""
        with self._lock:
            total_events = len(self._events)
            counts = dict(self._type_counts)
            unique_ips = len(self._ip_counts)
            # Top 5 flagged IPs
            top_ips = sorted(self._ip_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]

        # Inspect log files on disk
        paths = get_log_paths()
        files_info: dict[str, Any] = {}
        for key in ("app_log", "error_log", "security_log"):
            p: Path = paths[key]
            exists = p.exists()
            size = p.stat().st_size if exists else 0
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat() if exists else None
            files_info[key] = {
                "path": str(p),
                "exists": exists,
                "size_bytes": size,
                "last_modified": mtime,
            }

        return {
            "monitoring_active": True,
            "total_events_recorded": total_events,
            "event_counts_by_type": counts,
            "unique_flagged_ips": unique_ips,
            "top_flagged_ips": [{"ip": ip, "count": count} for ip, count in top_ips],
            "log_directory": str(paths["log_dir"]),
            "log_files": files_info,
        }

    def get_recent_events(
        self,
        limit: int = 50,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve recent security events in reverse chronological order."""
        with self._lock:
            events_list = list(self._events)

        if event_type:
            events_list = [e for e in events_list if e.event_type.lower() == event_type.lower()]

        # Newest first
        events_list.reverse()
        return [e.to_dict() for e in events_list[:limit]]

    def clear(self) -> None:
        """Clear memory buffers (used for testing)."""
        with self._lock:
            self._events.clear()
            self._type_counts.clear()
            self._ip_counts.clear()


# Global security monitor singleton
security_monitor = SecurityMonitor()


def record_security_event(
    event_type: str | SecurityEventType,
    client_ip: str = "unknown",
    path: str = "",
    method: str = "GET",
    detail: str = "",
    severity: str = "WARNING",
    error_id: str | None = None,
) -> SecurityEvent:
    """Helper to record an event on the global security monitor."""
    return security_monitor.record_event(
        event_type=event_type,
        client_ip=client_ip,
        path=path,
        method=method,
        detail=detail,
        severity=severity,
        error_id=error_id,
    )

