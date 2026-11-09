"""
backend/api/security_monitor.py — Real-time security event tracking and monitoring.

Guarantees:
1. Structured Security Events:
   - Tracks authentication failures, brute-force lockouts, rate limit violations,
     bot triggers, blocked static file probes, suspicious uploads, CSRF blocks,
     and 500 server errors.
2. Zero Secret Leakage:
   - Every event detail string is filtered through sanitize_error_detail before logging
     or storing. Secrets, passwords, bearer tokens, and keys are NEVER retained.
3. In-Memory Ring Buffer & Aggregates:
   - Fixed-size ring buffer for real-time operator queries via /api/admin/monitoring/security-events.
   - Aggregate statistics: event counts by type, unique flagged IPs, and top attacker IPs.
4. Bounded Deduplication:
   - Repeated identical events in a short window collapse into one ring-buffer
     entry with an `occurrences` counter, so a flood cannot evict unrelated
     evidence or turn security.log into a denial-of-service target. Both the
     dedupe map and the login-burst alert registry have hard size limits.
5. Dedicated Security Logging:
   - Dispatches formatted log lines to logger "security", writing directly to logs/security.log.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict, deque
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
    CSRF_BLOCKED = "csrf_blocked"
    INTERNAL_ERROR = "internal_error"


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
    occurrences: int = 1
    last_seen: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SecurityMonitor:
    """Thread-safe security event tracker with bounded dedupe and metrics."""

    #: Repeated identical events within this window are collapsed.
    DEDUPE_WINDOW_SECONDS = 10.0
    #: Hard cap on the dedupe map; evicts least-recently-seen keys first.
    MAX_DEDUPE_KEYS = 2_000
    #: One LOGIN_FAILURE_BURST alert per IP per window, no matter how many
    #: failed attempts arrive afterwards.
    BURST_WINDOW_SECONDS = 600.0
    MAX_BURST_KEYS = 2_000

    def __init__(
        self,
        max_events: int = 300,
        *,
        dedupe_window: float | None = None,
        max_dedupe: int | None = None,
        burst_window: float | None = None,
        max_burst: int | None = None,
        clock=time.time,
    ) -> None:
        self._events: deque[SecurityEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()
        self._type_counts: defaultdict[str, int] = defaultdict(int)
        self._ip_counts: defaultdict[str, int] = defaultdict(int)
        self._auth_failures_by_ip: defaultdict[str, deque[float]] = defaultdict(deque)
        self._dedupe_window = (
            self.DEDUPE_WINDOW_SECONDS if dedupe_window is None
            else max(0.0, float(dedupe_window))
        )
        self._max_dedupe = max(1, int(
            self.MAX_DEDUPE_KEYS if max_dedupe is None else max_dedupe))
        self._burst_window = (
            self.BURST_WINDOW_SECONDS if burst_window is None
            else max(0.0, float(burst_window))
        )
        self._max_burst = max(1, int(
            self.MAX_BURST_KEYS if max_burst is None else max_burst))
        self._clock = clock
        # key -> (event, dedupe expiry epoch). OrderedDict is the LRU bound.
        self._dedupe: OrderedDict[
            tuple[str, str, str, str, str, str], tuple[SecurityEvent, float]
        ] = OrderedDict()
        # ip -> burst-alert suppression expiry epoch.
        self._burst_alerted: OrderedDict[str, float] = OrderedDict()
        self._total_occurrences = 0

    def _prune_locked(self, now: float) -> None:
        while self._dedupe:
            _key, (_event, expires_at) = next(iter(self._dedupe.items()))
            if expires_at > now:
                break
            self._dedupe.popitem(last=False)
        while self._burst_alerted:
            _ip, expires_at = next(iter(self._burst_alerted.items()))
            if expires_at > now:
                break
            self._burst_alerted.popitem(last=False)

    def _record_burst_locked(self, type_str: str, client_ip: str,
                             now: float) -> bool:
        """Return True exactly once per IP per burst window."""
        if type_str not in ("auth_failure", "auth_lockout"):
            return False
        if client_ip == "unknown":
            return False
        cutoff = now - self._burst_window
        q = self._auth_failures_by_ip[client_ip]
        while q and q[0] < cutoff:
            q.popleft()
        q.append(now)
        if len(q) < 5:
            return False
        if client_ip in self._burst_alerted:
            return False
        self._burst_alerted[client_ip] = now + self._burst_window
        self._burst_alerted.move_to_end(client_ip)
        while len(self._burst_alerted) > self._max_burst:
            self._burst_alerted.popitem(last=False)
        return True

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
        """Record and log a security event while scrubbing all secrets.

        Identical events inside the dedupe window increment ``occurrences``
        on the retained event instead of appending a duplicate. The first
        occurrence is always logged; repeats are counted, not written, so an
        attacker cannot force unbounded log growth.
        """
        type_str = event_type.value if isinstance(event_type, SecurityEventType) else str(event_type)
        safe_detail = str(sanitize_error_detail(detail or ""))
        safe_path = html.escape(str(path or "")[:200])
        safe_ip = str(client_ip or "unknown")[:64]
        safe_method = str(method or "GET").upper()[:10]
        safe_severity = severity.upper()
        now = self._clock()
        timestamp = datetime.now(timezone.utc).isoformat()

        with self._lock:
            self._prune_locked(now)
            dedupe_key = (
                type_str, safe_ip, safe_path, safe_method, safe_detail,
                safe_severity,
            )
            retained = self._dedupe.get(dedupe_key)
            first_occurrence = retained is None
            if retained is not None:
                event = retained[0]
                event.occurrences += 1
                event.last_seen = timestamp
                self._dedupe.move_to_end(dedupe_key)
            else:
                event = SecurityEvent(
                    event_id="sec_" + secrets.token_hex(6),
                    timestamp=timestamp,
                    event_type=type_str,
                    severity=safe_severity,
                    client_ip=safe_ip,
                    path=safe_path,
                    method=safe_method,
                    detail=safe_detail,
                    error_id=error_id,
                    occurrences=1,
                    last_seen=timestamp,
                )
                self._events.append(event)
                self._dedupe[dedupe_key] = (
                    event, now + self._dedupe_window)
                while len(self._dedupe) > self._max_dedupe:
                    self._dedupe.popitem(last=False)

            self._type_counts[type_str] += 1
            self._ip_counts[safe_ip] += 1
            self._total_occurrences += 1
            burst = self._record_burst_locked(type_str, safe_ip, now)

        if first_occurrence:
            # Format and log to security logger. Repeats are counted in the
            # ring buffer and summary, never re-written to disk.
            log_line = (
                f"[{type_str.upper()}] ip={safe_ip} method={safe_method} "
                f"path={safe_path} - {safe_detail}"
            )
            if error_id:
                log_line += f" [error_id={error_id}]"
            if safe_severity in ("ERROR", "CRITICAL"):
                log.error(log_line)
            else:
                log.warning(log_line)

        if burst:
            try:
                from alerts import alert_nowait, AlertEvent
                alert_nowait(
                    AlertEvent.LOGIN_FAILURE_BURST,
                    f"Login failure burst detected: 5+ auth failures in "
                    f"{int(self._burst_window)}s from IP {safe_ip}",
                    key=f"login_burst:{safe_ip}",
                )
            except Exception:
                pass

        return event

    def get_summary(self) -> dict[str, Any]:
        """Return aggregated security summary metrics and log file statuses."""
        with self._lock:
            total_events = len(self._events)
            counts = dict(self._type_counts)
            unique_ips = len(self._ip_counts)
            # Top 5 flagged IPs
            top_ips = sorted(self._ip_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
            total_occurrences = self._total_occurrences
            retained_duplicates = sum(
                max(0, e.occurrences - 1) for e in self._events)
            dedupe_keys = len(self._dedupe)
            burst_keys = len(self._burst_alerted)

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
            "total_events_recorded": total_occurrences,
            "unique_events_retained": total_events,
            "duplicate_occurrences_retained": retained_duplicates,
            "dedupe_keys_tracked": dedupe_keys,
            "dedupe_keys_max": self._max_dedupe,
            "burst_alert_keys_tracked": burst_keys,
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
            self._auth_failures_by_ip.clear()
            self._dedupe.clear()
            self._burst_alerted.clear()
            self._total_occurrences = 0


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

