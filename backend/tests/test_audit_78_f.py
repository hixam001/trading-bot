"""
backend/tests/test_audit_78_f.py — Minor hardening leftovers tests (§78 F1–F6).
"""
import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# F1: Docker Compose restart policy and clean no-wallet exit
# ---------------------------------------------------------------------------

def test_docker_compose_live_restart_policy():
    """F1: docker-compose.yml 'live' service must have restart: on-failure."""
    compose_path = REPO_ROOT / "docker-compose.yml"
    assert compose_path.exists()
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    live = data.get("services", {}).get("live", {})
    assert live.get("restart") == "on-failure", "live service restart policy must be 'on-failure'"


# ---------------------------------------------------------------------------
# F2: Secret redaction coverage (single-quoted dicts and keypair forms)
# ---------------------------------------------------------------------------

def test_secret_redaction_single_quoted_dicts():
    """F2: SecretRedactorFilter must mask single-quoted dict reprs in log messages."""
    from api.error_handler import SecretRedactorFilter
    filter_obj = SecretRedactorFilter()

    # Create dummy LogRecord
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="Request failed with headers: {'Authorization': 'secret-token-xyz-12345', 'api_key': 'my-secret-key-999'}",
        args=(), exc_info=None,
    )
    filter_obj.filter(record)
    assert "secret-token-xyz-12345" not in record.msg
    assert "my-secret-key-999" not in record.msg
    assert "<REDACTED" in record.msg


def test_keypair_representations_registered_and_redacted():
    """F2: Loading keypair must register raw JSON, base58 string, and hex in secret registry."""
    from live_execution.wallet import load_keypair
    from secret_registry import is_secret_registered
    from solders.keypair import Keypair

    # Generate a real valid keypair for testing
    real_kp = Keypair()
    dummy_key = list(bytes(real_kp))
    dummy_json = json.dumps(dummy_key)

    with patch("live_execution.config.WALLET_KEYPAIR_JSON", dummy_json):
        with patch("live_execution.config.WALLET_KEYPAIR_PATH", ""):
            kp = load_keypair()

    # Raw JSON string must be registered
    assert is_secret_registered(dummy_json)
    # Base58 public/private representation must be registered
    assert is_secret_registered(str(kp))
    # Hex representation must be registered
    assert is_secret_registered(bytes(dummy_key).hex())


# ---------------------------------------------------------------------------
# F3: Portfolio cash null-safety
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_wallet_cash_usd_returns_none_when_unreadable():
    """F3: get_wallet_cash_usd helper must return None (never 0.0) when unknown."""
    from live_execution.wallet import get_wallet_cash_usd

    with patch("live_execution.wallet.public_address", return_value=None):
        cash = await get_wallet_cash_usd()
        assert cash is None, "When wallet is unknown, cash must be None, never 0.0"

    with patch("live_execution.wallet.public_address", return_value="11111111111111111111111111111111"):
        with patch("live_execution.solana.get_usdc_balance", AsyncMock(return_value=None)):
            cash = await get_wallet_cash_usd()
            assert cash is None, "When balance RPC is unreachable, cash must be None, never 0.0"


# ---------------------------------------------------------------------------
# F4: get_client_ip trust model and rightmost untrusted XFF
# ---------------------------------------------------------------------------

def test_get_client_ip_trusted_proxy_cidrs():
    """F4: get_client_ip must honor XFF only for trusted socket peers and take rightmost untrusted IP."""
    from unittest.mock import MagicMock
    from api.rate_limiter import get_client_ip

    # Case 1: Direct untrusted client (e.g. 203.0.113.10)
    req1 = MagicMock()
    req1.client.host = "203.0.113.10"
    req1.headers = {"x-forwarded-for": "198.51.100.5"}
    assert get_client_ip(req1) == "203.0.113.10", "Direct untrusted connection must ignore XFF"

    # Case 2: Connection from trusted Docker bridge (172.18.0.5) with XFF
    req2 = MagicMock()
    req2.client.host = "172.18.0.5"
    req2.headers = {"x-forwarded-for": "198.51.100.5"}
    assert get_client_ip(req2) == "198.51.100.5", "Trusted proxy must parse client IP from XFF"

    # Case 3: Chained / spoofed XFF behind trusted proxy: [spoofed_ip, real_client_ip]
    req3 = MagicMock()
    req3.client.host = "127.0.0.1"
    req3.headers = {"x-forwarded-for": "1.1.1.1, 203.0.113.50"}
    # Standard proxy defense: rightmost untrusted IP is the real client IP (not spoofed 1.1.1.1)
    assert get_client_ip(req3) == "203.0.113.50"


# ---------------------------------------------------------------------------
# F5: ADMIN_TOKEN entropy check
# ---------------------------------------------------------------------------

def test_admin_token_entropy_verification():
    """F5: ADMIN_TOKEN must require >= 32 chars and >= 12 distinct characters."""
    from config_helpers import validate_admin_token_entropy

    # Low entropy tokens must fail
    assert validate_admin_token_entropy("a" * 32) is False
    assert validate_admin_token_entropy("1234" * 8) is False
    assert validate_admin_token_entropy("k" * 40) is False
    assert validate_admin_token_entropy("short") is False

    # Valid tokens must pass
    assert validate_admin_token_entropy("test-operator-secret-at-least-32-chars-long") is True
    import secrets
    assert validate_admin_token_entropy(secrets.token_hex(32)) is True


# ---------------------------------------------------------------------------
# F6: Pyflakes clean check on touched files
# ---------------------------------------------------------------------------

def test_pyflakes_clean_run_live_cycle_and_db_pg():
    """F6: pyflakes must report 0 undefined names or linter errors on touched files."""
    cmd = [
        str(REPO_ROOT / ".venv" / "bin" / "pyflakes"),
        str(REPO_ROOT / "backend" / "run_live_cycle.py"),
        str(REPO_ROOT / "backend" / "api" / "db_pg.py"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, f"Pyflakes reported warnings:\n{proc.stdout}\n{proc.stderr}"
