"""
backend/tests/test_logging_and_monitoring.py — Logging and Security Monitoring Test Suite.

Verifies:
1. Log destinations & rotation setup:
   - Log directory is created and log files (app.log, error.log, security.log) exist on disk.
2. Complete secret scrubbing across all log outputs:
   - Configured admin tokens, API keys, passwords, bearer tokens, basic auth URLs,
     and Solana wallet keypairs are NEVER written to app.log, error.log, or security.log.
3. Security monitor event tracking:
   - Structured events (auth_failure, rate_limit_exceeded, bot_detected, blocked_path_probe, internal_error).
   - In-memory ring buffer and aggregated summary statistics (event counts by type, top suspicious IPs).
4. Operator monitoring API endpoints:
   - GET /api/admin/monitoring/security-events requires admin token, returns summary & event records.
   - GET /api/admin/monitoring/logs returns log file paths, disk sizes, and statuses.
5. End-to-end security event triggers:
   - Failed login records AUTH_FAILURE.
   - Honeypot tripping records BOT_DETECTED.
   - Probing dotfiles/sourcemaps records BLOCKED_PATH_PROBE.
"""
from __future__ import annotations

import logging
from pathlib import Path
import httpx
import pytest
import pytest_asyncio

import config
from api import db
from api.logging_config import get_log_paths, setup_logging
from api.main import app
from api.security_monitor import (
    SecurityEventType,
    record_security_event,
    security_monitor,
)


@pytest_asyncio.fixture(autouse=True)
def isolated_logging_env(tmp_path, monkeypatch):
    """Hermetic logging test environment directing log files to tmp_path."""
    original_log_dir = getattr(config, "LOG_DIR", Path(__file__).parent.parent.parent / "logs")
    log_dir = tmp_path / "test_logs"
    monkeypatch.setattr(config, "LOG_DIR", log_dir)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "super-secret-admin-token-789012")
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "sk-deepseek-test-key-345678")
    monkeypatch.setattr(config, "DEBUG", False)
    config.DB_PATH = tmp_path / "test_monitor.db"
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")

    # Clear monitor ring buffer before each test
    security_monitor.clear()

    # Initialize log files in tmp_path
    setup_logging(force=True)
    yield log_dir

    # Restore default logging on teardown
    monkeypatch.setattr(config, "LOG_DIR", original_log_dir)
    setup_logging(force=True)


@pytest_asyncio.fixture
async def client():
    await db.init_db()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Log Destinations & File Setup
# ---------------------------------------------------------------------------

def test_log_paths_and_directory_creation(isolated_logging_env):
    """Verify configured log paths and that log directory is created."""
    paths = get_log_paths()
    assert paths["log_dir"].exists()
    assert paths["app_log"].parent == paths["log_dir"]
    assert paths["error_log"].name == "error.log"
    assert paths["security_log"].name == "security.log"


def test_standard_and_error_logging_separation(isolated_logging_env):
    """Verify INFO messages go to app.log and ERROR messages go to error.log."""
    paths = get_log_paths()
    test_logger = logging.getLogger("test.module")

    test_logger.info("This is an informational test message 12345")
    test_logger.error("This is an error test message 67890")

    # Flush all handlers
    for h in logging.getLogger().handlers:
        h.flush()

    assert paths["app_log"].exists()
    app_log_content = paths["app_log"].read_text(encoding="utf-8")
    assert "informational test message 12345" in app_log_content
    assert "error test message 67890" in app_log_content

    assert paths["error_log"].exists()
    error_log_content = paths["error_log"].read_text(encoding="utf-8")
    assert "error test message 67890" in error_log_content
    # Info message must NOT be in error.log
    assert "informational test message 12345" not in error_log_content


# ---------------------------------------------------------------------------
# 2. Secret Redaction across All Log Files
# ---------------------------------------------------------------------------

def test_logs_scrub_admin_token_and_api_keys(isolated_logging_env):
    """Verify admin token and configured API keys are scrubbed before hitting disk."""
    paths = get_log_paths()
    test_logger = logging.getLogger("test.secret")

    test_logger.error(f"Login failed for token {config.ADMIN_TOKEN}")
    test_logger.info(f"API call with key {config.DEEPSEEK_API_KEY}")

    for h in logging.getLogger().handlers:
        h.flush()

    app_log_content = paths["app_log"].read_text(encoding="utf-8")
    error_log_content = paths["error_log"].read_text(encoding="utf-8")

    assert config.ADMIN_TOKEN not in app_log_content
    assert config.ADMIN_TOKEN not in error_log_content
    assert config.DEEPSEEK_API_KEY not in app_log_content
    assert "<REDACTED-SECRET>" in app_log_content


def test_logs_scrub_bearer_passwords_and_urls(isolated_logging_env):
    """Verify bearer tokens, basic auth URLs, and passwords are scrubbed in log files."""
    paths = get_log_paths()
    test_logger = logging.getLogger("test.network")

    test_logger.error(
        "Request failed to https://admin:SuperSecretPass123!@api.solana.com "
        "with Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 and password=RealPassword987"
    )

    for h in logging.getLogger().handlers:
        h.flush()

    error_log_content = paths["error_log"].read_text(encoding="utf-8")
    assert "SuperSecretPass123!" not in error_log_content
    assert "***:***@" in error_log_content
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in error_log_content
    assert "Bearer <REDACTED>" in error_log_content
    assert "RealPassword987" not in error_log_content
    assert "password=<REDACTED>" in error_log_content


def test_security_log_scrubs_solana_keypairs(isolated_logging_env):
    """Verify 64-int Solana wallet keypairs are redacted in security.log."""
    paths = get_log_paths()
    keypair_array = str(list(range(64)))

    record_security_event(
        event_type=SecurityEventType.AUTH_FAILURE,
        client_ip="10.0.0.1",
        detail=f"Keypair attempt with payload {keypair_array}",
    )

    for h in logging.getLogger("security").handlers:
        h.flush()

    security_log_content = paths["security_log"].read_text(encoding="utf-8")
    assert "[<REDACTED-KEYPAIR>]" in security_log_content
    assert keypair_array not in security_log_content


# ---------------------------------------------------------------------------
# 3. Security Monitor Event Tracking & Metrics
# ---------------------------------------------------------------------------

def test_security_monitor_records_and_aggregates():
    """Verify SecurityMonitor stores events and computes accurate aggregate summaries."""
    record_security_event(
        event_type=SecurityEventType.AUTH_FAILURE,
        client_ip="192.168.1.100",
        path="/api/auth/login",
        method="POST",
        detail="Bad credentials",
    )
    record_security_event(
        event_type=SecurityEventType.RATE_LIMIT_EXCEEDED,
        client_ip="192.168.1.100",
        path="/api/auth/login",
        method="POST",
        detail="Rate limited",
    )
    record_security_event(
        event_type=SecurityEventType.BOT_DETECTED,
        client_ip="10.20.30.40",
        path="/api/auth/signup",
        method="POST",
        detail="Honeypot filled",
    )

    summary = security_monitor.get_summary()
    assert summary["monitoring_active"] is True
    assert summary["total_events_recorded"] == 3
    assert summary["event_counts_by_type"]["auth_failure"] == 1
    assert summary["event_counts_by_type"]["rate_limit_exceeded"] == 1
    assert summary["event_counts_by_type"]["bot_detected"] == 1
    assert summary["unique_flagged_ips"] == 2

    # Verify top attacker IP breakdown
    assert summary["top_flagged_ips"][0]["ip"] == "192.168.1.100"
    assert summary["top_flagged_ips"][0]["count"] == 2

    # Verify recent events retrieval
    recent = security_monitor.get_recent_events(limit=10)
    assert len(recent) == 3
    # Newest first
    assert recent[0]["event_type"] == "bot_detected"

    # Verify event_type filtering
    auth_only = security_monitor.get_recent_events(event_type="auth_failure")
    assert len(auth_only) == 1
    assert auth_only[0]["client_ip"] == "192.168.1.100"


# ---------------------------------------------------------------------------
# 4. Operator Monitoring API Endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_monitoring_api_requires_authentication(client):
    """GET /api/admin/monitoring/security-events rejects unauthenticated requests with 403."""
    r = await client.get("/api/admin/monitoring/security-events")
    assert r.status_code == 403

    r_logs = await client.get("/api/admin/monitoring/logs")
    assert r_logs.status_code == 403


@pytest.mark.asyncio
async def test_monitoring_api_returns_events_and_log_statuses(client):
    """GET /api/admin/monitoring/security-events and /logs return 200 with valid data."""
    # Seed a test security event
    record_security_event(
        event_type=SecurityEventType.BLOCKED_PATH_PROBE,
        client_ip="1.2.3.4",
        path="/.env",
        detail="Probe for .env file",
    )

    headers = {"X-Admin-Token": config.ADMIN_TOKEN}

    # 1. Check security events endpoint
    r = await client.get("/api/admin/monitoring/security-events", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "summary" in data
    assert data["summary"]["total_events_recorded"] >= 1
    assert len(data["events"]) >= 1
    assert data["events"][0]["event_type"] == "blocked_path_probe"

    # 2. Check logs status endpoint
    r_logs = await client.get("/api/admin/monitoring/logs", headers=headers)
    assert r_logs.status_code == 200
    logs_data = r_logs.json()
    assert logs_data["status"] == "ok"
    assert "log_files" in logs_data
    assert "app_log" in logs_data["log_files"]
    assert "error_log" in logs_data["log_files"]
    assert "security_log" in logs_data["log_files"]


# ---------------------------------------------------------------------------
# 5. End-to-End Suspicious Activity Triggers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_login_triggers_auth_failure_security_event(client):
    """A failed login attempt automatically records an AUTH_FAILURE security event."""
    security_monitor.clear()
    r = await client.post(
        "/api/auth/login",
        json={"token": "wrong-token-guess-12345", "captcha_token": "test-captcha-pass"},
    )
    assert r.status_code == 403

    events = security_monitor.get_recent_events()
    assert len(events) >= 1
    auth_events = [e for e in events if e["event_type"] == "auth_failure"]
    assert len(auth_events) == 1
    assert "wrong-token-guess-12345" not in auth_events[0]["detail"]


@pytest.mark.asyncio
async def test_honeypot_triggers_bot_detected_security_event(client):
    """Populating honeypot field automatically records a BOT_DETECTED security event."""
    security_monitor.clear()
    r = await client.post(
        "/api/auth/login",
        json={"token": "any-token", "honeypot": "automated_spambot_field"},
    )
    assert r.status_code == 400

    events = security_monitor.get_recent_events()
    bot_events = [e for e in events if e["event_type"] == "bot_detected"]
    assert len(bot_events) == 1
    assert "honeypot" in bot_events[0]["detail"].lower()


@pytest.mark.asyncio
async def test_blocked_path_probe_triggers_security_event(client):
    """Probing for dotfiles or sourcemaps records a BLOCKED_PATH_PROBE security event."""
    security_monitor.clear()
    r = await client.get("/.git/HEAD")
    assert r.status_code == 404

    events = security_monitor.get_recent_events()
    probe_events = [e for e in events if e["event_type"] == "blocked_path_probe"]
    assert len(probe_events) == 1
    assert ".git" in probe_events[0]["path"]
