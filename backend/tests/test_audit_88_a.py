"""
§88 Phase A — an unresolved pending buy must be invisible to nothing.

A pending buy is not an open position (no tokens yet), but while its
transaction is still landable it owns the mint/slot/exposure. Before this
fix the gate and preflight both allowed a second buy of the same mint with
a fresh per-minute idempotency key — the exact double-buy the pending
journal exists to prevent.

Covers:
  (a) pending M -> the live gate's `already_held` FAILS for M,
  (b) preflight refuses a second M buy,
  (c) after `settle_pending_buy(..., "expired")` M is buyable again,
  (d) after adoption M is in `meta` and is a real managed position,
  plus the executor's independent pre-replay guard (defence in depth).
"""
from __future__ import annotations

import asyncio
import types

import pytest

import live_execution.config as le_config
import run_live_cycle as rlc
from live_execution import executor as ex
from live_execution import jupiter_executor as je
from live_execution.models import ExecutionLedger
from rule_engine.rules import already_held

MINT = "MintPending88AAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _pending(ledger: ExecutionLedger, key: str = "k-pending") -> None:
    ledger.record_pending_buy(
        idempotency_key=key,
        mint=MINT,
        usd_size=2.5,
        tokens_out=10.0,
        price_usd=0.25,
        signature="PENDINGSIG",
        blockhash="BH1",
    )


@pytest.fixture
def portfolio_env(tmp_path, monkeypatch):
    monkeypatch.setattr(rlc.live_config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(rlc, "_unresolved_qty_alerted", set())
    monkeypatch.setattr(rlc.wallet, "load_keypair", lambda: object())
    monkeypatch.setattr(rlc.wallet, "pubkey_string", lambda p: "OWNER")

    async def usdc(addr):
        return 100.0

    async def sol(addr):
        return 1.0

    async def reconcile(meta, ledger=None):
        return {"checked": True, "discrepancies": []}

    monkeypatch.setattr(rlc.solana, "get_usdc_balance", usdc)
    monkeypatch.setattr(rlc.solana, "get_sol_balance", sol)
    monkeypatch.setattr(rlc, "_reconcile_with_chain", reconcile)
    monkeypatch.setattr("alerts.alert_nowait", lambda *a, **k: None)
    return ExecutionLedger(tmp_path / "executions.json")


@pytest.fixture
def armed_env(tmp_path, monkeypatch):
    monkeypatch.setattr(le_config, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(le_config, "STATE_DIR", tmp_path)
    return ExecutionLedger(tmp_path / "executions.json")


async def test_a_pending_mint_fails_the_gate_already_held(portfolio_env):
    ledger = portfolio_env
    _pending(ledger)

    portfolio, meta, _ = await rlc._live_portfolio(ledger)

    # Not a managed position: no token basis reaches the exit engine.
    assert MINT not in meta
    pending = [t for t in portfolio.open_positions if t.mint_address == MINT]
    assert len(pending) == 1
    assert pending[0].quantity == 0.0
    assert pending[0].position_size_usd == pytest.approx(2.5)
    assert pending[0].thesis == "pending buy"

    candidate = types.SimpleNamespace(symbol="PEND", mint_address=MINT)
    result = already_held(candidate, portfolio, None)

    assert result.passed is False
    assert result.value == pytest.approx(2.5)
    assert "already" in result.detail


def test_b_preflight_refuses_a_second_buy_while_pending(armed_env):
    ledger = armed_env
    _pending(ledger)

    with pytest.raises(je.Refusal, match="pending buy"):
        je.preflight(1.0, MINT, 6, ledger)

    # A different mint is unaffected.
    assert je.preflight(1.0, "OtherMint88", 6, ledger) == 6


def test_c_expired_pending_releases_the_mint_for_a_new_buy(armed_env):
    ledger = armed_env
    _pending(ledger)
    assert ledger.settle_pending_buy("k-pending", "expired") is True

    assert je.preflight(1.0, MINT, 6, ledger) == 6


async def test_d_adopted_pending_becomes_a_managed_position(portfolio_env):
    ledger = portfolio_env
    _pending(ledger)
    assert ledger.settle_pending_buy(
        "k-pending", "confirmed", usd_size=2.7, tokens_out=9.0,
        price_usd=2.7 / 9.0) is True

    portfolio, meta, _ = await rlc._live_portfolio(ledger)

    assert MINT in meta
    assert meta[MINT]["tokens"] == pytest.approx(9.0)
    managed = [t for t in portfolio.open_positions if t.mint_address == MINT]
    assert len(managed) == 1
    assert managed[0].quantity == pytest.approx(9.0)
    assert managed[0].thesis == "live book"


def test_executor_place_buy_has_its_own_pre_replay_pending_guard(
        armed_env, monkeypatch):
    """Defence in depth: even if the shared preflight is bypassed, place_buy
    must refuse before the idempotency replay can return/start a send."""
    ledger = armed_env
    _pending(ledger)
    monkeypatch.setattr(ex, "preflight", lambda *a, **k: 6)
    monkeypatch.setattr(ex.kill_switch, "assert_not_tripped", lambda *a, **k: None)

    result = asyncio.run(ex.place_buy(
        MINT, "PEND", 1.0, output_decimals=6,
        idempotency_key="fresh-key", ledger=ledger,
        reference_price_usd=1.0,
    ))

    assert result.status == "blocked"
    assert "pending buy" in result.reason
    # No new row was created by the refused attempt.
    assert len(ledger.pending_buys()) == 1
