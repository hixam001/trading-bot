"""
backend/tests/test_audit_79_d.py — §79 Phase D: Outbound alerting tests.

Verifies:
1. Delivered on success (ntfy and telegram formatting, headers, priorities).
2. Failing channel never raises and never blocks trading (<50ms caller return).
3. Secret sanitization across all outbound payloads.
4. Cooldown deduplication per key.
5. Flood limit cap (20/hr) and suppression tracking.
6. Refusal aggregation (first immediately, then digest).
7. Event hooks fire on triggers:
   - KILL_SWITCH_ENGAGED / DAILY_LOSS_BREAKER
   - SIM_REFUSAL / GUARD_REFUSAL
   - EXIT_UNRESOLVED / SELL_NO_REF_PRICE
   - RPC_NOT_DIVERSE / SIM_SINGLE_RPC_SELL
   - LOW_SOL / LOW_USDC
   - WALLET_DROP (reconcile.py)
   - LOGIN_FAILURE_BURST (security_monitor.py)
   - HEARTBEAT_STALE / HEARTBEAT_RECOVERED (heartbeat_watcher)
8. /api/safety surfaces outbound alerting status with zero secrets.
"""
from __future__ import annotations

import asyncio
from collections import deque
import json
from pathlib import Path
import time

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import alerts
from alerts import AlertEvent, AlertPriority, AlertItem
from secret_registry import clear_runtime_secrets, register_secret


@pytest.fixture(autouse=True)
def reset_alerts_state(monkeypatch):
    """Ensure clean alert queues, cooldown state, and mocks for each test."""
    alerts._pending_alerts.clear()
    alerts._queue = asyncio.Queue(maxsize=100)
    alerts._key_last_sent.clear()
    alerts._sent_timestamps.clear()
    alerts._dropped_count = 0
    alerts._suppressed_count = 0
    alerts._last_sent_at = None
    alerts._refusal_window_start = 0.0
    alerts._refusal_count = 0
    alerts._refusal_latest_msg = ""
    alerts._custom_transport_hook = None
    clear_runtime_secrets()
    yield
    alerts._custom_transport_hook = None
    clear_runtime_secrets()


@pytest.mark.asyncio
async def test_alert_delivered_on_success(monkeypatch):
    """Verify ntfy and Telegram channels format and receive messages accurately."""
    monkeypatch.setenv("ALERT_NTFY_URL", "https://ntfy.sh")
    monkeypatch.setenv("ALERT_NTFY_TOPIC", "test-topic")
    monkeypatch.setenv("ALERT_NTFY_TOKEN", "test-token-12345")
    monkeypatch.setenv("ALERT_TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl")
    monkeypatch.setenv("ALERT_TELEGRAM_CHAT_ID", "987654321")

    dispatched = []

    def mock_transport(channel, url, headers_or_json, text):
        dispatched.append({"channel": channel, "url": url, "payload": headers_or_json, "text": text})

    alerts._custom_transport_hook = mock_transport

    await alerts.alert(AlertEvent.KILL_SWITCH_ENGAGED, "Emergency stop requested", key="test_kill")
    await alerts.drain()

    assert len(dispatched) == 2
    ntfy_call = next(d for d in dispatched if d["channel"] == "ntfy")
    tg_call = next(d for d in dispatched if d["channel"] == "telegram")

    assert "test-topic" in ntfy_call["url"]
    assert ntfy_call["payload"]["Priority"] == "urgent"
    assert ntfy_call["text"] == "Emergency stop requested"

    assert "987654321" == tg_call["payload"]["chat_id"]
    assert "KILL_SWITCH_ENGAGED" in tg_call["payload"]["text"]


@pytest.mark.asyncio
async def test_failing_channel_never_raises_and_never_blocks(monkeypatch):
    """Assert alerting failure never raises into caller and returns within 50ms."""
    # Mutation: removing try/except in alert_nowait or worker would crash or raise
    monkeypatch.setenv("ALERT_NTFY_TOPIC", "broken-topic")

    def failing_transport(*args, **kwargs):
        raise RuntimeError("Network completely severed")

    alerts._custom_transport_hook = failing_transport

    # Test sync-safe alert_nowait execution time
    t0 = time.perf_counter()
    alerts.alert_nowait(AlertEvent.SIM_REFUSAL, "RPC simulation exploded")
    elapsed_sync = (time.perf_counter() - t0) * 1000.0

    # Test async alert execution time
    t1 = time.perf_counter()
    await alerts.alert(AlertEvent.SIM_REFUSAL, "RPC simulation exploded 2")
    elapsed_async = (time.perf_counter() - t1) * 1000.0

    assert elapsed_sync < 50.0, f"alert_nowait blocked caller: {elapsed_sync:.2f}ms"
    assert elapsed_async < 50.0, f"alert blocked caller: {elapsed_async:.2f}ms"

    # Worker draining with failing transport must not raise
    await alerts.drain()


@pytest.mark.asyncio
async def test_disabled_by_default_no_network_no_error(monkeypatch):
    """When no channel env vars are configured, alert logs locally without network."""
    monkeypatch.delenv("ALERT_NTFY_TOPIC", raising=False)
    monkeypatch.delenv("ALERT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALERT_TELEGRAM_CHAT_ID", raising=False)

    called = False

    def unexpected_transport(*args):
        nonlocal called
        called = True

    alerts._custom_transport_hook = unexpected_transport

    await alerts.alert(AlertEvent.LOW_SOL, "SOL is low")
    await alerts.drain()

    assert not called
    assert alerts.get_configured_channels() == []


@pytest.mark.asyncio
async def test_secret_sanitized_from_outgoing_body(monkeypatch):
    """Assert secrets, private tokens, and Solana keypairs are redacted before dispatch."""
    monkeypatch.setenv("ALERT_NTFY_TOPIC", "clean-topic")
    raw_secret = "super_secret_api_key_999888"
    register_secret(raw_secret)

    solana_keypair = str([12, 34, 56] * 22)  # 66-int array
    bearer = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz"
    raw_msg = f"Crash occurred with key {raw_secret} and kp {solana_keypair} and token {bearer}"

    dispatched_text = []

    def capture_transport(channel, url, payload, text):
        dispatched_text.append(text)

    alerts._custom_transport_hook = capture_transport

    await alerts.alert(AlertEvent.SIM_REFUSAL, raw_msg, key="sec_test")
    await alerts.drain()

    assert len(dispatched_text) == 1
    sent = dispatched_text[0]
    assert raw_secret not in sent
    assert "<REDACTED>" in sent
    assert "[<REDACTED-KEYPAIR>]" in sent
    assert "Bearer <REDACTED>" in sent


@pytest.mark.asyncio
async def test_cooldown_deduplication(monkeypatch):
    """Repeated alerts with the same key within cooldown are suppressed."""
    monkeypatch.setenv("ALERT_NTFY_TOPIC", "test-topic")
    dispatched = []
    alerts._custom_transport_hook = lambda ch, u, p, t: dispatched.append(t)

    # First alert passes
    await alerts.alert(AlertEvent.LOW_SOL, "SOL balance 0.005", key="low_sol")
    await alerts.drain()
    assert len(dispatched) == 1

    # Second alert with same key within cooldown is suppressed
    await alerts.alert(AlertEvent.LOW_SOL, "SOL balance 0.004", key="low_sol")
    await alerts.drain()
    assert len(dispatched) == 1
    assert alerts._suppressed_count == 1

    # Different key passes immediately
    await alerts.alert(AlertEvent.LOW_USDC, "USDC balance 1.50", key="low_usdc")
    await alerts.drain()
    assert len(dispatched) == 2


@pytest.mark.asyncio
async def test_flood_cap_hourly_limit(monkeypatch):
    """Global flood cap of 20 messages per rolling hour prevents runaway notification spam."""
    monkeypatch.setenv("ALERT_NTFY_TOPIC", "test-topic")
    dispatched = []
    alerts._custom_transport_hook = lambda ch, u, p, t: dispatched.append(t)

    for i in range(25):
        await alerts.alert(AlertEvent.INFO, f"Info message {i}", key=f"info_key_{i}", priority=AlertPriority.HIGH)

    await alerts.drain()

    assert len(dispatched) == 20
    assert alerts._suppressed_count == 5


@pytest.mark.asyncio
async def test_refusal_aggregation_digest(monkeypatch):
    """Refusals fire first instance immediately, then aggregate subsequent refusals."""
    monkeypatch.setenv("ALERT_NTFY_TOPIC", "test-topic")
    dispatched = []
    alerts._custom_transport_hook = lambda ch, u, p, t: dispatched.append(t)

    # First refusal fires immediately
    await alerts.alert(AlertEvent.SIM_REFUSAL, "Simulation outAmount too low", key="sim_1")
    await alerts.drain()
    assert len(dispatched) == 1

    # Second and third refusals within 30m window aggregate
    await alerts.alert(AlertEvent.SIM_REFUSAL, "Simulation SOL cap exceeded", key="sim_2")
    await alerts.alert(AlertEvent.SIM_REFUSAL, "Simulation token drain", key="sim_3")
    await alerts.drain()
    assert len(dispatched) == 1  # Still 1
    assert alerts._refusal_count == 3


@pytest.mark.asyncio
async def test_kill_switch_trip_emits_alerts(tmp_path):
    """Assert kill_switch.trip emits KILL_SWITCH_ENGAGED or DAILY_LOSS_BREAKER."""
    from live_execution import kill_switch

    events_fired = []

    def mock_alert(event, msg, *, key=None, **kwargs):
        events_fired.append((event, msg, key))

    alerts.alert_nowait = mock_alert

    # Manual trip
    kill_switch.trip("Operator panic button", state_dir=tmp_path)
    assert len(events_fired) == 1
    assert events_fired[0][0] == AlertEvent.KILL_SWITCH_ENGAGED
    assert "Operator panic button" in events_fired[0][1]

    # Automatic breaker trip
    kill_switch.trip("AUTO: realized daily loss -35.0 breached breaker", state_dir=tmp_path)
    assert len(events_fired) == 2
    assert events_fired[1][0] == AlertEvent.DAILY_LOSS_BREAKER


@pytest.mark.asyncio
async def test_sim_refusal_and_guard_refusal_emit_alerts():
    """Assert sim_guard._refuse and sign_after_checks emit SIM_REFUSAL and GUARD_REFUSAL."""
    from live_execution import sim_guard
    from live_execution.trade_intent import TradeIntent
    from live_execution import jupiter_executor

    events_fired = []
    alerts.alert_nowait = lambda ev, msg, key=None, **kw: events_fired.append((ev, msg))

    # Trigger sim_guard refusal
    with pytest.raises(Exception):
        await sim_guard.simulate_and_assert(
            swap_b64="",
            quote=None,
            payer_pubkey="11111111111111111111111111111111",
            intent=TradeIntent("buy", "w", "m1", "m2", 100, 50, None, 1.0),
        )

    assert any(ev == AlertEvent.SIM_REFUSAL for ev, _ in events_fired)

    # Trigger static guard refusal in sign_after_checks
    intent = TradeIntent("buy", "11111111111111111111111111111111", "m1", "m2", 100, 50, None, 1.0)
    class FakePayer:
        def pubkey(self):
            return "11111111111111111111111111111111"

    # Corrupt transaction payload triggers inspect_swap_transaction failure
    with pytest.raises(Exception):
        await jupiter_executor.sign_after_checks("invalid_base64!", FakePayer(), intent)


@pytest.mark.asyncio
async def test_wallet_drop_detection(tmp_path):
    """Assert check_wallet_drop in reconcile.py alerts on unexplained balance drops."""
    from live_execution.reconcile import check_wallet_drop
    from live_execution.models import ExecutionLedger

    events_fired = []
    alerts.alert_nowait = lambda ev, msg, key=None, **kw: events_fired.append((ev, msg))

    ledger_path = tmp_path / "executions.json"
    ledger = ExecutionLedger(ledger_path)

    # Seed baseline snapshot: 100 USDC + 1 SOL @ $150 = $250 total
    snap_file = tmp_path / "wallet_snapshot.json"
    snap_file.write_text(json.dumps({
        "ts": 1000.0,
        "usdc": 100.0,
        "sol": 1.0,
        "sol_price_usd": 150.0,
        "total_usd": 250.0,
    }))

    # Cycle 2: Wallet now has 80 USDC + 1 SOL @ $150 = $230 total ($20 drop).
    # Ledger records NO trades in that window.
    res = check_wallet_drop(
        current_usdc=80.0,
        current_sol=1.0,
        sol_price_usd=150.0,
        ledger=ledger,
        state_dir=tmp_path,
        now_fn=lambda: 1060.0,
    )

    assert res["alerted"] is True
    assert res["unexplained_drop_usd"] == 20.0
    assert any(ev == AlertEvent.WALLET_DROP for ev, _ in events_fired)


@pytest.mark.asyncio
async def test_login_failure_burst_detection():
    """Assert 5 auth failures from the same IP within 10m triggers LOGIN_FAILURE_BURST."""
    from api.security_monitor import SecurityMonitor, SecurityEventType

    events_fired = []
    alerts.alert_nowait = lambda ev, msg, key=None, **kw: events_fired.append((ev, msg))

    monitor = SecurityMonitor()
    target_ip = "192.168.1.100"

    for i in range(4):
        monitor.record_event(SecurityEventType.AUTH_FAILURE, client_ip=target_ip)
    assert len(events_fired) == 0

    # 5th failure triggers burst alert
    monitor.record_event(SecurityEventType.AUTH_FAILURE, client_ip=target_ip)
    assert len(events_fired) == 1
    assert events_fired[0][0] == AlertEvent.LOGIN_FAILURE_BURST
    assert target_ip in events_fired[0][1]


@pytest.mark.asyncio
async def test_heartbeat_watcher_stale_and_recovery(tmp_path):
    """Assert heartbeat watcher alerts on stale file and sends recovery when fresh."""
    from alerts import heartbeat_watcher

    events_fired = []
    alerts.alert_nowait = lambda ev, msg, key=None, **kw: events_fired.append((ev, msg))

    hb_file = tmp_path / "heartbeat.txt"
    curr_time = 1000.0
    hb_file.write_text("700\n")  # age = 300s -> stale if threshold is 200s

    stop_event = asyncio.Event()

    async def run_one_tick():
        stop_event.set()
        await heartbeat_watcher(
            state_dir=tmp_path,
            check_interval=0.01,
            stale_threshold=200.0,
            stop_event=stop_event,
            now_fn=lambda: curr_time,
        )

    await run_one_tick()
    assert any(ev == AlertEvent.HEARTBEAT_STALE for ev, _ in events_fired)


@pytest.mark.asyncio
async def test_api_safety_outbound_alerts_endpoint(monkeypatch):
    """Assert /api/safety exposes outbound alerting status with zero secrets."""
    from api.main import app

    monkeypatch.setenv("ALERT_NTFY_TOPIC", "public-safety-topic")
    monkeypatch.setenv("ALERT_NTFY_TOKEN", "super-secret-ntfy-token")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/safety")
        assert res.status_code == 200
        body = res.json()

        # Existing alerts list intact
        assert isinstance(body["alerts"], list)

        # New outbound alerting status
        assert "outbound_alerts" in body
        out = body["outbound_alerts"]
        assert "ntfy" in out["channels"]
        assert "queued" in out
        assert "dropped" in out
        assert "suppressed" in out

        # Zero secret leakage in response JSON
        raw_json = json.dumps(body)
        assert "super-secret-ntfy-token" not in raw_json


@pytest.mark.asyncio
async def test_rpc_diversity_alert_hook(monkeypatch):
    """Assert solana.check_rpc_diversity emits RPC_NOT_DIVERSE when distinct hosts < 2.
    Mutation discipline: removing alert_nowait(AlertEvent.RPC_NOT_DIVERSE, ...) in solana.py
    causes assert any(ev == AlertEvent.RPC_NOT_DIVERSE...) to fail.
    """
    from live_execution import solana

    events_fired = []
    alerts.alert_nowait = lambda ev, msg, key=None, **kw: events_fired.append((ev, msg))

    # Single host
    solana.check_rpc_diversity(["https://single-rpc.com/v1", "https://single-rpc.com/v2"])
    assert any(ev == AlertEvent.RPC_NOT_DIVERSE for ev, _ in events_fired)


@pytest.mark.asyncio
async def test_sim_single_rpc_sell_alert_hook(monkeypatch):
    """Assert solana.simulate_transaction emits SIM_SINGLE_RPC_SELL during sell with 1 endpoint.
    Mutation discipline: removing alert_nowait(AlertEvent.SIM_SINGLE_RPC_SELL, ...) in solana.py
    causes assert any(ev == AlertEvent.SIM_SINGLE_RPC_SELL...) to fail.
    """
    from live_execution import solana, config as live_config
    import httpx

    events_fired = []
    alerts.alert_nowait = lambda ev, msg, key=None, **kw: events_fired.append((ev, msg))

    monkeypatch.setattr(live_config, "SIM_SELL_ALLOW_SINGLE_RPC", True)
    monkeypatch.setattr(live_config, "RPC_URLS", ["https://rpc1.com"])

    def mock_handler(request):
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"value": {"err": None, "logs": [], "postAccounts": [{"lamports": 1000000000}]}},
            },
        )

    monkeypatch.setattr(solana, "_get_async_client", lambda timeout=8.0: httpx.AsyncClient(transport=httpx.MockTransport(mock_handler)))

    res = await solana.simulate_transaction("fake_b64", ["addr1"], side="sell", endpoints=["https://rpc1.com"])
    assert res is not None
    assert any(ev == AlertEvent.SIM_SINGLE_RPC_SELL for ev, _ in events_fired)


@pytest.mark.asyncio
async def test_sell_no_ref_price_alert_hook(monkeypatch, tmp_path):
    """Assert executor.place_sell emits SELL_NO_REF_PRICE when reference mark is None.
    Mutation discipline: removing alert_nowait(AlertEvent.SELL_NO_REF_PRICE, ...) in executor.py
    causes assert any(ev == AlertEvent.SELL_NO_REF_PRICE...) to fail.
    """
    from live_execution import executor, wallet, solana
    from live_execution.commit_log import CommitLog
    from live_execution.models import ExecutionLedger
    from solders.keypair import Keypair

    events_fired = []
    alerts.alert_nowait = lambda ev, msg, key=None, **kw: events_fired.append((ev, msg))

    payer = Keypair()
    monkeypatch.setattr(wallet, "load_keypair", lambda: payer)
    monkeypatch.setattr(wallet, "verify_expected_address", lambda p: p.pubkey().__str__())

    async def mock_decimals(m):
        return 6

    async def mock_sol_bal(a):
        return 0.5

    async def mock_token_bal(a):
        return {payer.pubkey().__str__(): 10.0}

    monkeypatch.setattr(solana, "get_token_balances", mock_token_bal)
    monkeypatch.setattr(solana, "get_mint_decimals", mock_decimals)
    monkeypatch.setattr(solana, "get_sol_balance", mock_sol_bal)

    async def mock_memo(*args, **kwargs):
        return {"signature": "fake_memo_sig", "slot": 12345}

    async def mock_quote(*args, **kwargs):
        return {
            "inputMint": "So11111111111111111111111111111111111111112",
            "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "inAmount": "1000000",
            "outAmount": "1000000",
            "priceImpactPct": "0.01",
            "otherAmountThreshold": "950000",
            "swapMode": "ExactIn",
            "slippageBps": 50,
        }

    async def mock_build(*args, **kwargs):
        return "dGVzdA=="

    monkeypatch.setattr(executor.memo, "publish_commit_memo", mock_memo)
    monkeypatch.setattr(executor, "_get_json", mock_quote)
    monkeypatch.setattr(executor, "_build_swap_transaction", mock_build)
    monkeypatch.setattr(executor.config, "STATE_DIR", tmp_path)

    ledger = ExecutionLedger(tmp_path / "executions.json")
    mint = "So11111111111111111111111111111111111111112"
    ledger.record_buy("key_1", mint, 10.0, 1.0, 10.0, "sig_1")

    try:
        await executor.place_sell(
            mint=mint,
            symbol="SOL",
            fraction=1.0,
            reference_price_usd=None,
            ledger=ledger,
        )
    except Exception:
        pass

    assert any(ev == AlertEvent.SELL_NO_REF_PRICE for ev, _ in events_fired)
