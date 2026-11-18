"""Offline regressions for the November repository and deployment audit."""
from __future__ import annotations

from string import hexdigits

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import preflight_deploy as pf


@pytest.mark.parametrize("value", [None, "", "bad", "NaN", "Infinity", True, False, 10**1000])
def test_quote_impact_is_a_known_finite_fact(value):
    from live_execution import executor, jupiter_executor as je
    with pytest.raises(je.ExecutionError, match="price impact"):
        executor.quote_impact_pct({"priceImpactPct": value})


def test_partial_sell_reduces_the_entire_mint_and_is_idempotent(tmp_path):
    from live_execution.models import ExecutionLedger
    ledger = ExecutionLedger(tmp_path / "executions.json")
    ledger.record_buy("first", "mint", 10., 100., .1, "buy1", status="confirmed")
    ledger.record_buy("second", "mint", 30., 300., .1, "buy2", status="confirmed")
    first = ledger.reduce_position("mint", .5, 25., signature="sell1")
    assert ledger.open_token_amounts()["mint"] == pytest.approx(200.)
    assert sum(r["usd_size"] for r in ledger._load()
               if r["kind"] == "buy" and r["status"] == "confirmed") == pytest.approx(20.)
    assert first.pnl_usd == pytest.approx(5.)
    assert ledger.reduce_position("mint", .5, 25., signature="sell1") == first
    assert ledger.open_token_amounts()["mint"] == pytest.approx(200.)


def test_unicode_cookie_is_invalid_instead_of_raising(monkeypatch):
    from api import auth
    import config
    monkeypatch.setattr(config, "SESSION_SECRET", hexdigits[:16] * 4)
    cookie = f"{int(time.time())}.nonce.é"
    assert auth.verify_session_token(cookie) is False
    auth.revoke_session_token(cookie)


async def test_unicode_operator_token_is_rejected(monkeypatch):
    import config
    import httpx
    from api.main import app
    monkeypatch.setattr(config, "ADMIN_TOKEN", hexdigits[:16] * 4)
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        response = await client.post("/api/auth/login", json={"token": "é" * 40})
    assert response.status_code == 403


@pytest.mark.parametrize("payload", [[], None, "bad", {"version": 1},
    {"version": 1, "revoked": {"0" * 64: "not-an-expiry"}},
    {"version": 1, "revoked": {"0" * 64: float("inf")}}])
def test_corrupt_revocation_shape_cannot_restore_a_session(tmp_path, monkeypatch, payload):
    import config
    from api import auth
    monkeypatch.setattr(config, "SESSION_SECRET", hexdigits[:16] * 4)
    path = tmp_path / "revocations.json"
    monkeypatch.setattr(config, "SESSION_REVOCATION_FILE", str(path))
    token = auth.create_session_token()
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(auth, "_REVOCATIONS_LOADED", False)
    monkeypatch.setattr(auth, "_REVOCATIONS_ERROR", False)
    assert auth.verify_session_token(token) is False


@pytest.mark.parametrize("data", [{}, {"program": "unknown", "parsed": {"info": {}}},
    {"program": "spl-token", "parsed": {"type": "account", "info": {}}},
    {"program": "spl-token-2022", "parsed": {"info": {"extensions": "bad"}}}])
def test_malformed_mint_security_remains_unknown(data):
    from data_providers.onchain_security import parse_mint_extensions
    assert parse_mint_extensions(data).get("program") is None


async def test_malformed_mint_security_rotates_rpc_and_is_not_cached(monkeypatch):
    import config
    import httpx
    from data_providers import onchain_security as ocs
    ocs._MINT_EXTENSION_CACHE.clear()
    monkeypatch.setattr(config, "ONCHAIN_RPC_URLS", ["https://first.invalid", "https://second.invalid"])
    calls = []
    async def request(client, method, endpoint, **kwargs):
        calls.append(endpoint)
        data = {} if len(calls) == 1 else {"program": "spl-token", "parsed": {"type": "mint", "info": {}}}
        return httpx.Response(200, json={"result": {"value": {"data": data}}})
    monkeypatch.setattr(ocs, "bounded_request", request)
    flags = await ocs.get_mint_extension_flags("audit-mint")
    assert flags["program"] == "spl-token"
    assert len(calls) == 2
    assert ocs._MINT_EXTENSION_CACHE["audit-mint"][1] == flags


@pytest.mark.parametrize("value", ["x" * 64, "01234567" * 4])
def test_preflight_rejects_credentials_the_api_will_reject(value):
    assert pf.check_admin_token({"ADMIN_TOKEN": value}).status == pf.FAIL


@pytest.mark.parametrize("secret", ["weak", "x" * 64, hexdigits[:16] * 4])
def test_preflight_validates_dedicated_session_key(secret):
    assert pf.check_runtime_security({"ADMIN_TOKEN": hexdigits[:16] * 4,
                                      "SESSION_SECRET": secret}).status == pf.FAIL


def test_security_monitor_bounds_every_ip_map_and_burst_queue():
    from api.security_monitor import SecurityMonitor, SecurityEventType
    monitor = SecurityMonitor(max_burst=2)
    for i in range(4):
        for _ in range(10):
            monitor.record_event(SecurityEventType.AUTH_FAILURE, client_ip=f"192.0.2.{i}")
    assert len(monitor._ip_counts) <= 2
    assert len(monitor._auth_failures_by_ip) <= 2
    assert all(len(queue) <= 5 for queue in monitor._auth_failures_by_ip.values())


from tests.test_audit_88_f import stopped_cycle_env  # noqa: E402,F401


async def test_fixed_live_sizing_applies_unknown_security_haircut(stopped_cycle_env, monkeypatch):
    import decision_pipeline
    import run_live_cycle as rlc
    env = stopped_cycle_env
    original = decision_pipeline.read_candidates
    async def candidates(provider):
        rows = await original(provider)
        rows[0].security_unknown = True
        return rows
    monkeypatch.setattr(decision_pipeline, "read_candidates", candidates)
    monkeypatch.setattr(rlc.paper_config, "SIZING_MODE", "fixed")
    monkeypatch.setattr(rlc.paper_config, "TICKET_CASH_FRACTION", .15)
    monkeypatch.setattr(rlc.paper_config, "TICKET_MAX_USD", 1000.)
    monkeypatch.setattr(rlc.paper_config, "UNKNOWN_SECURITY_SIZE_MULT", .5)
    monkeypatch.setattr(rlc.live_config, "min_live_ticket_usd", lambda equity: 1.)
    place = AsyncMock(return_value=SimpleNamespace(status="blocked", reason="dummy"))
    monkeypatch.setattr(rlc, "place_order", place)
    await rlc.run_cycle(provider=env.provider, thinker=env.thinker, stop=asyncio.Event())
    assert place.call_args.kwargs["usd"] == pytest.approx(75.)


@pytest.fixture
def manual_flow(tmp_path, monkeypatch):
    from live_execution import jupiter_executor as je, solana
    from live_execution.models import ExecutionLedger
    from live_execution import memo
    monkeypatch.setattr(memo, "publish_commit_memo", AsyncMock(return_value={"signature": "dummy-memo", "slot": 1}))
    monkeypatch.setattr(je.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(je.config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(je.config, "REQUIRE_MANUAL_CONFIRMATION", False)
    monkeypatch.setattr(je.wallet, "load_keypair", lambda: object())
    monkeypatch.setattr(je.wallet, "verify_expected_address", lambda payer: "dummy-wallet")
    monkeypatch.setattr(solana, "get_mint_decimals", AsyncMock(return_value=6))
    monkeypatch.setattr(je, "_get_json", AsyncMock(return_value={"outAmount": "10000000", "priceImpactPct": "0"}))
    monkeypatch.setattr(je, "_build_swap_transaction", AsyncMock(return_value="dummy"))
    signed = solana.SignedSwap("local-dummy-signature", b"dummy", _checked=True, blockhash="dummy-blockhash")
    monkeypatch.setattr(je, "sign_after_checks", AsyncMock(return_value=signed))
    monkeypatch.setattr(solana, "send_checked_swap", AsyncMock(return_value=signed.signature))
    monkeypatch.setattr(solana, "confirm_signature", AsyncMock(return_value={"confirmed": True, "slot": 10, "err": None}))
    monkeypatch.setattr(solana, "get_signature_status_all_strict", AsyncMock(return_value=[]))
    return je, solana, ExecutionLedger(tmp_path / "executions.json")


@pytest.mark.parametrize("case", ["foreign-receipt", "ambiguous-error", "contradictory-confirmation"])
async def test_manual_swap_retains_local_pending_authority(manual_flow, monkeypatch, case):
    je, solana, ledger = manual_flow
    if case == "foreign-receipt":
        monkeypatch.setattr(solana, "send_checked_swap", AsyncMock(return_value="foreign-signature"))
    elif case == "ambiguous-error":
        monkeypatch.setattr(solana, "send_checked_swap", AsyncMock(return_value=None))
        monkeypatch.setattr(solana, "get_signature_status", AsyncMock(return_value={"err": "failure"}))
    else:
        monkeypatch.setattr(solana, "confirm_signature", AsyncMock(return_value={"confirmed": True, "slot": 10, "err": "failure"}))
    try:
        await je.execute_confirmed_trade("mint", 6, 10., idempotency_key="intent", ledger=ledger, reference_price_usd=1.)
    except je.ExecutionError:
        pass
    row = ledger.get_by_idempotency_key("intent")
    assert row.status == "pending"
    assert row.signature == "local-dummy-signature"


@pytest.mark.parametrize("foreign", [True, False])
async def test_commit_memo_requires_its_own_signature_and_valid_finality(monkeypatch, foreign):
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    from live_execution import memo, solana
    monkeypatch.setattr(solana, "latest_blockhash", AsyncMock(return_value="4uQeVj5tqViQh7yWWGStvkEG1Zmhx6uasJtWCJziofM"))
    async def send(raw, **kwargs):
        return "foreign-signature" if foreign else str(VersionedTransaction.from_bytes(raw).signatures[0])
    monkeypatch.setattr(solana, "send_raw_transaction", send)
    monkeypatch.setattr(solana, "confirm_signature", AsyncMock(return_value={"confirmed": True, "slot": 10, "err": None if foreign else "failed"}))
    with pytest.raises(memo.MemoPublishError):
        await memo.publish_commit_memo(Keypair(), "ab" * 32)


def test_preflight_dotenv_comments_match_service_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text('DATA_BACKEND=live # production\nADMIN_TOKEN="abc#def" # comment\nURL=https://example.invalid/#fragment\n')
    assert pf.load_env_file(path) == {"DATA_BACKEND": "live", "ADMIN_TOKEN": "abc#def", "URL": "https://example.invalid/#fragment"}


def test_preflight_checks_rendered_credentials_over_shell_values(tmp_path):
    env = {"ADMIN_TOKEN": hexdigits[:16] * 4}
    config = {"services": {"api": {"environment": {"ADMIN_TOKEN": "weak"}}, "live": {}}}
    checks = pf.run_preflight(env, tmp_path, compose_config=config)
    assert next(c for c in checks if c.name == "admin token").status == pf.FAIL


async def test_postgres_wipe_rolls_back_partial_failure():
    from api import db_pg
    class Connection:
        def __init__(self):
            self.rows = ["feed", "trade"]
            self.calls = 0
        def transaction(self):
            conn = self
            class Transaction:
                async def __aenter__(self): self.before = list(conn.rows)
                async def __aexit__(self, kind, error, trace):
                    if error: conn.rows[:] = self.before
            return Transaction()
        async def fetchrow(self, sql): return {"n": 1}
        async def execute(self, sql, *args):
            self.calls += 1
            if self.calls == 2: raise RuntimeError("dummy database failure")
            self.rows.pop(0)
    conn = Connection()
    with pytest.raises(RuntimeError, match="dummy database failure"):
        await db_pg.wipe_paper_book(conn, 1000.)
    assert conn.rows == ["feed", "trade"]


@pytest.mark.parametrize("status", [
    {"confirmationStatus": "confirmed", "slot": 10},
    {"confirmationStatus": "finalized", "err": None, "slot": True},
    {"confirmationStatus": "confirmed", "err": None},
])
async def test_rpc_confirmation_cannot_invent_missing_success_fields(monkeypatch, status):
    from live_execution import solana
    monkeypatch.setattr(solana, "rpc", AsyncMock(return_value={"value": [status]}))
    monkeypatch.setattr(solana.asyncio, "sleep", AsyncMock())
    result = await solana.confirm_signature("dummy", timeout_s=.001)
    assert result["confirmed"] is False


async def test_explicit_drill_single_rpc_does_not_relax_default_signer_quorum(monkeypatch):
    from live_execution import solana
    import httpx
    monkeypatch.setattr(solana.config, "SIM_MIN_AGREEING_RPCS", 2)
    response = httpx.Response(200, json={"result": {"value": {"err": None, "accounts": []}}})
    monkeypatch.setattr(solana, "bounded_request", AsyncMock(return_value=response))
    result = await solana.simulate_transaction("dummy", [], endpoints=["https://one.invalid"], require_dual_rpc=False)
    assert result["agreeing_count"] == 1
    with pytest.raises(solana.SimulationError, match="quorum"):
        await solana.simulate_transaction("dummy", [], endpoints=["https://one.invalid"])


async def test_listing_handshake_without_data_has_bounded_retries(monkeypatch):
    import websockets
    from data_providers import new_listings as mod
    count = 0
    class Socket:
        async def __aenter__(self):
            if count > 3: raise RuntimeError("fixture safety bound")
            return self
        async def __aexit__(self, *args): pass
        async def send(self, data): pass
        async def recv(self): raise RuntimeError("subscription closed")
    def connect(*args, **kwargs):
        nonlocal count
        count += 1
        return Socket()
    real_wait = asyncio.wait_for
    async def wait(awaitable, timeout):
        if timeout < 120:
            awaitable.close()
            raise asyncio.TimeoutError
        return await real_wait(awaitable, timeout)
    monkeypatch.setattr(websockets, "connect", connect)
    monkeypatch.setattr(mod.asyncio, "wait_for", wait)
    await mod.NewListingFeed()._run()
    assert count == 3


async def test_dropped_alert_queue_entry_is_completed(monkeypatch):
    import alerts
    queue = asyncio.Queue(maxsize=2)
    queue.put_nowait(object())
    queue.put_nowait(object())
    monkeypatch.setattr(alerts, "_queue", queue)
    monkeypatch.setattr(alerts, "_ensure_worker_running", lambda: None)
    alerts.alert_nowait(alerts.AlertEvent.TEST, "dummy")
    while not queue.empty():
        queue.get_nowait()
        queue.task_done()
    assert queue._unfinished_tasks == 0


async def test_alert_deployment_probe_requires_a_configured_recipient(monkeypatch):
    from scripts import send_test_alert
    monkeypatch.setattr(send_test_alert.sys, "argv", ["send_test_alert.py"])
    monkeypatch.setattr(send_test_alert, "get_configured_channels", lambda: [])
    assert await send_test_alert.main() == 1


@pytest.mark.parametrize("payload", [[], None, {"mints": []}, {"mints": {"mint": {"closes": [None]}}}])
def test_malformed_blocklist_uses_documented_nonfatal_recovery(tmp_path, monkeypatch, payload):
    import blocklist, config
    path = tmp_path / "blocks.json"
    monkeypatch.setattr(config, "BLOCKLIST_STATE_FILE", str(path))
    path.write_text(json.dumps(payload))
    assert blocklist.filter_candidates([]) == ([], [])
    assert path.with_suffix(".corrupt").is_file()


def test_blocklist_reports_failed_persistence(tmp_path, monkeypatch):
    import blocklist, config
    monkeypatch.setattr(config, "BLOCKLIST_STATE_FILE", str(tmp_path / "blocks.json"))
    blocklist.block_mint("mint", "TEST", "dummy block")
    monkeypatch.setattr(blocklist, "_save", lambda data: (_ for _ in ()).throw(OSError("dummy failure")))
    assert blocklist.unblock_mint("mint") is False
    assert blocklist.block_mint("other", "TEST", "dummy") is False
    assert blocklist.is_blocked_mint("mint")


@pytest.mark.parametrize("name", ["break", "kill"])
def test_safety_state_directory_is_unreadable_instead_of_clear(tmp_path, monkeypatch, name):
    if name == "break":
        from rule_engine import liveness
        import config
        path = tmp_path / "break.json"
        path.mkdir()
        monkeypatch.setattr(config, "BREAK_STATE_FILE", str(path))
        check = liveness.is_on_break
    else:
        from live_execution import kill_switch
        (tmp_path / "kill_switch.json").mkdir()
        check = lambda: kill_switch.is_tripped(tmp_path)
    with pytest.raises(RuntimeError): check()


@pytest.mark.parametrize("proceeds", [-1., float("nan"), float("inf"), True])
async def test_vanished_repair_refuses_invalid_proceeds_before_work(monkeypatch, proceeds):
    from live_execution.scripts import repair_vanished as repair
    monkeypatch.setattr(repair, "_ledger", lambda: (_ for _ in ()).throw(AssertionError("must not inspect ledger")))
    assert await repair._close("mint", proceeds, "dummy") == 1


async def test_manual_pending_row_preserves_commit_material(manual_flow, monkeypatch):
    je, solana, ledger = manual_flow
    monkeypatch.setattr(solana, "send_checked_swap", AsyncMock(return_value=None))
    with pytest.raises(je.ExecutionError):
        await je.execute_confirmed_trade("mint", 6, 10., idempotency_key="intent", ledger=ledger, reference_price_usd=1.)
    pending = ledger.get_by_idempotency_key("intent")
    assert pending.commit_hash and pending.commit_nonce and pending.commit_payload
    assert pending.memo_signature == "dummy-memo" and pending.memo_slot == 1


async def test_captcha_configuration_exposes_only_public_key(monkeypatch):
    from api.routes import auth
    import config
    monkeypatch.setattr(config, "CAPTCHA_ENABLED", True)
    monkeypatch.setattr(config, "CAPTCHA_PROVIDER", "hcaptcha")
    monkeypatch.setattr(config, "CAPTCHA_SITE_KEY", "public-site-identifier")
    monkeypatch.setattr(config, "CAPTCHA_SECRET_KEY", "private-verifier-secret")
    value = await auth.get_challenge()
    assert value["site_key"] == "public-site-identifier" and value["enabled"]
    assert "private-verifier-secret" not in json.dumps(value)


def test_compose_api_clears_host_wallet_selector_too():
    import yaml
    from pathlib import Path
    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text())
    api = pf._service_env(compose["services"]["api"])
    assert api["WALLET_KEYPAIR_PATH"] == ""
    assert api["WALLET_KEYPAIR_JSON"] == ""
    assert api["WALLET_KEYPAIR_HOST_PATH"] == ""


@pytest.mark.parametrize("inaccessible", ["directory", "permission"])
def test_unreadable_revocation_store_cannot_resurrect_a_cookie(tmp_path, monkeypatch, inaccessible):
    from api import auth
    import config
    path = tmp_path / "revocations.json"
    monkeypatch.setattr(config, "SESSION_SECRET", hexdigits[:16] * 4)
    monkeypatch.setattr(config, "SESSION_REVOCATION_FILE", str(path))
    cookie = auth.create_session_token()
    if inaccessible == "directory":
        path.mkdir()
    else:
        original = type(path).read_text
        def read(self, *args, **kwargs):
            if self == path: raise PermissionError("dummy inaccessible registry")
            return original(self, *args, **kwargs)
        monkeypatch.setattr(type(path), "read_text", read)
    monkeypatch.setattr(auth, "_REVOCATIONS_LOADED", False)
    monkeypatch.setattr(auth, "_REVOCATIONS_ERROR", False)
    assert not auth.verify_session_token(cookie)


@pytest.mark.parametrize("kind", ["ledger", "commits", "approvals"])
def test_nonfile_authority_store_cannot_look_empty(tmp_path, kind):
    from live_execution.models import ExecutionLedger
    from live_execution.commit_log import CommitLog
    from live_execution.confirmation_queue import ConfirmationQueue
    path = tmp_path / "authority.json"
    path.mkdir()
    store = {"ledger": ExecutionLedger, "commits": CommitLog, "approvals": ConfirmationQueue}[kind](path)
    with pytest.raises(RuntimeError): store._load()
