"""Offline operation identity, decimals and credential transport regressions."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from live_execution import config as live_config
from live_execution import jupiter_executor as je
from live_execution import wallet
from live_execution.confirmation_queue import ConfirmationQueue, ConfirmationError
from live_execution.trade_intent import canonical_operation, valid_decimals
from data_providers import stealth_browser as sb
from data_providers.jupiter import raw_units_for_one_token


def operation(**changes):
    fields = dict(side="buy", wallet="dummy-wallet", input_mint="dummy-usdc",
                  output_mint="dummy-token", in_amount_raw=10_000_000,
                  decimals=0, slippage_bps=50, min_out_raw=100)
    fields.update(changes)
    return canonical_operation(**fields)


@pytest.mark.parametrize("field,value", [
    ("side", "sell"), ("wallet", "other-wallet"),
    ("input_mint", "other-usdc"), ("output_mint", "other-token"),
    ("in_amount_raw", 1), ("decimals", 9), ("slippage_bps", 51),
    ("min_out_raw", 99), ("min_out_raw", None),
])
def test_operation_mismatch_does_not_consume(tmp_path, field, value):
    queue = ConfirmationQueue(tmp_path / "approvals.json", now_fn=lambda: 1.)
    proposal = queue.propose("dummy-token", 0, 10, operation=operation())
    queue.approve(proposal.id)
    with pytest.raises(ConfirmationError):
        queue.consume(proposal.id, operation=operation(**{field: value}))
    assert queue._get(proposal.id).status == "approved"
    assert queue.consume(proposal.id, operation=operation()).status == "consumed"


def test_stricter_bounds_and_legacy_approval(tmp_path):
    queue = ConfirmationQueue(tmp_path / "approvals.json", now_fn=lambda: 1.)
    proposal = queue.propose("dummy-token", 0, 10, operation=operation())
    queue.approve(proposal.id)
    queue.consume(proposal.id, operation=operation(slippage_bps=10, min_out_raw=101))
    legacy = queue.propose("dummy-token", 0, 10)
    queue.approve(legacy.id)
    with pytest.raises(ConfirmationError, match="unbound"):
        queue.consume(legacy.id, operation=operation())
    assert queue._get(legacy.id).status == "approved"


@pytest.mark.parametrize("value", [True, False, -1, 256, 6.0, "6", None])
def test_decimal_domain(value):
    assert not valid_decimals(value)
    with pytest.raises((ValueError, je.Refusal)):
        je.assert_decimals_known(value, "dummy-token")
    with pytest.raises(ValueError):
        raw_units_for_one_token(value)


def test_zero_and_max_native_decimals():
    assert valid_decimals(0) and valid_decimals(255)
    assert raw_units_for_one_token(0) == 1


def ready_legacy(monkeypatch):
    monkeypatch.setattr(je, "preflight", lambda *a: 6)
    monkeypatch.setattr(live_config, "REQUIRE_MANUAL_CONFIRMATION", False)
    monkeypatch.setattr(je.wallet, "load_keypair", lambda: SimpleNamespace(pubkey=lambda: "dummy-wallet"))
    return SimpleNamespace(get_by_idempotency_key=lambda key: None)


async def test_legacy_wrong_wallet_refuses_before_chain_or_quote(monkeypatch):
    ledger = ready_legacy(monkeypatch)
    monkeypatch.setattr(live_config, "EXPECTED_WALLET_ADDRESS", "other-wallet")
    chain = AsyncMock(side_effect=AssertionError("must not query chain"))
    monkeypatch.setattr(je.solana, "get_mint_decimals", chain)
    with pytest.raises(wallet.WalletError, match="expected wallet"):
        await je.execute_confirmed_trade("dummy-token", 6, 10,
                                         idempotency_key="dummy-intent", ledger=ledger,
                                         reference_price_usd=1.0)
    chain.assert_not_awaited()


@pytest.mark.parametrize("chain_decimals", [None, True, 256, 9])
async def test_legacy_decimals_disagreement_prevents_quote(monkeypatch, chain_decimals):
    ledger = ready_legacy(monkeypatch)
    monkeypatch.setattr(live_config, "EXPECTED_WALLET_ADDRESS", "dummy-wallet")
    monkeypatch.setattr(je.solana, "get_mint_decimals", AsyncMock(return_value=chain_decimals))
    quote = AsyncMock(side_effect=AssertionError("must not quote"))
    monkeypatch.setattr(je, "get_jupiter_quote", quote)
    with pytest.raises(je.Refusal, match="decimals"):
        await je.execute_confirmed_trade("dummy-token", 6, 10,
                                         idempotency_key="dummy-intent", ledger=ledger,
                                         reference_price_usd=1.0)
    quote.assert_not_awaited()


def test_credential_destination_policy(monkeypatch):
    monkeypatch.setattr(sb.config, "FOMO_API_BASE", "https://feed.invalid")
    headers = {"Authorization": "Bearer dummy"}
    assert sb._credential_destination_allowed("https://feed.invalid/a", headers)
    for url in ("https://other.invalid/a", "http://feed.invalid/a", "https://feed.invalid:444/a", "https://user@feed.invalid/a"):
        assert not sb._credential_destination_allowed(url, headers)


async def test_browser_credentials_scoped_and_redirect_refused():
    page = SimpleNamespace(route=AsyncMock())
    headers = {"authorization": "Bearer dummy"}
    await sb._scoped_browser_setup(page, "https://feed.invalid/a", headers)
    callback = page.route.await_args.args[1]
    route = SimpleNamespace(request=SimpleNamespace(url="https://feed.invalid/a"),
                            fetch=AsyncMock(return_value=SimpleNamespace(status=200)),
                            fulfill=AsyncMock(), abort=AsyncMock())
    await callback(route)
    route.fetch.assert_awaited_once_with(headers=headers, max_redirects=0)
    route.fulfill.assert_awaited_once()
    route.fetch.reset_mock();route.fulfill.reset_mock();route.abort.reset_mock()
    route.request.url = "https://other.invalid/a"
    await callback(route)
    route.abort.assert_awaited_once();route.fetch.assert_not_awaited()
    route.request.url = "https://feed.invalid/a"
    route.fetch.return_value = SimpleNamespace(status=302)
    await callback(route)
    assert route.abort.await_count == 2
    route.fulfill.assert_not_awaited()


async def test_curl_credential_redirects_disabled(monkeypatch):
    import sys
    monkeypatch.setattr(sb.config, "FOMO_API_BASE", "https://feed.invalid")
    fetch = AsyncMock(return_value=SimpleNamespace(status=302, body=b"", headers={"location": "https://other.invalid"}))
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", SimpleNamespace(AsyncFetcher=SimpleNamespace(get=fetch)))
    assert await sb.curl_get("https://feed.invalid/a", {"Authorization": "Bearer dummy"}) is None
    assert fetch.await_args.kwargs["follow_redirects"] is False
    fetch.reset_mock()
    assert await sb.curl_get("https://other.invalid/a", {"Authorization": "Bearer dummy"}) is None
    fetch.assert_not_awaited()


@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_enabled_executor_approval_mismatch_before_memo(monkeypatch, tmp_path, side):
    from live_execution import executor as ex
    from live_execution.models import ExecutionLedger
    monkeypatch.setattr(live_config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(live_config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(live_config, "REQUIRE_MANUAL_CONFIRMATION", True)
    monkeypatch.setattr(ex, "preflight", lambda *a: 6)
    monkeypatch.setattr(ex.kill_switch, "assert_not_tripped", lambda **kw: None)
    monkeypatch.setattr(ex.wallet, "load_keypair", lambda: SimpleNamespace(pubkey=lambda: "dummy-wallet"))
    monkeypatch.setattr(live_config, "EXPECTED_WALLET_ADDRESS", "dummy-wallet")
    monkeypatch.setattr(ex.solana, "get_mint_decimals", AsyncMock(return_value=6))
    monkeypatch.setattr(ex.solana, "get_sol_balance", AsyncMock(return_value=1.))
    monkeypatch.setattr(ex.solana, "get_usdc_balance", AsyncMock(return_value=100.))
    memo = AsyncMock(side_effect=AssertionError("mismatched approval reached memo"))
    monkeypatch.setattr(ex.memo, "publish_commit_memo", memo)
    queue = ConfirmationQueue(tmp_path / "approvals.json")
    approved = operation(side=side, wallet="other-wallet", decimals=6)
    pc = queue.propose("dummy-token", 6, 10., operation=approved)
    queue.approve(pc.id)
    ledger = ExecutionLedger(tmp_path / "ledger.json")
    if side == "buy":
        result = await ex.place_buy("dummy-token", "DUMMY", 10., 6,
            idempotency_key="dummy-intent", confirmation_id=pc.id, queue=queue,
            ledger=ledger, reference_price_usd=1.)
    else:
        ledger.record_buy("dummy-buy", "dummy-token", 10., 90., 1., "dummy-signature")
        result = await ex.place_sell("dummy-token", "DUMMY", 1/3,
            confirmation_id=pc.id, queue=queue, ledger=ledger, reference_price_usd=1.)
    assert result.status == "blocked"
    assert "approval operation mismatch" in result.reason
    assert queue._get(pc.id).status == "approved"
    memo.assert_not_awaited()


def test_confirmation_list_displays_exact_operation(tmp_path, monkeypatch, capsys):
    from live_execution.scripts import confirm_trade
    queue = ConfirmationQueue(tmp_path / "approvals.json")
    queue.propose("dummy-token", 0, 10., operation=operation())
    monkeypatch.setattr(confirm_trade, "_queue", lambda: queue)
    assert confirm_trade.cmd_list(None) == 0
    output = capsys.readouterr().out
    for expected in ('"wallet": "dummy-wallet"', '"in_amount_raw": 10000000',
                     '"side": "buy"', '"decimals": 0', '"min_out_raw": 100'):
        assert expected in output
