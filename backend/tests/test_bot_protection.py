"""
tests/test_bot_protection.py — Server-side bot challenge and CAPTCHA tests.

Verifies Rule 12:
- Honeypot field traps automated spam bots before processing.
- Missing CAPTCHA token rejected when CAPTCHA is enabled.
- Server-side verification for Turnstile / hCaptcha.
- Built-in cryptographic proof-of-work challenge generation and verification.
"""
from __future__ import annotations

import hashlib
import httpx
import pytest
import pytest_asyncio

import config
from api import db
from api.bot_protection import create_bot_challenge
from api.main import app

TOKEN = "test-token-bot-protection"


@pytest_asyncio.fixture
async def app_env(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "bot_test.db"
    monkeypatch.setattr(config, "ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", True)
    monkeypatch.setattr(config, "CAPTCHA_PROVIDER", "internal")
    await db.init_db()
    yield


@pytest_asyncio.fixture
async def client(app_env):
    from data_providers.mock import MockProvider
    from llm.narrator import Narrator

    app.state.provider = MockProvider()
    app.state.narrator = Narrator()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_honeypot_field_blocks_automated_bots(client):
    """If a bot fills out the hidden honeypot field, the request is immediately rejected."""
    # Bot fills out honeypot on login
    r_login = await client.post(
        "/api/auth/login",
        json={"token": TOKEN, "_hp_website": "http://spambot.com"},
    )
    assert r_login.status_code == 400
    assert "honeypot" in r_login.json()["detail"].lower()

    # Bot fills out honeypot on signup
    r_signup = await client.post(
        "/api/auth/signup",
        json={
            "username": "spammer",
            "password": "Password123!",
            "_hp_website": "spambot",
        },
    )
    assert r_signup.status_code == 400
    assert "honeypot" in r_signup.json()["detail"].lower()


@pytest.mark.asyncio
async def test_missing_captcha_token_rejected_when_enabled(client):
    """When CAPTCHA is enabled, missing token is rejected before checking credentials."""
    r = await client.post(
        "/api/auth/login",
        json={"token": TOKEN},  # no captcha_token provided
    )
    assert r.status_code == 400
    assert "missing captcha" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_internal_proof_of_work_challenge_flow(client):
    """Test generating and solving a cryptographic proof-of-work bot challenge."""
    # 1. Client fetches challenge
    r_challenge = await client.get("/api/auth/challenge")
    assert r_challenge.status_code == 200
    data = r_challenge.json()
    nonce = data["nonce"]
    ts = data["timestamp"]
    diff = data["difficulty"]
    sig = data["signature"]

    # 2. Client solves challenge locally
    solution = None
    prefix = "0" * diff
    for i in range(100_000):
        candidate = f"sol_{i}"
        h = hashlib.sha256(f"{nonce}:{candidate}".encode()).hexdigest()
        if h.startswith(prefix):
            solution = candidate
            break
    assert solution is not None

    token = f"{nonce}.{ts}.{sig}.{solution}"

    # 3. Submit login with solved proof-of-work
    r = await client.post(
        "/api/auth/login",
        json={"token": TOKEN, "captcha_token": token},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "authenticated"


@pytest.mark.asyncio
async def test_invalid_proof_of_work_solution_rejected(client):
    """An incorrect proof-of-work solution is rejected by the server."""
    challenge = create_bot_challenge(difficulty=3)
    token = f"{challenge['nonce']}.{challenge['timestamp']}.{challenge['signature']}.invalid_guess"

    r = await client.post(
        "/api/auth/login",
        json={"token": TOKEN, "captcha_token": token},
    )
    assert r.status_code == 400
    assert "bot challenge failed" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_third_party_captcha_turnstile_verification(client, monkeypatch):
    """Test server-side verification using Cloudflare Turnstile / hCaptcha API."""
    monkeypatch.setattr(config, "CAPTCHA_PROVIDER", "turnstile")
    monkeypatch.setattr(config, "CAPTCHA_SECRET_KEY", "1x0000000000000000000000000000000AA")
    monkeypatch.setattr(config, "CAPTCHA_VERIFY_URL", "https://challenges.cloudflare.com/turnstile/v0/siteverify")

    from api import bot_protection

    async def mock_verify(token: str, client_ip: str | None = None) -> bool:
        return token == "valid_turnstile_token"

    monkeypatch.setattr(bot_protection, "_verify_external_captcha", mock_verify)

    # Valid token succeeds
    r_ok = await client.post(
        "/api/auth/login",
        json={"token": TOKEN, "captcha_token": "valid_turnstile_token"},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["status"] == "authenticated"

    # Invalid token fails
    r_bad = await client.post(
        "/api/auth/login",
        json={"token": TOKEN, "captcha_token": "bad_token"},
    )
    assert r_bad.status_code == 400
    assert "captcha" in r_bad.json()["detail"].lower()
