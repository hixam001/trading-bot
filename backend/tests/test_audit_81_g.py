"""
backend/tests/test_audit_81_g.py — Tests for §81 Phase G:
Real-world validation, dry-run guard, jupiter_client, and quote contract fail-closed.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest

import config
from live_execution import jupiter_client
from live_execution.sim_guard import QuoteMismatch, SimulationRefused, validate_quote_against_intent
from live_execution.trade_intent import TradeIntent


def test_dry_run_guard_ast_no_keypair():
    """Verify scripts/dry_run_guard.py never imports load_keypair or signing helpers."""
    script_path = config.BASE_DIR.parent / "scripts" / "dry_run_guard.py"
    assert script_path.exists(), f"Missing script: {script_path}"

    tree = ast.parse(script_path.read_text(encoding="utf-8"))
    forbidden = {"load_keypair", "sign_after_checks", "sign_transaction", "_sign_transaction"}
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in forbidden:
            violations.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in forbidden:
            violations.append(node.attr)

    assert not violations, f"dry_run_guard.py references forbidden signing functions: {violations}"


@pytest.mark.asyncio
async def test_dry_run_guard_success_and_refusal(monkeypatch, capsys):
    """
    Test dry_run_guard against faked Jupiter + RPC:
    - Returns 0 and PASSED on clean quote
    - Returns 1 and REFUSED with category on failure
    - Strictly never calls any signing function (sentinel check)
    """
    from scripts import dry_run_guard

    # Sentinel to guarantee no signing occurs
    signing_called = False

    def _poison_sign(*args, **kwargs):
        nonlocal signing_called
        signing_called = True
        raise AssertionError("Signing function was invoked in dry_run_guard!")

    # Fake responses
    clean_quote = {
        "inputMint": config.USDC_MINT,
        "outputMint": "So11111111111111111111111111111111111111112",
        "inAmount": "10000000",
        "outAmount": "66666666",
        "otherAmountThreshold": "66333332",
        "slippageBps": 50,
        "swapMode": "ExactIn",
    }

    monkeypatch.setattr(dry_run_guard.solana, "get_mint_decimals", AsyncMock(return_value=9))
    monkeypatch.setattr(dry_run_guard.wallet, "public_address", lambda: "11111111111111111111111111111111")
    monkeypatch.setattr(dry_run_guard, "_get_json", AsyncMock(return_value=clean_quote))
    monkeypatch.setattr(dry_run_guard, "_build_swap_transaction", AsyncMock(return_value="dummy_b64_tx"))
    monkeypatch.setattr(
        dry_run_guard,
        "inspect_swap_transaction",
        lambda tx, payer: {"priority_fee_lamports": 5000, "compute_unit_limit": 200000, "compute_unit_price": 25000},
    )

    class FakeProvider:
        async def get_current_price(self, mint, dec):
            return 150.0

    monkeypatch.setattr(dry_run_guard, "JupiterProvider", FakeProvider)

    sim_success = {
        "status": "passed",
        "input_decrease": 10000000,
        "output_increase": 66666666,
        "sol_decrease": 5000,
        "units": 150000,
        "agreeing_rpcs": 2,
    }
    monkeypatch.setattr(dry_run_guard, "simulate_and_assert", AsyncMock(return_value=sim_success))

    # 1. Clean run (human format)
    args = argparse.Namespace(
        side="buy",
        mint="So11111111111111111111111111111111111111112",
        usd=10.0,
        json=False,
        allow_live_state=True,
    )
    code = await dry_run_guard.async_main(args)
    assert code == 0
    out = capsys.readouterr().out
    assert "VERDICT: PASSED" in out
    assert "SOL decrease:          5000 lamports" in out
    assert not signing_called

    # 2. Clean run (JSON format)
    args_json = argparse.Namespace(
        side="buy",
        mint="So11111111111111111111111111111111111111112",
        usd=10.0,
        json=True,
        allow_live_state=True,
    )
    code = await dry_run_guard.async_main(args_json)
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == "PASSED"
    assert data["agreeing_rpc_count"] == 2
    assert not signing_called

    # 3. Refusal run (SimulationRefused)
    monkeypatch.setattr(
        dry_run_guard,
        "simulate_and_assert",
        AsyncMock(side_effect=SimulationRefused("sim balance assertion violated", category="sim_refused")),
    )
    code_refused = await dry_run_guard.async_main(args)
    assert code_refused == 1
    out_refused = capsys.readouterr().out
    assert "VERDICT: REFUSED" in out_refused
    assert "sim_refused" in out_refused
    assert not signing_called


@pytest.mark.asyncio
async def test_dry_run_guard_refuses_when_state_unisolated(monkeypatch):
    """dry_run_guard refuses if LIVE_EXECUTION_STATE_DIR is unset and default state dir exists without --allow-live-state."""
    from scripts import dry_run_guard

    monkeypatch.delenv("LIVE_EXECUTION_STATE_DIR", raising=False)
    args = argparse.Namespace(
        side="buy",
        mint="So11111111111111111111111111111111111111112",
        usd=10.0,
        json=False,
        allow_live_state=False,
    )
    code = await dry_run_guard.async_main(args)
    assert code == 2


def test_jupiter_client_urls_and_headers(monkeypatch):
    """Verify jupiter_client URL and header construction under default and custom env vars."""
    # 1. Defaults with no env
    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    monkeypatch.delenv("JUPITER_BASE_URL", raising=False)

    assert jupiter_client.get_base_url() == "https://lite-api.jup.ag/swap/v1"
    assert jupiter_client.quote_url() == "https://lite-api.jup.ag/swap/v1/quote"
    assert jupiter_client.swap_url() == "https://lite-api.jup.ag/swap/v1/swap"
    assert jupiter_client.program_id_to_label_url() == "https://lite-api.jup.ag/swap/v1/program-id-to-label"
    assert jupiter_client.get_headers() == {}

    # 2. Custom JUPITER_API_KEY
    monkeypatch.setenv("JUPITER_API_KEY", "secret-test-token")
    assert jupiter_client.get_headers() == {"x-api-key": "secret-test-token"}

    # 3. Custom JUPITER_BASE_URL
    monkeypatch.setenv("JUPITER_BASE_URL", "https://api.jup.ag/swap/v1")
    assert jupiter_client.get_base_url() == "https://api.jup.ag/swap/v1"
    assert jupiter_client.quote_url() == "https://api.jup.ag/swap/v1/quote"
    assert jupiter_client.swap_url() == "https://api.jup.ag/swap/v1/swap"
    assert jupiter_client.program_id_to_label_url() == "https://api.jup.ag/swap/v1/program-id-to-label"


@pytest.mark.parametrize("missing_field", [
    "inAmount",
    "outAmount",
    "otherAmountThreshold",
    "slippageBps",
    "swapMode",
    "inputMint",
    "outputMint",
])
def test_quote_contract_fails_closed_on_missing_fields(missing_field: str):
    """
    Verify the fail-closed contract: if any required field is missing from a Jupiter quote,
    validate_quote_against_intent refuses with QuoteMismatch.
    """
    valid_quote = {
        "inputMint": config.USDC_MINT,
        "outputMint": "So11111111111111111111111111111111111111112",
        "inAmount": "10000000",
        "outAmount": "66666666",
        "otherAmountThreshold": "66333332",
        "slippageBps": 50,
        "swapMode": "ExactIn",
    }
    intent = TradeIntent(
        side="buy",
        wallet="11111111111111111111111111111111",
        input_mint=config.USDC_MINT,
        output_mint="So11111111111111111111111111111111111111112",
        in_amount_raw=10000000,
        slippage_bps=50,
        min_out_raw=66333332,
        reference_price_usd=150.0,
    )

    corrupted = dict(valid_quote)
    del corrupted[missing_field]

    with pytest.raises(QuoteMismatch):
        validate_quote_against_intent(corrupted, intent)
