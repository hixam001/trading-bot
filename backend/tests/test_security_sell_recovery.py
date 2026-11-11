"""A3: real orchestration/ledger, dummy execution boundary; no transaction send."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from live_execution import executor as ex, solana
from live_execution.commit_log import CommitLog
from live_execution.models import ExecutionLedger
from live_execution.trade_intent import TradeIntent

MINT = "So11111111111111111111111111111111111111112"
SUCCESS = {"confirmationStatus": "confirmed", "err": None, "slot": 5}
ERROR = {"InstructionError": [1, {"Custom": 6001}]}
FAILURE = {"confirmationStatus": "finalized", "err": ERROR, "slot": 5}


@pytest.fixture
def sell_env(tmp_path, monkeypatch):
    monkeypatch.setattr(ex.config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(ex.config, "REQUIRE_MANUAL_CONFIRMATION", False)
    monkeypatch.setattr(ex.config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(ex.kill_switch, "assert_exit_allowed", lambda: None)
    monkeypatch.setattr(ex.wallet, "load_keypair", lambda: object())
    monkeypatch.setattr(ex.wallet, "verify_expected_address", lambda payer: None)
    monkeypatch.setattr(ex.wallet, "pubkey_string", lambda payer: "dummy-wallet")
    monkeypatch.setattr(solana, "get_mint_decimals", AsyncMock(return_value=6))
    monkeypatch.setattr(solana, "get_sol_balance", AsyncMock(return_value=1.0))
    monkeypatch.setattr(solana, "is_blockhash_valid", AsyncMock(return_value=False))
    monkeypatch.setattr(ex.memo, "publish_commit_memo", AsyncMock(return_value={"signature": "memo", "slot": 1}))
    monkeypatch.setattr(ex, "_get_json", AsyncMock(return_value={"outAmount": "30000000", "priceImpactPct": "0.001"}))
    monkeypatch.setattr(ex, "_build_swap_transaction", AsyncMock(return_value="AA=="))
    monkeypatch.setattr(ex, "_fill_actuals", AsyncMock(return_value={"usdc_delta": 30.0, "tokens_delta": 30.0}))
    ledger = ExecutionLedger(tmp_path / "ledger.json")
    ledger.record_buy("buy", MINT, 90, 90, 1, "buy-signature")
    return ledger, CommitLog(tmp_path / "commits.json")


@pytest.mark.asyncio
async def test_partial_sell_absence_then_success_authorizes_once(sell_env, monkeypatch):
    ledger, commits = sell_env
    boundary = AsyncMock(return_value=ex.OrderResult(status="failed", signature="prior-signature",
                                                    reason="not confirmed before timeout", unresolved=True))
    monkeypatch.setattr(ex, "_broadcast_and_confirm", boundary)
    monkeypatch.setattr(solana, "get_signature_status_all_strict", AsyncMock(return_value=[solana.ABSENT, SUCCESS]))
    result = await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ledger)
    assert result.status == "filled"
    assert result.signature == "prior-signature"
    assert boundary.await_count == 1
    assert boundary.call_args.kwargs["intent"].in_amount_raw == 30_000_000
    assert ledger.open_token_amounts()[MINT] == pytest.approx(60)
    assert len([r for r in ledger._load() if r["kind"] == "close"]) == 1
    assert not commits.pending_sells(MINT)
    solana.is_blockhash_valid.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("observations", [
    [], [solana.ABSENT, solana.ABSENT], [solana.ABSENT, solana.UNREACHABLE],
    [solana.UNREACHABLE, solana.UNREACHABLE], [{}, {}],
    [SUCCESS, FAILURE], [FAILURE, solana.ABSENT],
    [{"confirmationStatus": "processed", "err": None}, solana.ABSENT],
    [{"confirmationStatus": "confirmed"}, solana.ABSENT],
    [{"confirmationStatus": "confirmed", "err": None}, solana.ABSENT],
    [{"confirmationStatus": "confirmed", "err": None, "slot": True}, solana.ABSENT],
    [{"confirmationStatus": "confirmed", "err": False, "slot": 5}, solana.ABSENT],
    [{"confirmationStatus": "confirmed", "err": {"UnknownError": True}, "slot": 5}] * 2,
    [{"confirmationStatus": "confirmed", "err": {"InstructionError": []}, "slot": 5}] * 2,
    [{"confirmationStatus": "confirmed", "err": None, "slot": 5, "status": {"Err": "AccountNotFound"}}] * 2,
])
async def test_unknown_or_conflicting_exit_survives_restart(sell_env, monkeypatch, observations):
    ledger, commits = sell_env
    boundary = AsyncMock(return_value=ex.OrderResult(status="failed", signature="pending",
                                                    reason="not confirmed before timeout", unresolved=True))
    monkeypatch.setattr(ex, "_broadcast_and_confirm", boundary)
    monkeypatch.setattr(solana, "get_signature_status_all_strict", AsyncMock(return_value=observations))
    first = await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ledger)
    assert first.unresolved
    assert len(commits.pending_sells(MINT)) == 1
    # Fresh ledger/log instances model process restart. No new memo/quote/send.
    restarted = ExecutionLedger(ledger.path)
    for _ in range(2):
        result = await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=restarted)
        assert result.unresolved
    assert boundary.await_count == 1
    assert ex.memo.publish_commit_memo.await_count == 1
    assert ex._get_json.await_count == 1
    assert ledger.open_token_amounts()[MINT] == 90


@pytest.mark.asyncio
async def test_conclusive_landed_failure_can_retry_bounded(sell_env, monkeypatch):
    ledger, commits = sell_env
    boundary = AsyncMock(side_effect=[
        ex.OrderResult(status="failed", signature="failed-signature", reason="not confirmed before timeout"),
        ex.OrderResult(status="filled", signature="success-signature"),
    ])
    monkeypatch.setattr(ex, "_broadcast_and_confirm", boundary)
    monkeypatch.setattr(solana, "get_signature_status_all_strict", AsyncMock(return_value=[FAILURE, FAILURE]))
    result = await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ledger)
    assert result.status == "filled"
    assert boundary.await_count == 2
    assert [c.kwargs["intent"].slippage_bps for c in boundary.call_args_list] == [50, 150]
    assert ledger.open_token_amounts()[MINT] == pytest.approx(60)
    assert not commits.pending_sells(MINT)
    row = commits.all()[0]
    assert row["sell_attempt_history"][0]["signature"] == "failed-signature"
    assert row["sell_attempt_history"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_crash_after_reduction_recovery_does_not_reduce_twice(sell_env, monkeypatch):
    ledger, commits = sell_env
    rec = commits.seal("sell", {"mint": MINT, "symbol": "DUMMY", "fraction": 1 / 3})
    terms = dict(wallet="dummy-wallet", fraction=1 / 3, tokens=30, full_close=False,
                 rule_id="exit_take_profit", proceeds_usd=30)
    commits.record_sell_attempt(rec["hash"], "signature", "old-blockhash", terms)
    ledger.reduce_position(MINT, 1 / 3, 30, signature="signature")
    monkeypatch.setattr(solana, "get_signature_status_all_strict", AsyncMock(return_value=[SUCCESS, SUCCESS]))
    boundary = AsyncMock()
    monkeypatch.setattr(ex, "_broadcast_and_confirm", boundary)
    result = await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ExecutionLedger(ledger.path))
    assert result.status == "filled"
    assert ledger.open_token_amounts()[MINT] == pytest.approx(60)
    assert len([r for r in ledger._load() if r["kind"] == "close"]) == 1
    assert not CommitLog(commits.path).pending_sells(MINT)
    boundary.assert_not_awaited()


@pytest.mark.asyncio
async def test_signed_sell_is_durable_before_send(sell_env, monkeypatch):
    ledger, commits = sell_env
    sealed = commits.seal("sell", {"mint": MINT})
    sealed["_sell_terms"] = dict(wallet="dummy-wallet", fraction=1 / 3, tokens=30,
                                 full_close=False, rule_id=None, proceeds_usd=30)
    signed = SimpleNamespace(signature="signed-before-send", blockhash="old-blockhash")
    monkeypatch.setattr(ex, "sign_after_checks", AsyncMock(return_value=signed))

    async def send(swap):
        assert commits.pending_sells(MINT)[0]["sell_attempt"]["signature"] == swap.signature
        return None

    monkeypatch.setattr(solana, "send_checked_swap", send)
    monkeypatch.setattr(solana, "get_signature_status", AsyncMock(return_value=None))
    intent = TradeIntent(side="sell", wallet="dummy-wallet", input_mint=MINT,
                         output_mint=ex._BACKEND_USDC_MINT, in_amount_raw=30_000_000,
                         slippage_bps=50, min_out_raw=0, reference_price_usd=1.0)
    result = await ex._broadcast_and_confirm("dummy", object(), intent, logc=commits, sealed=sealed)
    assert result.unresolved
    assert commits.pending_sells(MINT)


def test_orphan_repair_preserves_unresolved_signed_exit(sell_env):
    _, commits = sell_env
    rec = commits.seal("sell", {"mint": MINT})
    commits.record_memo(rec["hash"], "memo", 1)
    commits.record_sell_attempt(rec["hash"], "signature", "old-blockhash", dict(wallet="dummy-wallet", fraction=1, tokens=90, proceeds_usd=90, full_close=True))
    commits.now_fn = lambda: 1e15
    assert commits.reconcile_orphaned() == 0
    assert commits.pending_sells(MINT)


@pytest.mark.asyncio
async def test_exception_after_signing_keeps_durable_hold(sell_env, monkeypatch):
    ledger, commits = sell_env
    signed = SimpleNamespace(signature="signed-then-timeout", blockhash="old-blockhash")
    monkeypatch.setattr(ex, "sign_after_checks", AsyncMock(return_value=signed))
    monkeypatch.setattr(solana, "send_checked_swap", AsyncMock(side_effect=TimeoutError("dummy timeout")))
    monkeypatch.setattr(solana, "get_signature_status_all_strict", AsyncMock(return_value=[solana.ABSENT, solana.UNREACHABLE]))
    result = await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ledger)
    assert result.unresolved
    assert ledger.open_token_amounts()[MINT] == 90
    assert commits.pending_sells(MINT)[0]["sell_attempt"]["signature"] == signed.signature
    solana.send_checked_swap.assert_awaited_once()
    restarted = await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ExecutionLedger(ledger.path))
    assert restarted.unresolved
    solana.send_checked_swap.assert_awaited_once()
