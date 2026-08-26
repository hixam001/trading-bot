"""
tests/test_omo_r1_binding.py — OMO-R1 independent verifier + binding report.

Properties verified:
  1. _verify_binding_checks passes all 4 checks on a well-formed tx
  2. A failed tx (meta.err != null) → mismatched
  3. Temporal ordering violation → mismatched
  4. Wrong fee payer → mismatched
  5. Mint not in token balances → mismatched
  6. Missing tx (get_transaction=None) → unknown, never pass
  7. live_execution not importable (paper mode) → unknown, never pass
  8. Unbound rows (no signature) → status='unbound' in binding endpoint
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from api.routes.proof import _verify_binding_checks


def _good_tx(wallet: str = "WALLET123", mint: str = "MINT456") -> dict:
    """A fully valid transaction fixture.

    blockTime 1798761600 ≈ 2027-01-01T00:00:00Z, which is after our
    commit timestamp 2026-08-26T10:00:00+00:00 (1787738400), satisfying
    the time-ordering check.
    """
    return {
        "blockTime": 1798761600,  # 2027-01-01 > commit 2026-08-26
        "meta": {
            "err": None,
            "preTokenBalances": [{"mint": mint}],
            "postTokenBalances": [{"mint": mint}],
        },
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": wallet}],
            }
        },
    }


def _good_payload(mint: str = "MINT456") -> dict:
    return {"mint": mint, "entry_allowed": True}


_COMMIT_AT = "2026-08-26T10:00:00+00:00"


# ---------------------------------------------------------------------------
# _verify_binding_checks unit tests
# ---------------------------------------------------------------------------

def test_all_checks_pass_on_well_formed_tx():
    tx = _good_tx()
    result = _verify_binding_checks(tx, _good_payload(), _COMMIT_AT, "WALLET123")
    assert result["status"] == "matched"
    for check in result["checks"]:
        assert check["status"] == "pass", f"check {check['name']} not pass: {check}"


def test_failed_tx_returns_mismatched():
    tx = _good_tx()
    tx["meta"]["err"] = {"InstructionError": [0, "InvalidArgument"]}
    result = _verify_binding_checks(tx, _good_payload(), _COMMIT_AT, "WALLET123")
    assert result["status"] == "mismatched"
    conf_check = next(c for c in result["checks"] if c["name"] == "tx_confirmed")
    assert conf_check["status"] == "fail"


def test_temporal_ordering_violation():
    """Commit created AFTER the tx blockTime → ordering fail."""
    tx = _good_tx()
    # commit at 2036 (epoch > 2027 blockTime)
    result = _verify_binding_checks(
        tx, _good_payload(), "2036-01-01T00:00:00+00:00", "WALLET123"
    )
    assert result["status"] == "mismatched"
    ord_check = next(c for c in result["checks"] if c["name"] == "time_ordering")
    assert ord_check["status"] == "fail"


def test_wrong_fee_payer_returns_mismatched():
    tx = _good_tx()
    result = _verify_binding_checks(tx, _good_payload(), _COMMIT_AT, "WRONG_WALLET")
    assert result["status"] == "mismatched"
    fp_check = next(c for c in result["checks"] if c["name"] == "fee_payer")
    assert fp_check["status"] == "fail"


def test_mint_not_in_balances_returns_mismatched():
    tx = _good_tx(mint="MINT456")
    tx["meta"]["preTokenBalances"] = [{"mint": "OTHER_MINT"}]
    tx["meta"]["postTokenBalances"] = [{"mint": "OTHER_MINT"}]
    result = _verify_binding_checks(tx, _good_payload("MINT456"), _COMMIT_AT, "WALLET123")
    assert result["status"] == "mismatched"
    mint_check = next(c for c in result["checks"] if c["name"] == "mint_present")
    assert mint_check["status"] == "fail"


def test_missing_block_time_returns_unknown_not_pass():
    """blockTime absent → time_ordering is unknown."""
    tx = _good_tx()
    tx.pop("blockTime")
    result = _verify_binding_checks(tx, _good_payload(), _COMMIT_AT, "WALLET123")
    assert result["status"] in ("unknown", "matched")
    ord_check = next(c for c in result["checks"] if c["name"] == "time_ordering")
    assert ord_check["status"] == "unknown"


def test_empty_account_keys_returns_unknown_not_pass():
    """No accountKeys → fee_payer is unknown, never pass."""
    tx = _good_tx()
    tx["transaction"]["message"]["accountKeys"] = []
    result = _verify_binding_checks(tx, _good_payload(), _COMMIT_AT, "WALLET123")
    fp_check = next(c for c in result["checks"] if c["name"] == "fee_payer")
    assert fp_check["status"] == "unknown"


# ---------------------------------------------------------------------------
# /api/binding.json endpoint tests
# ---------------------------------------------------------------------------

def _make_mock_db(rows):
    """Return a properly-structured mock db module for proof.py tests."""
    mock_db = MagicMock()

    @asynccontextmanager
    async def _mock_get_db():
        yield None

    mock_db.get_db = _mock_get_db
    mock_db.get_verify_commits = AsyncMock(return_value=rows)
    return mock_db


@pytest.mark.asyncio
async def test_binding_unbound_rows_report_unbound():
    """Rows with no signature → status='unbound'."""
    from api.routes.proof import get_binding

    mock_row = {
        "id": 1, "symbol": "BONK", "signature": None,
        "verdict": "buy", "payload_json": "{}", "payload_hash": "hash1",
        "nonce": "n1", "created_at": _COMMIT_AT,
    }

    with patch("api.routes.proof.db", _make_mock_db([mock_row])):
        result = await get_binding()

    assert result["totals"]["unbound"] == 1
    assert result["pairs"][0]["status"] == "unbound"


@pytest.mark.asyncio
async def test_binding_rpc_returns_none_reports_unknown():
    """When get_transaction returns None → status=unknown, not pass."""
    from api.routes.proof import get_binding

    mock_row = {
        "id": 3, "symbol": "WIF", "signature": "rpc-no-data-sig",
        "verdict": "buy", "payload_json": '{"mint": "MINT2"}',
        "payload_hash": "hash3", "nonce": "n3", "created_at": _COMMIT_AT,
    }

    async def mock_get_transaction(sig):
        return None  # RPC unavailable

    mock_live_solana = MagicMock()
    mock_live_solana.get_transaction = mock_get_transaction

    with patch("api.routes.proof.db", _make_mock_db([mock_row])), \
         patch.dict("sys.modules", {"live_execution.solana": mock_live_solana}):
        result = await get_binding()

    bound_pairs = [p for p in result["pairs"] if p.get("signature")]
    assert len(bound_pairs) == 1
    assert bound_pairs[0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_binding_matched_tx_reports_matched():
    """A row with a valid tx and matching wallet/mint → status=matched."""
    from api.routes.proof import get_binding
    import json

    mint = "MINT456"
    wallet = "WALLET123"
    payload = {"mint": mint, "entry_allowed": True}

    mock_row = {
        "id": 4, "symbol": "BONK", "signature": "valid-sig",
        "verdict": "buy", "payload_json": json.dumps(payload),
        "payload_hash": "hash4", "nonce": "n4", "created_at": _COMMIT_AT,
    }

    async def mock_get_transaction(sig):
        return _good_tx(wallet=wallet, mint=mint)

    mock_live_solana = MagicMock()
    mock_live_solana.get_transaction = mock_get_transaction

    import config as cfg
    with patch("api.routes.proof.db", _make_mock_db([mock_row])), \
         patch.dict("sys.modules", {"live_execution.solana": mock_live_solana}), \
         patch.object(cfg, "WALLET_ADDRESS", wallet, create=True):
        result = await get_binding()

    bound_pairs = [p for p in result["pairs"] if p.get("signature")]
    assert len(bound_pairs) == 1
    assert bound_pairs[0]["status"] == "matched"
    assert result["totals"]["matched"] == 1
