"""
backend/tests/test_s2_auth_hardening.py — S2 authentication/authorization proofs.

Covers the operator-surface hardening added in §92 S2:
  * CSRF for cookie-authenticated mutations (custom header + Origin/Referer)
  * persistent logout revocation across restart (signature-only file)
  * global distributed brute-force ceiling (per-IP rotation cannot bypass)
  * single-use proof-of-work challenges (no replay window)
  * signup concealed by default
  * ADMIN_TOKEN / SESSION_SECRET fail-closed at boot
  * dedicated SESSION_SECRET separate from the operator token
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import httpx
import pytest
import pytest_asyncio

import config
from api import auth, bot_protection, db
from api.auth import (
    create_session_token,
    is_session_revoked,
    revoke_session_token,
    verify_session_token,
)
from api.main import app

TOKEN = "test-operator-session-token-32-chars-long"
SESSION_ONLY = "test-dedicated-session-secret-32-chars"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest_asyncio.fixture
async def s2_env(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "s2.db"
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(config, "SESSION_SECRET", "")
    monkeypatch.setattr(config, "SESSION_MAX_AGE_SECONDS", 3600)
    monkeypatch.setattr(config, "ALLOW_SIGNUP", False)
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", False)
    monkeypatch.setattr(config, "LIVE_BOOK_PUBLIC", False)
    await db.init_db()
    yield


@pytest_asyncio.fixture
async def client(s2_env):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        yield c


async def _login(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/auth/login", json={"token": TOKEN})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cookie_mutation_without_custom_header_is_rejected(client):
    await _login(client)
    r = await client.post("/api/auth/logout")
    assert r.status_code == 403
    assert "csrf" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_cookie_mutation_with_header_succeeds(client):
    await _login(client)
    r = await client.post(
        "/api/auth/logout", headers={"X-Requested-With": "XMLHttpRequest"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "logged_out"


@pytest.mark.asyncio
async def test_cookie_mutation_rejects_cross_site_origin_even_with_header(client):
    await _login(client)
    r = await client.post(
        "/api/auth/logout",
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://evil.example",
        },
    )
    assert r.status_code == 403
    assert "origin" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_cookie_mutation_accepts_same_origin_with_header(client):
    await _login(client)
    r = await client.post(
        "/api/auth/logout",
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "http://127.0.0.1",
        },
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_cookie_mutation_rejects_cross_site_referer(client):
    await _login(client)
    r = await client.post(
        "/api/auth/logout",
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://evil.example/steal",
        },
    )
    assert r.status_code == 403
    assert "referer" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_header_authenticated_mutation_has_no_cookie_csrf_requirement(client):
    r = await client.post(
        "/api/admin/reset",
        params={"confirm": "yes", "mode": "prune_only"},
        headers={"X-Admin-Token": TOKEN},
    )
    assert r.status_code == 200


def test_csrf_block_is_a_security_event():
    from api.security_monitor import SecurityEventType

    assert SecurityEventType.CSRF_BLOCKED.value == "csrf_blocked"


# ---------------------------------------------------------------------------
# Persistent revocation
# ---------------------------------------------------------------------------

def test_logout_revocation_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / "revocations.json"
    monkeypatch.setattr(config, "SESSION_REVOCATION_FILE", str(path))
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)

    token = create_session_token()
    revoke_session_token(token)
    assert is_session_revoked(token) is True
    assert path.is_file()

    # Simulate a fresh process: memory gone, on-disk registry still there.
    auth._REVOKED_SESSION_SIGS.clear()
    auth._REVOCATIONS_LOADED = False
    auth._REVOCATIONS_ERROR = False

    assert is_session_revoked(token) is True
    assert verify_session_token(token) is False


def test_revocation_file_stores_only_signature_and_expiry(tmp_path, monkeypatch):
    path = tmp_path / "revocations.json"
    monkeypatch.setattr(config, "SESSION_REVOCATION_FILE", str(path))
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)

    token = create_session_token()
    revoke_session_token(token)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert list(payload["revoked"]) == [token.split(".")[2]]
    raw = path.read_text(encoding="utf-8")
    assert token not in raw
    assert token.split(".")[1] not in raw  # nonce never persisted
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_corrupt_revocation_file_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "revocations.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(config, "SESSION_REVOCATION_FILE", str(path))
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    auth._REVOCATIONS_LOADED = False
    auth._REVOCATIONS_ERROR = False

    token = create_session_token()
    assert is_session_revoked(token) is True
    assert verify_session_token(token) is False


# ---------------------------------------------------------------------------
# Global brute-force ceiling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_global_lockout_stops_distributed_ip_rotation(client):
    statuses = []
    for i in range(50):
        r = await client.post(
            "/api/auth/login",
            json={"token": "wrong-credential"},
            headers={"X-Forwarded-For": f"203.0.113.{i + 1}"},
        )
        statuses.append(r.status_code)
    assert set(statuses[:5]) == {403}

    # 51st attempt from a brand-new IP: per-IP budget is untouched, but the
    # global window is full.
    r_locked = await client.post(
        "/api/auth/login",
        json={"token": "wrong-credential"},
        headers={"X-Forwarded-For": "198.51.100.250"},
    )
    assert r_locked.status_code == 429
    assert "rate limited" in r_locked.json()["detail"].lower()


@pytest.mark.asyncio
async def test_global_lockout_never_blocks_correct_operator_token(client):
    for i in range(50):
        await client.post(
            "/api/auth/login",
            json={"token": "wrong-credential"},
            headers={"X-Forwarded-For": f"203.0.113.{i + 1}"},
        )
    # Wrong credential from a new IP is throttled...
    r_bad = await client.post(
        "/api/auth/login",
        json={"token": "wrong-credential"},
        headers={"X-Forwarded-For": "198.51.100.251"},
    )
    assert r_bad.status_code == 429

    # ...but the real operator still authenticates and clears the window.
    r_ok = await client.post("/api/auth/login", json={"token": TOKEN})
    assert r_ok.status_code == 200
    assert len(auth._GLOBAL_FAILED_ATTEMPTS) == 0


# ---------------------------------------------------------------------------
# Single-use PoW
# ---------------------------------------------------------------------------

def _solve(challenge: dict) -> str:
    prefix = "0" * int(challenge["difficulty"])
    for i in range(500_000):
        candidate = f"sol_{i}"
        if hashlib.sha256(
            f"{challenge['nonce']}:{candidate}".encode()
        ).hexdigest().startswith(prefix):
            return candidate
    raise AssertionError("could not solve test challenge")


@pytest.mark.asyncio
async def test_proof_of_work_challenge_is_single_use(client, monkeypatch):
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", True)
    monkeypatch.setattr(config, "CAPTCHA_PROVIDER", "internal")

    r_challenge = await client.get("/api/auth/challenge")
    assert r_challenge.status_code == 200
    challenge = r_challenge.json()
    solution = _solve(challenge)
    token = (
        f"{challenge['nonce']}.{challenge['timestamp']}."
        f"{challenge['signature']}.{solution}"
    )

    first = await client.post(
        "/api/auth/login", json={"token": TOKEN, "captcha_token": token}
    )
    assert first.status_code == 200

    replay = await client.post(
        "/api/auth/login", json={"token": TOKEN, "captcha_token": token}
    )
    assert replay.status_code == 400
    assert "bot challenge" in replay.json()["detail"].lower()


def test_wrong_pow_solution_does_not_burn_challenge():
    challenge = bot_protection.create_bot_challenge(difficulty=3)
    assert bot_protection.verify_proof_of_work(
        challenge["nonce"],
        challenge["timestamp"],
        challenge["signature"],
        "definitely-wrong",
    ) is False
    solution = _solve(challenge)
    assert bot_protection.verify_proof_of_work(
        challenge["nonce"],
        challenge["timestamp"],
        challenge["signature"],
        solution,
    ) is True


# ---------------------------------------------------------------------------
# Signup concealment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signup_is_404_by_default(client):
    r = await client.post(
        "/api/auth/signup",
        json={"username": "valid_user", "password": "ValidPassword123!"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_signup_opt_in_still_refuses_account_creation(client, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_SIGNUP", True)
    r = await client.post(
        "/api/auth/signup",
        json={"username": "valid_user", "password": "ValidPassword123!"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Boot-time secret validation
# ---------------------------------------------------------------------------

def _import_config(env_overrides: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("ADMIN_TOKEN", None)
    env.pop("SESSION_SECRET", None)
    env.update(env_overrides)
    env["PYTHONPATH"] = str(REPO_ROOT / "backend")
    return subprocess.run(
        [sys.executable, "-c", "import config; config.validate_api_credentials()"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_invalid_admin_token_refuses_boot(tmp_path):
    proc = _import_config({"ADMIN_TOKEN": "a" * 40}, tmp_path)
    assert proc.returncode != 0
    assert "ADMIN_TOKEN" in proc.stderr


def test_weak_session_secret_refuses_boot(tmp_path):
    proc = _import_config(
        {
            "ADMIN_TOKEN": "valid-admin-token-" + "a1b2c3d4" * 4,
            "SESSION_SECRET": "short",
        },
        tmp_path,
    )
    assert proc.returncode != 0
    assert "SESSION_SECRET" in proc.stderr


def test_session_secret_equal_to_admin_token_refuses_boot(tmp_path):
    same = "same-token-" + "a1b2c3d4" * 4
    proc = _import_config(
        {"ADMIN_TOKEN": same, "SESSION_SECRET": same},
        tmp_path,
    )
    assert proc.returncode != 0
    assert "SESSION_SECRET" in proc.stderr


def test_valid_distinct_secrets_boot(tmp_path):
    proc = _import_config(
        {
            "ADMIN_TOKEN": "valid-admin-token-" + "a1b2c3d4" * 4,
            "SESSION_SECRET": "valid-session-secret-" + "e5f6a7b8" * 4,
        },
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# Dedicated session secret
# ---------------------------------------------------------------------------

def test_session_signing_uses_dedicated_secret_and_survives_admin_rotation(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(config, "SESSION_SECRET", SESSION_ONLY)

    token = create_session_token()
    assert verify_session_token(token) is True

    # Token is signed with SESSION_SECRET, not ADMIN_TOKEN.
    assert verify_session_token(token, secret=SESSION_ONLY) is True
    assert verify_session_token(token, secret=TOKEN) is False

    # Rotating the operator token does not invalidate live dashboard sessions.
    monkeypatch.setattr(config, "ADMIN_TOKEN", "rotated-operator-token-" + "9" * 24)
    assert verify_session_token(token) is True
