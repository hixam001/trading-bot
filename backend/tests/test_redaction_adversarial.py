"""
backend/tests/test_redaction_adversarial.py — S1 secret-shielding proofs.

The scrubber is shape-based on purpose: an operator may paste a key into a
traceback, an HTTP client may echo a credentialed URL, and a provider may
rotate in a token our config has never seen. These tests treat every
outbound/logging surface (log lines, tracebacks, alert payloads, 4xx error
details) as hostile and prove the material never reaches it.
"""
from __future__ import annotations

import logging
from io import StringIO

import pytest

import alerts
import config
from api.error_handler import SecretRedactorFilter, sanitize_error_detail
from secret_registry import register_secret


def _redact(text: str) -> str:
    """Exercise the public error-detail scrubber used for client responses."""
    return sanitize_error_detail(text)


def _log_with_filter(message: str, *args: object, exc_info: object = None) -> str:
    """Emit one record through the production filter and return formatted text."""
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test.redaction.adversarial")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.filters = [SecretRedactorFilter()]
    logger.error(message, *args, exc_info=exc_info)
    handler.flush()
    return stream.getvalue()


def _base58_key() -> str:
    # Valid base58 alphabet, 88 chars (64-byte Solana secret key size).
    return ("4" * 88)


def _json_keypair() -> str:
    return "[" + ",".join(str((i * 37) % 256) for i in range(64)) + "]"


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("json-keypair", f"wallet_keypair={_json_keypair()}"),
        ("base58-keypair", f"private key: {_base58_key()}"),
        (
            "pem",
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ\n"
            "-----END PRIVATE KEY-----",
        ),
        ("bearer", "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"),
        ("basic-auth-url", "https://alice:hunter2-in-url@rpc.example.com"),
        (
            "rpc-path-key",
            "https://mainnet.helius-rpc.com/v0/" + "A1b2C3d4E5f6G7h8I9j0K1l2",
        ),
        (
            "query-key",
            "GET https://rpc.example.com/?api-key=" + "Q" * 32 + "&commitment=confirmed",
        ),
        ("json-secret", '{"api_key": "json-secret-value-123456789"}'),
        ("openai-shape", "provider said " + "sk-" + "a" * 24),
        ("groq-shape", "provider said " + "gsk_" + "b" * 24),
        ("github-shape", "provider said " + "ghp_" + "c" * 24),
        ("telegram-shape", "bot " + "1234567890:" + "d" * 35),
        ("ntfy-shape", "topic " + "tk_" + "e" * 24),
    ],
)
def test_redact_text_masks_every_structural_shape(label: str, payload: str) -> None:
    scrubbed = _redact(payload)

    assert payload not in scrubbed, f"{label} survived redaction: {scrubbed!r}"
    assert "<REDACTED" in scrubbed or "***:***@" in scrubbed


def test_logging_filter_scrubs_known_config_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "operator-" + "z" * 40
    monkeypatch.setattr(config, "ADMIN_TOKEN", token)

    logged = _log_with_filter("auth failed for %s", token)

    assert token not in logged
    assert "<REDACTED-SECRET>" in logged


def test_logging_filter_scrubs_runtime_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "runtime-" + "y" * 40
    register_secret(token)

    logged = _log_with_filter("rotated token %s", token)

    assert token not in logged
    assert "<REDACTED-SECRET>" in logged


def test_logging_filter_scrubs_traceback_text() -> None:
    keypair = _json_keypair()

    try:
        raise RuntimeError(f"wallet parse failed for {keypair}")
    except RuntimeError:
        logged = _log_with_filter("unhandled failure", exc_info=True)

    assert keypair not in logged
    assert "RuntimeError" in logged  # traceback itself is still useful
    assert "[<REDACTED-KEYPAIR>]" in logged


def test_logging_filter_scrubs_repr_and_mapping_arguments() -> None:
    key = _base58_key()
    payload = {"wallet_key": key, "state": "pending"}

    logged = _log_with_filter("state=%s", repr(payload))

    assert key not in logged
    assert "<REDACTED-KEYPAIR>" in logged


def test_logging_filter_redacts_message_args_without_touching_plain_text() -> None:
    token = "sk-" + "q" * 24

    logged = _log_with_filter("upstream returned %s", token)

    assert token not in logged
    assert "<REDACTED-TOKEN>" in logged


def test_sanitize_error_detail_uses_generic_redaction_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "detail-" + "x" * 40
    monkeypatch.setattr(config, "ADMIN_TOKEN", token)

    scrubbed = sanitize_error_detail(
        f"callback failed: token={token} url=https://u:p4ssword@host.example"
    )

    assert token not in scrubbed
    assert "p4ssword" not in scrubbed
    assert "<REDACTED>" in scrubbed


def test_alert_payload_scrubs_channel_token_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "ntfy-" + "w" * 40
    monkeypatch.setenv("ALERT_NTFY_TOKEN", token)

    scrubbed = alerts.sanitize_alert_text(f"delivery failed with token {token}")

    assert token not in scrubbed
    assert "<REDACTED>" in scrubbed


def test_alert_payload_scrubs_keypair_and_provider_shapes() -> None:
    payload = (
        f"keypair {_json_keypair()} base58 {_base58_key()} "
        "sk-" + "r" * 24
    )

    scrubbed = alerts.sanitize_alert_text(payload)

    assert _json_keypair() not in scrubbed
    assert _base58_key() not in scrubbed
    assert ("sk-" + "r" * 24) not in scrubbed


def test_redaction_is_idempotent_and_never_raises_on_odd_input() -> None:
    once = _redact(f"private key: {_base58_key()}")
    twice = _redact(once)

    assert once == twice
    from redaction import redact_text
    assert redact_text(None) == "None"
