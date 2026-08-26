"""
tests/test_omo_r6_disclosure.py — OMO-R6 public disclosure endpoint tests.

Properties verified:
  1. /api/disclosure.json returns required fields with correct types
  2. paper_only is always True (hardcoded)
  3. armed is always False (hardcoded; live_execution disarmed)
  4. No secrets leaked (no API keys, no wallet address, no DB URL)
  5. break state pass-through: when liveness says on_break=True, disclosure reports it
  6. /api/reasoning.json returns correct shape with provenance fields
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch, MagicMock
import pytest

import config


# ---------------------------------------------------------------------------
# /api/disclosure.json
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disclosure_required_fields():
    """disclosure.json contains all required top-level fields."""
    from api.routes.disclosure import get_disclosure
    result = await get_disclosure()

    assert "generated_at_utc" in result
    assert "armed" in result
    assert "paper_only" in result
    assert "kill_switch" in result
    assert "break" in result
    assert "config_truths" in result


@pytest.mark.asyncio
async def test_disclosure_paper_only_always_true():
    """PAPER_TRADING_ONLY is hardcoded True and must always surface as True."""
    from api.routes.disclosure import get_disclosure
    result = await get_disclosure()
    assert result["paper_only"] is True


@pytest.mark.asyncio
async def test_disclosure_armed_always_false():
    """LIVE_TRADING_ENABLED defaults to False; disclosure must surface False."""
    from api.routes.disclosure import get_disclosure
    result = await get_disclosure()
    # While unarmed (default), armed must be False
    assert result["armed"] is False


@pytest.mark.asyncio
async def test_disclosure_no_secrets_in_output():
    """No API keys, wallet address, or DB URL should appear in the output."""
    from api.routes.disclosure import get_disclosure
    import json
    result = await get_disclosure()
    output_str = json.dumps(result).lower()

    # These are secrets we must never surface
    forbidden_fragments = [
        "api_key", "apikey", "secret", "password", "private_key",
        "sk-", "supabase_db_url", "birdeye_api_key",
        "deepseek_api_key", "groq_api_key",
    ]
    for frag in forbidden_fragments:
        assert frag not in output_str, (
            f"Potential secret fragment '{frag}' found in disclosure output"
        )


@pytest.mark.asyncio
async def test_disclosure_config_truths_numeric_types():
    """All numeric config_truths fields are numbers or None, not strings."""
    from api.routes.disclosure import get_disclosure
    result = await get_disclosure()
    ct = result["config_truths"]

    numeric_fields = [
        "min_liquidity_usd", "min_volume_1h_usd", "min_ticket_usd",
        "daily_deploy_cap_usd", "stop_loss_pct", "trail_activation_pct",
        "trail_give_back_pp", "max_candidates_per_tick", "tick_interval_seconds",
        "regime_min_median_vol_usd",
    ]
    for field in numeric_fields:
        val = ct.get(field)
        assert val is None or isinstance(val, (int, float)), (
            f"config_truth '{field}' should be numeric or None, got {type(val)}"
        )


@pytest.mark.asyncio
async def test_disclosure_break_state_on_break():
    """When liveness is on break, disclosure reflects on_break=True."""
    from api.routes.disclosure import get_disclosure

    def mock_is_on_break():
        return True

    def mock_break_reason():
        return "deliberate test break"

    def mock_break_until():
        return 9999999999.0

    with (patch("api.routes.disclosure._break_state", return_value={
            "on_break": True,
            "reason": "deliberate test break",
            "break_until_epoch": 9999999999.0,
        })):
        result = await get_disclosure()

    assert result["break"]["on_break"] is True
    assert "deliberate test break" in result["break"]["reason"]


@pytest.mark.asyncio
async def test_disclosure_kill_switch_no_file_returns_active_false():
    """When there is no kill switch file, active must be False (not KILL)."""
    from api.routes.disclosure import _kill_switch_state
    from pathlib import Path
    with patch.object(Path, "is_file", return_value=False):
        state = _kill_switch_state()
    assert state["active"] is False


# ---------------------------------------------------------------------------
# /api/reasoning.json
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reasoning_required_fields():
    """reasoning.json returns correct shape per row."""
    from api.routes.disclosure import get_reasoning
    from contextlib import asynccontextmanager
    import json as _json

    mock_commit = {
        "id": 1,
        "created_at": "2026-08-26T12:00:00+00:00",
        "symbol": "BONK",
        "mint": "mint123",
        "entry_allowed": True,
        "payload": {"think_source": "deepseek:deepseek-chat"},
        "payload_hash": "abc123",
        "nonce": "nonce1",
        "payload_json": _json.dumps({"think_source": "deepseek:deepseek-chat"}),
    }

    with patch("api.routes.disclosure.db") as mock_db:
        @asynccontextmanager
        async def _get_db():
            yield None
        mock_db.get_db = _get_db
        mock_db.get_recent_decision_commits = AsyncMock(return_value=[mock_commit])
        result = await get_reasoning(limit=10)

    assert "reasoning" in result
    assert result["count"] == 1
    row = result["reasoning"][0]
    for field in ("id", "symbol", "think_source", "entry_allowed",
                  "inputs_snapshot_hash", "commit_hash"):
        assert field in row, f"missing field: {field}"


@pytest.mark.asyncio
async def test_reasoning_inputs_hash_is_deterministic():
    """The same payload always produces the same inputs_snapshot_hash."""
    import hashlib, json
    from api.routes.disclosure import get_reasoning

    payload = {"think_source": "template", "mint": "abc"}
    payload_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    expected_hash = hashlib.sha256(payload_str.encode()).hexdigest()

    mock_commit = {
        "id": 2,
        "created_at": "2026-08-26T12:00:00+00:00",
        "symbol": "WIF",
        "mint": "abc",
        "entry_allowed": False,
        "payload": payload,
        "payload_hash": "def456",
        "nonce": "nonce2",
        "payload_json": json.dumps(payload),
    }

    from contextlib import asynccontextmanager

    with patch("api.routes.disclosure.db") as mock_db:
        @asynccontextmanager
        async def _get_db():
            yield None
        mock_db.get_db = _get_db
        mock_db.get_recent_decision_commits = AsyncMock(return_value=[mock_commit])
        result = await get_reasoning(limit=10)

    assert result["reasoning"][0]["inputs_snapshot_hash"] == expected_hash
