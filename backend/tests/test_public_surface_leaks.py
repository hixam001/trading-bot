"""
backend/tests/test_public_surface_leaks.py — S3: public surfaces leak nothing.

The public JSON feeds are intentionally unauthenticated, so their failure paths
must be as carefully written as their happy paths. A public endpoint that
formats an exception into its body hands an attacker filesystem paths, RPC
URLs, provider text, and sometimes the configured wallet.

These tests pin the contract:

* a corrupt kill-switch / break-state file reports a FIXED reason, never the
  exception (which embeds the state path);
* a binding mismatch never echoes the configured wallet or the transaction's
  fee payer;
* transaction/memo error objects are never stringified into a public detail;
* the live book's fail-soft reasons are fixed strings;
* every documented Public GET route is requested through the real app with
  distinctive sentinels configured as secrets, then scanned for sentinel
  values, keypair/PEM/Bearer shapes, tracebacks, and local paths.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

import config
from api import db
from api.main import app
from api.routes import live_book
from api.routes.disclosure import _break_state, _kill_switch_state
from api.routes.proof import _verify_binding_checks, _verify_memo

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOC = REPO_ROOT / "docs" / "10_SECURITY.md"

# A valid-length Base58-looking mint so the documented history route reaches
# its handler instead of failing path validation.
_PROBE_MINT = "1" * 32

_SENTINELS = {
    "ADMIN_TOKEN": "S3-ADMIN-SENTINEL-9f3c2a7d4e6b8c1f",
    "SESSION_SECRET": "S3-SESSION-SENTINEL-4d8e2b6f9a1c3e5d",
    "GROQ_API_KEY": "S3-GROQ-SENTINEL-7b1d5f9c3e8a2d6b",
    "DEEPSEEK_API_KEY": "S3-DEEPSEEK-SENTINEL-2e6a8c4f1b7d9e3a",
    "BIRDEYE_API_KEY": "S3-BIRDEYE-SENTINEL-8c4e2a6f9d1b5f7e",
    "JUPITER_API_KEY": "S3-JUPITER-SENTINEL-5a9f3c7e1d8b2a4f",
    "CAPTCHA_SECRET_KEY": "S3-CAPTCHA-SENTINEL-6d2b8e4a9f1c5e7b",
    "FIRECRAWL_API_KEY": "S3-FIRECRAWL-SENTINEL-1f7e3b9d5a2c8d4e",
}

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
_KEYPAIR_ARRAY_RE = re.compile(r"\[(?:\s*\d{1,3}\s*,){20,}\s*\d{1,3}\s*\]")
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")
_PRIVATE_KEY_FIELD_RE = re.compile(
    r'"(?:private[_-]?key|secret[_-]?key|keypair)"\s*:', re.IGNORECASE)


def _documented_public_gets() -> list[str]:
    """Every GET row marked Public in the security matrix."""
    paths: list[str] = []
    for line in DOC.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip().strip("`") for c in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        method, path, auth = cells[0].upper(), cells[1], cells[2].lower()
        if method == "GET" and auth.startswith("public") and path.startswith("/"):
            paths.append(path)
    return paths


def _materialize(route_path: str) -> str:
    """Replace FastAPI path parameters with harmless probe values."""
    return re.sub(r"\{[^}]+\}", _PROBE_MINT, route_path)


class _AlwaysHealthyLLM:
    async def health(self) -> bool:
        return True


class _ProbeNarrator:
    _main_llm = _AlwaysHealthyLLM()


@pytest_asyncio.fixture
async def public_client(tmp_path, monkeypatch):
    """Real app, isolated DB, sentinel secrets, and corrupt safety state."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "public-surface.db")
    monkeypatch.setattr(config, "LIVE_BOOK_PUBLIC", False)
    monkeypatch.setattr(config, "DEBUG", False)
    monkeypatch.setattr(config, "ALLOW_SIGNUP", False)
    for name, value in _SENTINELS.items():
        monkeypatch.setattr(config, name, value, raising=False)

    # Corrupt both fail-closed state files on purpose: at HEAD their
    # exception text (including the state path) was published verbatim.
    bad_kill = tmp_path / "kill_switch.json"
    bad_kill.write_text("{not valid json", encoding="utf-8")
    bad_break = tmp_path / "break_state.json"
    bad_break.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(bad_kill))
    monkeypatch.setattr(config, "BREAK_STATE_FILE", str(bad_break))

    monkeypatch.setattr(app.state, "narrator", _ProbeNarrator(), raising=False)
    monkeypatch.setattr(
        app.state, "provider", SimpleNamespace(), raising=False)
    from api.routes import system_status
    monkeypatch.setattr(
        system_status, "_health_cache", {"ok": True, "at": 1e18})

    await db.init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://test") as client:
        yield client, tmp_path


def _scan_public_body(path: str, status_code: int, body: str,
                      local_path: Path) -> list[str]:
    problems: list[str] = []
    if status_code >= 500:
        problems.append(f"{path}: returned {status_code}")
    for name, sentinel in _SENTINELS.items():
        if sentinel in body:
            problems.append(f"{path}: leaked configured {name}")
    for label, pattern in (
        ("PEM private key", _PRIVATE_KEY_RE),
        ("Bearer credential", _BEARER_RE),
        ("keypair byte array", _KEYPAIR_ARRAY_RE),
        ("private-key field", _PRIVATE_KEY_FIELD_RE),
        ("traceback", _TRACEBACK_RE),
    ):
        if pattern.search(body):
            problems.append(f"{path}: exposed {label}")
    for local in (str(REPO_ROOT), str(local_path)):
        if local in body:
            problems.append(f"{path}: exposed local path {local!r}")
    return problems


@pytest.mark.asyncio
async def test_every_public_get_route_leaks_nothing(public_client):
    client, tmp_path = public_client
    routes = _documented_public_gets()
    assert routes, "route matrix must document at least one Public GET route"

    problems: list[str] = []
    for route_path in routes:
        response = await client.get(_materialize(route_path))
        problems.extend(_scan_public_body(
            route_path, response.status_code, response.text, tmp_path))

    assert problems == [], "\n".join(problems)


@pytest.mark.asyncio
async def test_corrupt_public_safety_state_still_reports_fail_closed(public_client):
    """The corrupt state injected by the fixture must be reported honestly and
    without the exception text."""
    client, tmp_path = public_client
    response = await client.get("/api/disclosure.json")
    assert response.status_code == 200
    body = response.json()
    assert body["kill_switch"]["active"] is True
    assert body["break"]["on_break"] is True
    serialized = json.dumps(body)
    assert str(tmp_path) not in serialized
    assert "Expecting" not in serialized


# ---------------------------------------------------------------------------
# Kill switch / break state: fixed public reasons
# ---------------------------------------------------------------------------

def test_corrupt_kill_switch_reason_is_fixed(tmp_path, monkeypatch):
    path = tmp_path / "kill_switch.json"
    path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(path))

    state = _kill_switch_state()

    assert state == {"active": True, "reason": "unreadable — treated as KILL"}
    assert str(path) not in json.dumps(state)
    assert "Expecting" not in json.dumps(state)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (RuntimeError("break state at /srv/secret/path is corrupt"),
         "state corrupt"),
        (OSError("permission denied: /srv/secret/path"),
         "state unavailable"),
    ],
)
def test_break_state_exception_reason_is_fixed(exc, expected, monkeypatch):
    def _raise():
        raise exc

    monkeypatch.setattr("rule_engine.liveness.is_on_break", _raise)

    state = _break_state()

    assert state["on_break"] is True
    assert state["reason"] == expected
    assert state["break_until_epoch"] == 0.0
    blob = json.dumps(state)
    assert "/srv/secret" not in blob
    assert "break state at" not in blob


# ---------------------------------------------------------------------------
# /api/binding.json: no wallet or fee-payer echo
# ---------------------------------------------------------------------------

def _binding_tx(wallet: str = "WALLET123", mint: str = "MINT456") -> dict:
    return {
        "blockTime": 1798761600,
        "meta": {
            "err": None,
            "preTokenBalances": [{"mint": mint}],
            "postTokenBalances": [{"mint": mint}],
        },
        "transaction": {"message": {"accountKeys": [{"pubkey": wallet}]}},
    }


def test_binding_mismatch_withholds_wallet_and_fee_payer():
    configured_wallet = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
    attacker_key = "7b1d5f9c3e8a2d6b4f8c1a9e3b7d5f2c6a8e4b9d1f3c7a5e"

    result = _verify_binding_checks(
        _binding_tx(wallet=attacker_key),
        {"mint": "MINT456", "kind": "buy"},
        "2026-08-26T10:00:00+00:00",
        configured_wallet,
    )

    fee_check = next(c for c in result["checks"] if c["name"] == "fee_payer")
    assert fee_check["status"] == "fail"
    blob = json.dumps(result)
    assert configured_wallet not in blob
    assert attacker_key not in blob
    assert "withheld" in fee_check["detail"]


def test_binding_transaction_error_text_is_not_echoed():
    marker = "provider said /srv/operator/secret-token"
    tx = _binding_tx()
    tx["meta"]["err"] = {"InstructionError": [0, {"Custom": marker}]}

    result = _verify_binding_checks(
        tx, {"mint": "MINT456", "kind": "buy"},
        "2026-08-26T10:00:00+00:00", "WALLET123")

    blob = json.dumps(result)
    confirm = next(c for c in result["checks"] if c["name"] == "tx_confirmed")
    assert confirm["status"] == "fail"
    assert confirm["detail"] == "transaction error present"
    assert marker not in blob


@pytest.mark.asyncio
async def test_memo_transaction_error_text_is_not_echoed():
    marker = "rpc error at /srv/operator/secret-token"
    tx = {
        "slot": 10,
        "meta": {"err": {"InstructionError": [0, marker]}, "logMessages": []},
    }

    async def _get_tx(_sig):
        return tx

    block = await _verify_memo(
        {"memo_signature": "memo-sig", "memo_slot": 1, "signature": None},
        "recomputed", _get_tx, "commit:v1:")

    memo_check = next(c for c in block["checks"]
                      if c["name"] == "memo_confirmed")
    assert memo_check["detail"] == "transaction error present"
    assert marker not in json.dumps(block)


# ---------------------------------------------------------------------------
# Live book: fixed fail-soft reasons
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_portfolio_exception_reason_is_fixed(monkeypatch):
    marker = "/srv/operator/live/executions.json"
    monkeypatch.setattr(config, "LIVE_BOOK_PUBLIC", True)

    async def _boom(_request):
        raise RuntimeError(f"ledger unreadable at {marker}")

    monkeypatch.setattr(live_book, "_build", _boom)

    result = await live_book.get_live_portfolio(None)

    assert result == {"enabled": False, "reason": "unreadable"}
    assert marker not in json.dumps(result)


@pytest.mark.asyncio
async def test_live_executions_exception_reason_is_fixed(monkeypatch):
    marker = "/srv/operator/live/commits.json"
    monkeypatch.setattr(config, "LIVE_BOOK_PUBLIC", True)

    def _boom():
        raise RuntimeError(f"commit log unreadable at {marker}")

    monkeypatch.setattr(live_book, "_build_executions", _boom)

    result = await live_book.get_live_executions(None)

    assert result == {"enabled": False, "reason": "unreadable"}
    assert marker not in json.dumps(result)


def test_live_executions_corrupt_ledger_reason_is_fixed(monkeypatch):
    marker = "/srv/operator/live/executions.json"

    from live_execution import commit_log as commit_log_mod
    from live_execution import config as le_config
    from live_execution import models as models_mod

    class _FakeCommitLog:
        def __init__(self, _path):
            pass

        def recent(self, limit=100):
            return []

    class _FakeLedger:
        def __init__(self, _path):
            pass

        def _load(self):
            raise RuntimeError(f"ledger corrupt at {marker}")

    monkeypatch.setattr(le_config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(commit_log_mod, "CommitLog", _FakeCommitLog)
    monkeypatch.setattr(models_mod, "ExecutionLedger", _FakeLedger)

    result = live_book._build_executions()

    assert result == {"enabled": False, "reason": "ledger corrupt"}
    assert marker not in json.dumps(result)
