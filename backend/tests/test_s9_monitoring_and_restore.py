"""
backend/tests/test_s9_monitoring_and_restore.py — §92 S9 detection & recovery.

Two operator promises are tested here:

1. **Every security control leaves evidence, and a flood cannot erase it.**
   Each SecurityEventType has a production emitter; schema-rejected uploads
   are recorded (they previously vanished before the route handler); repeated
   identical events collapse into one bounded event with an occurrence count;
   and a login-failure burst alerts once per IP/window, not once per request.

2. **A backup can actually be restored.** A v2 archive is created, restored
   into a throwaway directory with a real SQLite book, verified row-for-row,
   checked for excluded key material, and left with the kill switch engaged.

No network, wallet, or real state directory is touched.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi.routing import APIRoute

import alerts
import config
from api import db
from api import rate_limiter
from api.main import app
from api.security_monitor import (
    SecurityEventType,
    SecurityMonitor,
    security_monitor,
)
from scripts import backup_state, restore_state

REPO_ROOT = Path(config.BASE_DIR).parent


@pytest_asyncio.fixture(autouse=True)
async def isolated_monitor(tmp_path, monkeypatch):
    """Keep monitor, limiter, DB, and logging state per test."""
    security_monitor.clear()
    rate_limiter.limiter.clear()
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    monkeypatch.setattr(config, "DEBUG", False)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    config.DB_PATH = tmp_path / "s9_monitor.db"
    await db.init_db()
    yield
    security_monitor.clear()
    rate_limiter.limiter.clear()


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --------------------------------------------------------------- dedupe/core

def test_repeated_events_collapse_but_keep_the_true_count():
    now = [1_000.0]
    monitor = SecurityMonitor(clock=lambda: now[0])
    for _ in range(100):
        monitor.record_event(
            SecurityEventType.RATE_LIMIT_EXCEEDED,
            client_ip="203.0.113.9",
            path="/api/feed",
            detail="Rate limit exceeded for tier 'general' (limit 120/min)",
        )

    events = monitor.get_recent_events()
    assert len(events) == 1
    assert events[0]["occurrences"] == 100
    assert events[0]["last_seen"] >= events[0]["timestamp"]

    summary = monitor.get_summary()
    assert summary["total_events_recorded"] == 100
    assert summary["unique_events_retained"] == 1
    assert summary["duplicate_occurrences_retained"] == 99
    assert summary["event_counts_by_type"]["rate_limit_exceeded"] == 100
    assert summary["dedupe_keys_tracked"] == 1

    # After the window expires the next identical event is new evidence.
    now[0] += monitor.DEDUPE_WINDOW_SECONDS + 1
    monitor.record_event(
        SecurityEventType.RATE_LIMIT_EXCEEDED,
        client_ip="203.0.113.9",
        path="/api/feed",
        detail="Rate limit exceeded for tier 'general' (limit 120/min)",
    )
    assert len(monitor.get_recent_events()) == 2


def test_dedupe_map_is_hard_bounded():
    now = [2_000.0]
    monitor = SecurityMonitor(max_dedupe=3, clock=lambda: now[0])
    for i in range(50):
        monitor.record_event(
            SecurityEventType.BLOCKED_PATH_PROBE,
            client_ip=f"198.51.100.{i}",
            path=f"/.probe-{i}",
            detail="Blocked static path probe",
        )

    assert len(monitor.get_recent_events()) == 50
    summary = monitor.get_summary()
    assert summary["dedupe_keys_tracked"] <= 3
    assert summary["dedupe_keys_max"] == 3


def test_login_burst_alert_fires_once_per_ip_per_window(monkeypatch):
    fired: list[tuple[object, str]] = []
    monkeypatch.setattr(
        alerts, "alert_nowait",
        lambda event, message, **kwargs: fired.append((event, message)),
    )

    now = [3_000.0]
    monitor = SecurityMonitor(clock=lambda: now[0])
    for _ in range(20):
        monitor.record_event(
            SecurityEventType.AUTH_FAILURE,
            client_ip="192.0.2.55",
            path="/api/auth/login",
            detail="Operator login failed: invalid credentials",
        )

    assert len(fired) == 1
    assert fired[0][0] == alerts.AlertEvent.LOGIN_FAILURE_BURST
    assert "192.0.2.55" in fired[0][1]

    # A different address is a different incident.
    for _ in range(5):
        monitor.record_event(
            SecurityEventType.AUTH_FAILURE,
            client_ip="192.0.2.56",
            path="/api/auth/login",
            detail="Operator login failed: invalid credentials",
        )
    assert len(fired) == 2

    # clear() must also reset burst suppression state, or a test/rotation
    # could silently lose the next real burst alert.
    monitor.clear()
    for _ in range(5):
        monitor.record_event(
            SecurityEventType.AUTH_FAILURE,
            client_ip="192.0.2.55",
            path="/api/auth/login",
            detail="Operator login failed: invalid credentials",
        )
    assert len(fired) == 3


def test_every_security_event_type_has_a_production_emitter():
    """A new enum member without an emitter is a silent monitoring gap."""
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "backend").rglob("*.py")
        if "tests" not in path.parts
        and path.name != "security_monitor.py"
    )
    missing = [
        member.name for member in SecurityEventType
        if f"SecurityEventType.{member.name}" not in source
    ]
    assert missing == [], f"security event types with no emitter: {missing}"


# ------------------------------------------------------------ real HTTP triggers

@pytest.mark.asyncio
async def test_schema_rejected_upload_records_suspicious_upload(client, monkeypatch):
    monkeypatch.setattr(
        config, "ADMIN_TOKEN", "s9-test-admin-token-0123456789abcdef")
    security_monitor.clear()

    response = await client.post(
        "/api/knowledge-base/ingest",
        json={"documents": [{"filename": "payload.exe",
                             "content": "not a document"}]},
        headers={"X-Admin-Token": config.ADMIN_TOKEN},
    )
    assert response.status_code == 422

    events = [
        e for e in security_monitor.get_recent_events()
        if e["event_type"] == SecurityEventType.SUSPICIOUS_UPLOAD.value
    ]
    assert len(events) == 1
    assert "no file was stored" in events[0]["detail"]
    assert "payload.exe" not in events[0]["detail"]


@pytest.mark.asyncio
async def test_csrf_block_records_an_event(client, monkeypatch):
    monkeypatch.setattr(config, "SESSION_COOKIE_NAME", "s9_session")
    security_monitor.clear()
    client.cookies.set(config.SESSION_COOKIE_NAME, "fake-cookie-value")

    response = await client.post(
        "/api/admin/reset?confirm=yes&mode=prune_only",
    )
    assert response.status_code == 403

    events = [
        e for e in security_monitor.get_recent_events()
        if e["event_type"] == SecurityEventType.CSRF_BLOCKED.value
    ]
    assert len(events) == 1
    assert "X-Requested-With" in events[0]["detail"]


@pytest.mark.asyncio
async def test_rate_limit_records_an_event(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_GENERAL_PER_MINUTE", 1)
    rate_limiter.limiter.clear()
    security_monitor.clear()

    first = await client.get("/api/feed")
    second = await client.get("/api/feed")
    assert first.status_code == 200
    assert second.status_code == 429

    events = [
        e for e in security_monitor.get_recent_events()
        if e["event_type"] == SecurityEventType.RATE_LIMIT_EXCEEDED.value
    ]
    assert len(events) == 1
    assert "general" in events[0]["detail"]


@pytest.mark.asyncio
async def test_unhandled_500_records_an_internal_error(client, monkeypatch):
    monkeypatch.setattr(config, "DEBUG", False)
    security_monitor.clear()

    async def boom():
        raise RuntimeError("s9 synthetic failure")

    route = APIRoute("/api/__s9_boom", boom, methods=["GET"])
    app.router.routes.insert(0, route)
    try:
        response = await client.get("/api/__s9_boom")
    finally:
        app.router.routes.remove(route)

    assert response.status_code == 500
    events = [
        e for e in security_monitor.get_recent_events()
        if e["event_type"] == SecurityEventType.INTERNAL_ERROR.value
    ]
    assert len(events) == 1
    assert "s9 synthetic failure" not in json.dumps(events[0])


# --------------------------------------------------------------- restore drill

def _write_state(state_dir: Path) -> None:
    state_dir.mkdir()
    (state_dir / "executions.json").write_text(
        json.dumps({"records": [{"kind": "buy", "mint": "S9MINT", "usd_size": 12.5, "status": "confirmed", "idempotency_key": "dummy-buy", "ts": 1.0}]}),
        encoding="utf-8")
    (state_dir / "commits.json").write_text(
        json.dumps([{"hash": "s9-hash", "nonce": "dummy-nonce", "kind": "buy", "status": "sealed", "payload": {"mint": "S9MINT"}, "sealed_at": 1.0}]), encoding="utf-8")
    (state_dir / "kill_switch.json").write_text(
        json.dumps({"tripped": False, "reason": "", "ts": 0}), encoding="utf-8")
    (state_dir / "wallet_keypair.json").write_text(
        json.dumps(list(range(64))), encoding="utf-8")


def _write_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, mint TEXT);")
        conn.execute("INSERT INTO trades (id, mint) VALUES (7, 'S9MINT');")
        conn.commit()
    finally:
        conn.close()


def test_backup_restore_drill_verifies_book_and_trips_kill_switch(tmp_path):
    backup_key = Fernet.generate_key().decode("utf-8")
    secret_key = Fernet.generate_key().decode("utf-8")

    state_dir = tmp_path / "live-state"
    db_path = tmp_path / "trading_bot.db"
    _write_state(state_dir)
    _write_db(db_path)

    archive = backup_state.create_backup(
        state_dir=state_dir,
        db_path=db_path,
        backup_dir=tmp_path / "backups",
        backup_key=backup_key,
        secret_store_key=secret_key,
    )

    target_state = tmp_path / "restore-drill" / "state"
    target_db = tmp_path / "restore-drill" / "trading_bot.db"
    result = restore_state.restore_backup(
        archive, target_state, target_db, backup_key)

    assert json.loads((target_state / "executions.json").read_text()) == {"records": [{"kind": "buy", "mint": "S9MINT", "usd_size": 12.5, "status": "confirmed", "idempotency_key": "dummy-buy", "ts": 1.0}]}
    assert json.loads((target_state / "commits.json").read_text()) == [{"hash": "s9-hash", "nonce": "dummy-nonce", "kind": "buy", "status": "sealed", "payload": {"mint": "S9MINT"}, "sealed_at": 1.0}]
    assert not (target_state / "wallet_keypair.json").exists()

    conn = sqlite3.connect(str(target_db))
    try:
        assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
        assert conn.execute("SELECT id, mint FROM trades").fetchall() == [
            (7, "S9MINT")]
    finally:
        conn.close()

    kill_switch = json.loads(
        (target_state / "kill_switch.json").read_text(encoding="utf-8"))
    assert kill_switch["tripped"] is True
    assert archive.name in kill_switch["reason"]
    assert "run reconcile and clear manually" in kill_switch["reason"]

    assert result["manifest"]["files"]
    assert result["pre_restore_backup"] is None
