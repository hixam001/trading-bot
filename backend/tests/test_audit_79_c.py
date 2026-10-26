"""
test_audit_79_c.py — Audit §79 Phase C: Token-safety gate improvements.

Validates:
- C-1: freeze_authority_revoked is False blocks in security_clear; None still passes.
- C-2: Token-2022 mint extension screening (permanentDelegate, transferHook,
       nonTransferable, pausableConfig, defaultAccountState=frozen, transferFeeConfig > max fee).
- C-3: get_mint_extension_flags parser on SPL Token and Token-2022 jsonParsed responses.
- C-4: Unknown-security ticket haircut (UNKNOWN_SECURITY_SIZE_MULT=0.5, falls to 0.0 if < floor).
- C-5: Shadow risk flags computed without affecting gate verdicts (golden test).
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, patch

import config
from models import Candidate, PortfolioState, FeedEvent
from rule_engine.regime import MarketRegime
from rule_engine.rules import security_clear, ACTIVE_RULES
from data_providers.onchain_security import (
    get_mint_extension_flags,
    parse_mint_extensions,
    VERIFIED_DANGEROUS_EXTENSIONS,
)
from sizing import compute_ticket, compute_risk_flags


def _candidate(
    mint="Mint1111111111111111111111111111111111111111",
    mint_auth=True,
    freeze_auth=True,
    honeypot=False,
    token_program="spl-token",
    dangerous_extensions=None,
    transfer_fee_bps=None,
    transfer_fee_enable=None,
    security_unknown=False,
    top_holder_pct=0.10,
    holder_count=500,
) -> Candidate:
    return Candidate(
        symbol="TEST",
        mint_address=mint,
        price_usd=1.0,
        liquidity_usd=100_000.0,
        volume_24h_usd=500_000.0,
        market_cap_usd=1_000_000.0,
        volume_1h_usd=20_000.0,
        buys_1h=50,
        sells_1h=20,
        price_change_1h_pct=5.0,
        age_hours=24.0,
        has_twitter=True,
        has_telegram=True,
        has_website=True,
        decimals=6,
        mint_authority_revoked=mint_auth,
        freeze_authority_revoked=freeze_auth,
        is_likely_honeypot=honeypot,
        token_program=token_program,
        dangerous_extensions=dangerous_extensions or [],
        transfer_fee_bps=transfer_fee_bps,
        transfer_fee_enable=transfer_fee_enable,
        security_unknown=security_unknown,
        top_holder_pct=top_holder_pct,
        holder_count=holder_count,
    )


def _env():
    p = PortfolioState(cash_usd=1000.0, open_positions=[])
    r = MarketRegime(
        computed_at="2026-10-13T00:00:00Z",
        pct_candidates_green_1h=80.0,
        median_volume_1h_usd=10000.0,
        avg_buy_sell_ratio=1.5,
        regime_ok=True,
        regime_detail="healthy",
    )
    return p, r


# ---------------------------------------------------------------------------
# C-1: Freeze Authority Known-Bad Check
# ---------------------------------------------------------------------------

def test_freeze_authority_live_blocks():
    """
    Mutation: removing 'if c.freeze_authority_revoked is False:' from
    security_clear causes this test to fail because a live freeze authority
    would not be marked as a problem.
    """
    p, r = _env()
    c = _candidate(freeze_auth=False)
    res = security_clear(c, p, r)
    assert not res.passed
    assert "freeze authority NOT revoked" in res.detail


def test_freeze_authority_none_passes():
    """B10 preserved: None (= unknown) passes security_clear."""
    p, r = _env()
    c = _candidate(freeze_auth=None)
    res = security_clear(c, p, r)
    assert res.passed
    assert "freeze authority revoked: unknown" in res.detail


# ---------------------------------------------------------------------------
# C-2: Token-2022 Dangerous Extensions in security_clear
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dangerous_ext", [
    "permanentDelegate",
    "transferHook",
    "nonTransferable",
    "pausableConfig",
    "defaultAccountState (frozen)",
])
def test_dangerous_extension_blocks(dangerous_ext):
    """
    Mutation: removing dangerous extension checks from security_clear causes
    this test to fail because tokens with harmful extensions would pass the gate.
    """
    p, r = _env()
    c = _candidate(
        token_program="spl-token-2022",
        dangerous_extensions=[dangerous_ext],
    )
    res = security_clear(c, p, r)
    assert not res.passed
    assert f"dangerous extension: {dangerous_ext}" in res.detail


def test_transfer_fee_under_limit_passes():
    p, r = _env()
    c = _candidate(
        token_program="spl-token-2022",
        transfer_fee_bps=99,
        transfer_fee_enable=True,
    )
    res = security_clear(c, p, r)
    assert res.passed


def test_transfer_fee_over_limit_blocks():
    """
    Mutation: removing 'if c.transfer_fee_bps > max_fee:' from security_clear
    causes this test to fail because predatory transfer fees (>1%) would be admitted.
    """
    p, r = _env()
    c = _candidate(
        token_program="spl-token-2022",
        transfer_fee_bps=101,
        transfer_fee_enable=True,
    )
    res = security_clear(c, p, r)
    assert not res.passed
    assert "transfer fee 101 bps exceeds max 100 bps" in res.detail


# ---------------------------------------------------------------------------
# C-3: Parsing jsonParsed Account Info for Extensions
# ---------------------------------------------------------------------------

# Hand-crafted fixtures from official Solana documentation examples
SPL_TOKEN_JSON_PARSED = {
    "program": "spl-token",
    "parsed": {
        "info": {
            "decimals": 6,
            "freezeAuthority": None,
            "isInitialized": True,
            "mintAuthority": None,
            "supply": "1000000000000",
        },
        "type": "mint",
    },
    "space": 82,
}

TOKEN_2022_WITH_PERMANENT_DELEGATE = {
    "program": "spl-token-2022",
    "parsed": {
        "info": {
            "decimals": 9,
            "freezeAuthority": None,
            "isInitialized": True,
            "mintAuthority": None,
            "supply": "1000000000",
            "extensions": [
                {
                    "extension": "permanentDelegate",
                    "state": {"delegate": "AttackerPubkey111111111111111111111111111"},
                }
            ],
        },
        "type": "mint",
    },
    "space": 218,
}

TOKEN_2022_WITH_HIGH_FEE = {
    "program": "spl-token-2022",
    "parsed": {
        "info": {
            "decimals": 9,
            "freezeAuthority": None,
            "isInitialized": True,
            "mintAuthority": None,
            "supply": "1000000000",
            "extensions": [
                {
                    "extension": "transferFeeConfig",
                    "state": {
                        "newerTransferFee": {
                            "epoch": 500,
                            "maximumFee": "10000",
                            "transferFeeBasisPoints": 250,  # 2.5% > 1%
                        }
                    },
                }
            ],
        },
        "type": "mint",
    },
    "space": 218,
}

TOKEN_2022_DEFAULT_FROZEN = {
    "program": "spl-token-2022",
    "parsed": {
        "info": {
            "decimals": 9,
            "freezeAuthority": None,
            "isInitialized": True,
            "mintAuthority": None,
            "supply": "1000000000",
            "extensions": [
                {
                    "extension": "defaultAccountState",
                    "state": {"accountState": "frozen"},
                }
            ],
        },
        "type": "mint",
    },
    "space": 218,
}


def test_parse_spl_token_mint_extensions():
    parsed = parse_mint_extensions(SPL_TOKEN_JSON_PARSED)
    assert parsed["program"] == "spl-token"
    assert parsed["extensions"] == []
    assert parsed["dangerous_extensions"] == []
    assert parsed["transfer_fee_bps"] is None


def test_parse_token_2022_permanent_delegate():
    parsed = parse_mint_extensions(TOKEN_2022_WITH_PERMANENT_DELEGATE)
    assert parsed["program"] == "spl-token-2022"
    assert "permanentDelegate" in parsed["extensions"]
    assert "permanentDelegate" in parsed["dangerous_extensions"]


def test_parse_token_2022_high_transfer_fee():
    parsed = parse_mint_extensions(TOKEN_2022_WITH_HIGH_FEE)
    assert parsed["program"] == "spl-token-2022"
    assert parsed["transfer_fee_bps"] == 250
    assert any("transferFeeConfig" in d for d in parsed["dangerous_extensions"])


def test_transfer_fee_reports_active_older_but_gates_on_pending_newer():
    """§84: older fee is ACTIVE until the next epoch; newer is scheduled.
    Report the active bps but refuse on the worse of the two."""
    payload = {
        "program": "spl-token-2022",
        "parsed": {"info": {"extensions": [{
            "extension": "transferFeeConfig",
            "state": {
                "olderTransferFee": {"transferFeeBasisPoints": 50},
                "newerTransferFee": {"transferFeeBasisPoints": 250},
            },
        }]}},
    }
    parsed = parse_mint_extensions(payload)
    assert parsed["transfer_fee_bps"] == 50
    assert any("transferFeeConfig" in d for d in parsed["dangerous_extensions"])

    pending_only = {
        "program": "spl-token-2022",
        "parsed": {"info": {"extensions": [{
            "extension": "transferFeeConfig",
            "state": {
                "newerTransferFee": {"transferFeeBasisPoints": 250},
            },
        }]}},
    }
    parsed2 = parse_mint_extensions(pending_only)
    assert parsed2["transfer_fee_bps"] == 250
    assert parsed2["dangerous_extensions"]


def test_parse_token_2022_default_frozen():
    parsed = parse_mint_extensions(TOKEN_2022_DEFAULT_FROZEN)
    assert parsed["program"] == "spl-token-2022"
    assert any("defaultAccountState" in d for d in parsed["dangerous_extensions"])


@pytest.mark.asyncio
async def test_get_mint_extension_flags_rpc_failure_returns_empty():
    with patch("httpx.AsyncClient.post", side_effect=Exception("RPC timeout")):
        flags = await get_mint_extension_flags("AnyMintAddress111111111111111111111111111")
        assert flags == {}


# ---------------------------------------------------------------------------
# C-4: Unknown-Security Ticket Haircut
# ---------------------------------------------------------------------------

def test_haircut_reduces_ticket_when_above_floor(monkeypatch):
    """
    When security is unknown, ticket is multiplied by UNKNOWN_SECURITY_SIZE_MULT (0.5).
    Base $100 -> $50 (floor $25).
    """
    monkeypatch.setattr(config, "SIZING_MODE", "fixed")
    monkeypatch.setattr(config, "INTENDED_POSITION_SIZE_USD", 100.0)
    monkeypatch.setattr(config, "MIN_TICKET_USD", 25.0)
    monkeypatch.setattr(config, "UNKNOWN_SECURITY_SIZE_MULT", 0.5)

    full = compute_ticket(cash_usd=1000.0, heat=None, security_unknown=False)
    assert full == 100.0

    haircut = compute_ticket(cash_usd=1000.0, heat=None, security_unknown=True)
    assert haircut == 50.0


def test_haircut_falls_to_no_trade_when_below_floor(monkeypatch):
    """
    When ticket * haircut < min_ticket_usd, falls to 0.0 (no trade),
    NEVER trades below the minimum size.
    Base $30 * 0.5 = $15 < $25 -> returns 0.0.
    """
    monkeypatch.setattr(config, "SIZING_MODE", "fixed")
    monkeypatch.setattr(config, "INTENDED_POSITION_SIZE_USD", 30.0)
    monkeypatch.setattr(config, "MIN_TICKET_USD", 25.0)
    monkeypatch.setattr(config, "UNKNOWN_SECURITY_SIZE_MULT", 0.5)

    haircut = compute_ticket(cash_usd=1000.0, heat=None, security_unknown=True)
    assert haircut == 0.0


def test_haircut_multiplier_1_restores_old_behavior(monkeypatch):
    """Setting UNKNOWN_SECURITY_SIZE_MULT=1.0 restores full ticket sizing."""
    monkeypatch.setattr(config, "SIZING_MODE", "fixed")
    monkeypatch.setattr(config, "INTENDED_POSITION_SIZE_USD", 100.0)
    monkeypatch.setattr(config, "MIN_TICKET_USD", 25.0)
    monkeypatch.setattr(config, "UNKNOWN_SECURITY_SIZE_MULT", 1.0)

    ticket = compute_ticket(cash_usd=1000.0, heat=None, security_unknown=True)
    assert ticket == 100.0


# ---------------------------------------------------------------------------
# C-5: Shadow Risk Flags (Observability Only) & Golden Test
# ---------------------------------------------------------------------------

def test_shadow_risk_flags_buckets():
    c1 = _candidate(top_holder_pct=0.60, holder_count=30)
    flags1 = compute_risk_flags(c1)
    assert "top_holder:>50%" in flags1
    assert "holders:<50" in flags1

    c2 = _candidate(top_holder_pct=0.35, holder_count=150)
    flags2 = compute_risk_flags(c2)
    assert "top_holder:30-50%" in flags2
    assert "holders:50-200" in flags2

    c3 = _candidate(top_holder_pct=None, holder_count=None)
    flags3 = compute_risk_flags(c3)
    assert "top_holder:unknown" in flags3
    assert "holders:unknown" in flags3


def test_golden_gate_verdict_unchanged_for_clean_candidate():
    """
    Shadow flags and unknown-security metadata never change gate verdict
    for candidates without known-bad security signals.
    """
    p, r = _env()
    c = _candidate()
    results = [rule(c, p, r) for rule in ACTIVE_RULES]
    failed = [res for res in results if not res.passed]
    assert not failed, f"Failed rules: {[(f.rule_id, f.detail) for f in failed]}"
