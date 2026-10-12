"""
backend/tests/test_audit_78_e.py — Session revocation memory growth tests (§78 E1–E4).
"""
import time
from unittest.mock import patch
from fastapi.testclient import TestClient
import pytest

import config
from api.main import app
from api.auth import (
    _REVOKED_SESSION_SIGS,
    MAX_REVOKED_SESSIONS,
    create_session_token,
    is_session_revoked,
    revoke_session_token,
    verify_session_token,
)

client = TestClient(app)

TEST_SECRET = "test-operator-secret-at-least-32-chars-long"


@pytest.fixture(autouse=True)
def clean_revocation_set():
    """Ensure clean revocation set before and after each test."""
    _REVOKED_SESSION_SIGS.clear()
    yield
    _REVOKED_SESSION_SIGS.clear()


def test_spam_forged_cookies_does_not_grow_revocation_set(monkeypatch):
    """E1: Spamming forged cookies to /logout must return 200 but add NOTHING to revocation registry."""
    monkeypatch.setattr(config, "ADMIN_TOKEN", TEST_SECRET)

    # Pre-condition: set is empty
    assert len(_REVOKED_SESSION_SIGS) == 0

    for i in range(100):
        forged_cookie = f"1700000000.nonce{i:04d}.forgedsignature{i:04d}"
        resp = client.post(
            "/api/auth/logout",
            cookies={config.SESSION_COOKIE_NAME: forged_cookie},
        )
        assert resp.status_code == 200, "Logout should return 200 without leaking token validity"

    # Post-condition: set MUST STILL BE EMPTY
    assert len(_REVOKED_SESSION_SIGS) == 0, (
        f"Memory growth vulnerability: {len(_REVOKED_SESSION_SIGS)} forged cookies stored in revocation registry!"
    )


def test_legitimate_logout_revokes_session(monkeypatch):
    """E1: Valid cookie sent to /logout increases revocation registry by 1, rejecting subsequent auth."""
    monkeypatch.setattr(config, "ADMIN_TOKEN", TEST_SECRET)

    token = create_session_token(secret=TEST_SECRET)
    assert verify_session_token(token, secret=TEST_SECRET) is True

    init_len = len(_REVOKED_SESSION_SIGS)
    resp = client.post(
        "/api/auth/logout",
        cookies={config.SESSION_COOKIE_NAME: token},
    )
    assert resp.status_code == 200
    assert len(_REVOKED_SESSION_SIGS) == init_len + 1
    assert is_session_revoked(token) is True
    assert verify_session_token(token, secret=TEST_SECRET) is False


def test_expired_cookie_does_not_grow_revocation_set(monkeypatch):
    """E1: An already-expired cookie sent to /logout or revoke_session_token adds nothing to registry."""
    monkeypatch.setattr(config, "ADMIN_TOKEN", TEST_SECRET)

    now = int(time.time())
    past = now - (config.SESSION_MAX_AGE_SECONDS + 100)
    import hmac, hashlib
    from api.auth import _derive_session_key
    payload = f"{past}.oldnonce"
    sig = hmac.new(
        _derive_session_key(TEST_SECRET),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    expired_token = f"{payload}.{sig}"

    init_len = len(_REVOKED_SESSION_SIGS)
    revoke_session_token(expired_token)
    assert len(_REVOKED_SESSION_SIGS) == init_len, "Expired token must not be added to revocation registry"


def test_revocation_set_cap(monkeypatch):
    """E2: Inserting more than MAX_REVOKED_SESSIONS bounds the registry to MAX_REVOKED_SESSIONS."""
    monkeypatch.setattr(config, "ADMIN_TOKEN", TEST_SECRET)

    # Use a small test cap or test against MAX_REVOKED_SESSIONS
    # To keep test fast, test cap logic by setting a mock cap or verifying MAX_REVOKED_SESSIONS
    with patch("api.auth.MAX_REVOKED_SESSIONS", 50):
        for i in range(75):
            token = create_session_token(secret=TEST_SECRET)
            revoke_session_token(token)
            assert len(_REVOKED_SESSION_SIGS) <= 50


def test_prune_expired_entries(monkeypatch):
    """E2: Expired entries in revocation registry are pruned on subsequent operations."""
    monkeypatch.setattr(config, "ADMIN_TOKEN", TEST_SECRET)

    now = time.time()
    # Insert entry that expires in the past
    _REVOKED_SESSION_SIGS["old_sig"] = now - 10

    # Add a valid entry
    token = create_session_token(secret=TEST_SECRET)
    revoke_session_token(token)

    assert "old_sig" not in _REVOKED_SESSION_SIGS, "Expired entry must be pruned"


def test_mutation_check_forged_tokens_rejected(monkeypatch):
    """Mutation check: verify that accepting unverified tokens causes attack detection failure."""
    monkeypatch.setattr(config, "ADMIN_TOKEN", TEST_SECRET)

    fake_token = "1700000000.nonce.invalidsig"
    revoke_session_token(fake_token)
    assert "invalidsig" not in _REVOKED_SESSION_SIGS

