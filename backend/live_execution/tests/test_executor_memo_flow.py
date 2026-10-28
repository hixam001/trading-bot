"""
tests for the REF-R11 armed order flow in live_execution.executor.

The single most important property under test: the commit memo is published
and CONFIRMED before the fill is ever quoted/built/broadcast, and a memo that
cannot be confirmed blocks the fill entirely (the fill send is never
attempted). Also covers the micro-bootstrap funding guards (SOL reserve floor,
real USDC balance) refusing BEFORE any on-chain commitment is made.

Everything is hermetic: armed flags via monkeypatch, tmp state dir, mocked
wallet + RPC + memo + quote. No network.
"""
from __future__ import annotations

import base64

import pytest

import live_execution.config as le_config
from live_execution import executor as ex
from live_execution import memo as memo_mod
from live_execution.models import ExecutionLedger


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Armed-but-hermetic: enabled, no manual confirm, tmp state, stub wallet."""
    monkeypatch.setattr(le_config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(le_config, "REQUIRE_MANUAL_CONFIRMATION", False)
    monkeypatch.setattr(le_config, "STATE_DIR", tmp_path)

    class _Payer:
        pass

    payer = _Payer()
    monkeypatch.setattr(ex.wallet, "load_keypair", lambda: payer)
    monkeypatch.setattr(ex.wallet, "verify_expected_address", lambda p: None)
    monkeypatch.setattr(ex.wallet, "pubkey_string", lambda p: "WALLETADDR")
    return tmp_path


def _arm_balances(monkeypatch, sol=1.0, usdc=100.0):
    async def fake_sol(addr):
        return sol

    async def fake_usdc(addr):
        return usdc

    monkeypatch.setattr(ex.solana, "get_sol_balance", fake_sol)
    monkeypatch.setattr(ex.solana, "get_usdc_balance", fake_usdc)


def _arm_fill(monkeypatch, order):
    """Mock memo + quote + build + sign + send + confirm, recording order."""

    async def fake_memo(payer, seal_hash, endpoints=None):
        order.append("memo")
        return {"signature": "MEMOSIG", "slot": 5}

    async def fake_quote(mint, decimals, usd, ledger=None):
        order.append("quote")
        return {"quote": {"priceImpactPct": "0.001"},
                "tokens_out": 1.0, "price_usd": usd}

    async def fake_build(quote, addr):
        order.append("build")
        return base64.b64encode(b"swap").decode()

    async def fake_sign(b64, payer, intent=None, quote=None):
        order.append("sign")
        return ex.solana.SignedSwap("FILLSIG", b"raw", _checked=True)

    async def fake_send(signed, endpoints=None):
        order.append("send")
        return "FILLSIG"

    async def fake_confirm(sig, timeout_s=None, endpoints=None):
        order.append("confirm")
        return {"confirmed": True, "slot": 10, "err": None}

    monkeypatch.setattr(ex.memo, "publish_commit_memo", fake_memo)
    monkeypatch.setattr(ex, "get_jupiter_quote", fake_quote)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send)
    monkeypatch.setattr(ex.solana, "confirm_signature", fake_confirm)


async def test_happy_path_memo_before_fill(env, monkeypatch):
    order = []
    _arm_balances(monkeypatch)
    _arm_fill(monkeypatch, order)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k1", ledger=ledger, reference_price_usd=1.0)

    assert res.status == "filled"
    # The memo is published BEFORE the quote/build/sign/send/confirm.
    assert order == ["memo", "quote", "build", "sign", "send", "confirm"]
    # The result carries the seal + memo so the bridge can journal them.
    assert res.commit_hash != ""
    assert res.commit_nonce != ""
    assert res.commit_payload["kind"] == "buy"
    assert res.memo_signature == "MEMOSIG"
    assert res.memo_slot == 5
    assert res.signature == "FILLSIG"


async def test_ambiguous_send_resolves_landed_fill_and_journals_it(env, monkeypatch):
    """A send that returns None ("already processed"/transport ambiguity) must
    be resolved by signature status, not reported as a refusal. Otherwise the
    buy lands on-chain but is never journaled and a retry can double-buy."""
    order = []
    _arm_balances(monkeypatch)

    async def fake_memo(payer, seal_hash, endpoints=None):
        order.append("memo")
        return {"signature": "MEMOSIG", "slot": 5}

    async def fake_quote(mint, decimals, usd, ledger=None):
        order.append("quote")
        return {"quote": {"priceImpactPct": "0.001"},
                "tokens_out": 1.0, "price_usd": usd}

    async def fake_build(quote, addr):
        order.append("build")
        return base64.b64encode(b"swap").decode()

    async def fake_sign(b64, payer, intent=None, quote=None):
        order.append("sign")
        return ex.solana.SignedSwap("FILLSIG", b"raw", _checked=True)

    async def fake_send(signed, endpoints=None):
        order.append("send")
        return None

    async def fake_status(sig, endpoints=None):
        order.append("status")
        return {"slot": 10, "err": None, "confirmationStatus": "confirmed"}

    async def no_confirm(sig, timeout_s=None, endpoints=None):
        raise AssertionError("already-confirmed status must not re-poll")

    monkeypatch.setattr(ex.memo, "publish_commit_memo", fake_memo)
    monkeypatch.setattr(ex, "get_jupiter_quote", fake_quote)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_status)
    monkeypatch.setattr(ex.solana, "confirm_signature", no_confirm)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k-landed", ledger=ledger,
                             reference_price_usd=1.0)

    assert res.status == "filled"
    assert res.signature == "FILLSIG"
    assert order == ["memo", "quote", "build", "sign", "send", "status"]
    records = ledger._load()
    assert len(records) == 1
    assert records[0]["signature"] == "FILLSIG"


async def test_ambiguous_send_with_unknown_signature_stays_unresolved(env, monkeypatch):
    """No signature on-chain means we cannot prove a fill; fail closed and do
    not journal a phantom position."""
    order = []
    _arm_balances(monkeypatch)

    async def fake_memo(payer, seal_hash, endpoints=None):
        return {"signature": "MEMOSIG", "slot": 5}

    async def fake_quote(mint, decimals, usd, ledger=None):
        return {"quote": {"priceImpactPct": "0.001"},
                "tokens_out": 1.0, "price_usd": usd}

    async def fake_build(quote, addr):
        return base64.b64encode(b"swap").decode()

    async def fake_sign(b64, payer, intent=None, quote=None):
        return ex.solana.SignedSwap("FILLSIG", b"raw", _checked=True)

    async def fake_send(signed, endpoints=None):
        return None

    async def fake_status(sig, endpoints=None):
        return None

    monkeypatch.setattr(ex.memo, "publish_commit_memo", fake_memo)
    monkeypatch.setattr(ex, "get_jupiter_quote", fake_quote)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send)
    monkeypatch.setattr(ex.solana, "get_signature_status", fake_status)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k-unknown", ledger=ledger,
                             reference_price_usd=1.0)

    assert res.status == "failed"
    assert "unresolved" in res.reason
    assert ledger._load() == []


async def test_memo_failure_blocks_fill_entirely(env, monkeypatch):
    order = []
    _arm_balances(monkeypatch)

    async def failing_memo(payer, seal_hash, endpoints=None):
        order.append("memo")
        raise memo_mod.MemoPublishError("rpc down")

    def boom(*a, **k):
        raise AssertionError("fill path ran despite memo failure")

    monkeypatch.setattr(ex.memo, "publish_commit_memo", failing_memo)
    monkeypatch.setattr(ex, "get_jupiter_quote", boom)
    monkeypatch.setattr(ex, "_build_swap_transaction", boom)
    monkeypatch.setattr(ex, "sign_after_checks", boom)
    monkeypatch.setattr(ex.solana, "send_checked_swap", boom)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k2", ledger=ledger, reference_price_usd=1.0)

    assert res.status == "failed"
    assert "commit memo failed" in res.reason
    assert order == ["memo"]          # nothing after the memo ran
    # The seal is still recorded (honest refusal), carrying its hash.
    assert res.commit_hash != ""


async def test_buy_without_idempotency_key_refuses_before_anything(env, monkeypatch):
    """A7: the old time-based fallback key was NOT idempotent — a retry
    after an unclear failure generated a fresh key, bypassed the replay
    check, and could double-send. Refuse instead of fabricating."""
    async def boom(*a, **k):
        raise AssertionError("order progressed without an idempotency key")

    monkeypatch.setattr(ex.memo, "publish_commit_memo", boom)
    monkeypatch.setattr(ex, "get_jupiter_quote", boom)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6, ledger=ledger)

    assert res.status == "blocked"
    assert "idempotency_key is required" in res.reason
    assert ledger._load() == []       # nothing was ever journaled


async def test_insufficient_usdc_blocks_before_memo(env, monkeypatch):
    _arm_balances(monkeypatch, sol=1.0, usdc=0.5)   # below the 1.5 ticket

    def boom(*a, **k):
        raise AssertionError("memo/fill ran despite insufficient USDC")

    monkeypatch.setattr(ex.memo, "publish_commit_memo", boom)
    monkeypatch.setattr(ex, "get_jupiter_quote", boom)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k3", ledger=ledger)
    assert res.status == "blocked"
    assert "insufficient USDC" in res.reason


async def test_unreadable_usdc_blocks_before_memo(env, monkeypatch):
    _arm_balances(monkeypatch, sol=1.0, usdc=None)  # None = unreadable

    def boom(*a, **k):
        raise AssertionError("memo ran despite unreadable USDC balance")

    monkeypatch.setattr(ex.memo, "publish_commit_memo", boom)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k4", ledger=ledger)
    assert res.status == "blocked"
    assert "USDC balance unreadable" in res.reason


async def test_sol_reserve_floor_blocks_before_usdc(env, monkeypatch):
    # SOL below the (0.01) floor -> refuse before even reading USDC.
    _arm_balances(monkeypatch, sol=0.005, usdc=100.0)

    async def boom_usdc(addr):
        raise AssertionError("USDC read despite SOL reserve breach")

    monkeypatch.setattr(ex.solana, "get_usdc_balance", boom_usdc)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k5", ledger=ledger)
    assert res.status == "blocked"
    assert "SOL reserve" in res.reason


async def test_disarmed_refuses_before_anything(env, monkeypatch):
    monkeypatch.setattr(le_config, "LIVE_TRADING_ENABLED", False)

    def boom(*a, **k):
        raise AssertionError("network/path ran while disarmed")

    monkeypatch.setattr(ex.solana, "get_sol_balance", boom)
    ledger = ExecutionLedger(env / "exec.json")
    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k6", ledger=ledger)
    assert res.status == "unarmed"


# --- regressions: the two bugs that blocked ALL live executions (2026-08-28) --
# 1) ExecutionError was caught in four except-clauses but never imported, so
#    the FIRST quote failure crashed the whole cycle with NameError instead of
#    returning status="failed".
# 2) the sell path quoted via POST; Jupiter's /swap/v1/quote is GET (POST 405).

def _arm_sell_network(monkeypatch, calls):
    """Mock memo + GET-quote + build + sign + send + confirm for the SELL path."""

    async def fake_memo(payer, seal_hash, endpoints=None):
        calls.append("memo")
        return {"signature": "MEMOSIG", "slot": 5}

    async def fake_get(url, params):
        calls.append({"verb": "get", "url": url, "params": params})
        if url.endswith("/quote"):
            # selling 1.0 token (6-dec mint) -> $1.20 USDC out
            return {"outAmount": str(1_200_000), "priceImpactPct": "0.001"}
        raise AssertionError(f"unexpected GET {url}")

    async def fake_build(quote, addr):
        calls.append("build")
        return base64.b64encode(b"swap").decode()

    async def fake_sign(b64, payer, intent=None, quote=None):
        calls.append("sign")
        return ex.solana.SignedSwap("FILLSIG", b"raw", _checked=True)

    async def fake_send(signed, endpoints=None):
        calls.append("send")
        return "FILLSIG"

    async def fake_confirm(sig, timeout_s=None, endpoints=None):
        calls.append("confirm")
        return {"confirmed": True, "slot": 11, "err": None}

    async def fake_decimals(mint):
        return 6

    monkeypatch.setattr(ex.memo, "publish_commit_memo", fake_memo)
    monkeypatch.setattr(ex, "_get_json", fake_get)
    monkeypatch.setattr(ex, "_build_swap_transaction", fake_build)
    monkeypatch.setattr(ex, "sign_after_checks", fake_sign)
    monkeypatch.setattr(ex.solana, "send_checked_swap", fake_send)
    monkeypatch.setattr(ex.solana, "confirm_signature", fake_confirm)
    monkeypatch.setattr(ex.solana, "get_mint_decimals", fake_decimals)


def _ledger_with_open_position(env, key="kopen"):
    ledger = ExecutionLedger(env / "exec.json")
    ledger.record_buy(idempotency_key=key, mint="MINT", usd_size=1.0,
                      tokens_out=1.0, price_usd=1.0, signature="OPENSIG",
                      status="confirmed")
    return ledger


@pytest.mark.parametrize("fraction", [float("nan"), float("inf"), float("-inf")])
async def test_sell_rejects_non_finite_fraction(env, fraction):
    """§86: inf silently clamped to a full close; NaN crashed in int(). Both
    are ambiguous operator intent and must block before any network call."""
    ledger = _ledger_with_open_position(env)
    res = await ex.place_sell("MINT", "SYM", fraction, ledger=ledger)
    assert res.status == "blocked"
    assert "fraction" in res.reason


async def test_sell_non_finite_reference_price_does_not_crash(env, monkeypatch):
    """A bad reference price degrades to the existing no-reference path (the
    sell still self-consistency-checks the quote) instead of OverflowError."""
    calls = []
    _arm_balances(monkeypatch)
    _arm_sell_network(monkeypatch, calls)
    ledger = _ledger_with_open_position(env)

    res = await ex.place_sell("MINT", "SYM", 1.0, ledger=ledger,
                              reference_price_usd=float("inf"))

    assert res.status == "filled"
    assert res.reason in (None, "")


async def test_buy_non_finite_reference_price_blocks_before_memo(env, monkeypatch):
    order = []
    _arm_balances(monkeypatch)
    _arm_fill(monkeypatch, order)

    res = await ex.place_buy(
        "MINT", "SYM", 1.5, output_decimals=6,
        idempotency_key="k-bad-ref", ledger=ExecutionLedger(env / "exec.json"),
        reference_price_usd=float("nan"),
    )

    assert res.status == "blocked"
    assert "reference price" in res.reason
    assert order == []                      # no memo, quote, sign, or send


async def test_buy_quote_failure_returns_failed_not_nameerror(env, monkeypatch):
    """A quote-phase ExecutionError must yield status='failed' — NOT crash the
    cycle with NameError (ExecutionError was used but never imported)."""
    order = []
    _arm_balances(monkeypatch)
    _arm_fill(monkeypatch, order)

    async def boom_quote(mint, decimals, usd, ledger=None):
        raise ex.ExecutionError("giving up on quote after 3 attempts")

    monkeypatch.setattr(ex, "get_jupiter_quote", boom_quote)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="kq1", ledger=ledger, reference_price_usd=1.0)

    assert res.status == "failed"
    assert "giving up" in res.reason
    assert res.memo_signature == "MEMOSIG"   # memo went out; fill did not
    assert "send" not in order


async def test_sell_quote_uses_get_and_full_flow_fills(env, monkeypatch):
    """The sell quote must go out as GET with query params (POST -> 405 was
    the second execution blocker), and the full sell flow fills + closes the
    ledger position."""
    calls = []
    _arm_balances(monkeypatch)
    _arm_sell_network(monkeypatch, calls)
    ledger = _ledger_with_open_position(env)

    res = await ex.place_sell("MINT", "SYM", 1.0, ledger=ledger)

    assert res.status == "filled"
    quote_calls = [c for c in calls
                   if isinstance(c, dict) and c["verb"] == "get"]
    assert len(quote_calls) == 1
    assert quote_calls[0]["url"].endswith("/swap/v1/quote")
    assert quote_calls[0]["params"]["inputMint"] == "MINT"
    assert quote_calls[0]["params"]["outputMint"] == ex._BACKEND_USDC_MINT
    assert res.usd_value == pytest.approx(1.20)
    assert ledger.open_token_amounts().get("MINT", 0.0) == 0.0


async def test_sell_quote_failure_returns_failed_not_nameerror(env, monkeypatch):
    """Sell-side twin of the buy regression: quote failure -> status='failed'."""
    calls = []
    _arm_balances(monkeypatch)
    _arm_sell_network(monkeypatch, calls)

    async def boom_get(url, params):
        raise ex.ExecutionError("giving up on quote after 3 attempts")

    monkeypatch.setattr(ex, "_get_json", boom_get)
    ledger = _ledger_with_open_position(env, key="kopen2")

    res = await ex.place_sell("MINT", "SYM", 1.0, ledger=ledger)

    assert res.status == "failed"
    assert "quote" in res.reason
    assert "send" not in calls


async def test_corrupt_state_during_preflight_blocks_not_raises(env, monkeypatch):
    def broken_preflight(*a, **k):
        raise RuntimeError("kill switch state is corrupt")

    monkeypatch.setattr(ex, "preflight", broken_preflight)
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k-corrupt", ledger=ledger,
                             reference_price_usd=1.0)

    assert res.status == "blocked"
    assert "corrupt" in res.reason


async def test_corrupt_commit_log_blocks_buy_before_memo(env, monkeypatch):
    """A corrupt commits.json must refuse the buy before the memo, quote,
    signature, or broadcast — never raise out of place_buy."""
    order = []
    _arm_balances(monkeypatch)
    _arm_fill(monkeypatch, order)
    corrupt = env / "commits.json"
    corrupt.write_text("{not-json")
    ledger = ExecutionLedger(env / "exec.json")

    res = await ex.place_buy("MINT", "SYM", 1.5, output_decimals=6,
                             idempotency_key="k-corrupt-log", ledger=ledger,
                             reference_price_usd=1.0)

    assert res.status == "blocked"
    assert "commit log" in res.reason
    assert order == []
    assert corrupt.read_text() == "{not-json"


async def test_corrupt_commit_log_blocks_sell_before_memo(env, monkeypatch):
    """Sell-side twin: no memo/sign/send when the commit log cannot be read."""
    calls = []
    _arm_balances(monkeypatch)
    _arm_sell_network(monkeypatch, calls)
    corrupt = env / "commits.json"
    corrupt.write_text("{not-json")
    ledger = _ledger_with_open_position(env, key="k-corrupt-sell-log")

    res = await ex.place_sell("MINT", "SYM", 1.0, ledger=ledger)

    assert res.status == "blocked"
    assert "commit log" in res.reason
    assert calls == []
    assert corrupt.read_text() == "{not-json"
