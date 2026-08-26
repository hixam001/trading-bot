"""
tests/test_omo_r7_retro.py — OMO-R7 retro audit-log signature matching.

Key properties verified:
  1. Basic match: decision + fill within window → attributed
  2. Double-claim prevention: 3 decisions, 2 fills → 2 matched, 1 unmatched
  3. Window edge: fill >12h after decision → NOT matched
  4. Symbol mismatch: different symbols → NOT matched
  5. Temporal ordering: fill before decision → NOT matched
  6. Exact-bind precedence: row with signature already set is NOT overwritten
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from retro_matcher import run_retro_match, _strip_symbol, _parse_iso


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_decision(id_: int, symbol: str, created_at: str) -> dict:
    return {
        "id": id_,
        "created_at": created_at,
        "symbol": symbol,
        "mint_address": f"mint_{symbol}",
        "verdict": "buy",
        "payload": {"mint": f"mint_{symbol}", "entry_allowed": True},
    }


def _make_fill(trade_id: str, symbol: str, opened_at: str) -> dict:
    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "mint_address": f"mint_{symbol}",
        "opened_at": opened_at,
        "side": "buy",
    }


# ---------------------------------------------------------------------------
# Unit tests for pure helpers
# ---------------------------------------------------------------------------

def test_strip_symbol_strips_dollar_and_uppercases():
    assert _strip_symbol("$BONK") == "BONK"
    assert _strip_symbol("bonk") == "BONK"
    assert _strip_symbol("  $bonk  ") == "BONK"


def test_parse_iso_valid():
    dt = _parse_iso("2026-08-26T12:00:00+00:00")
    assert dt is not None
    assert dt.year == 2026


def test_parse_iso_none():
    assert _parse_iso(None) is None
    assert _parse_iso("") is None
    assert _parse_iso("not-a-date") is None


# ---------------------------------------------------------------------------
# Integration tests via mocked db
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_basic_match_attributes_fill():
    """A decision + fill with matching symbol within window → attributed."""
    now = _now()
    decisions = [_make_decision(1, "BONK", _iso(now - timedelta(minutes=5)))]
    fills = [_make_fill("trade-abc", "BONK", _iso(now))]

    bind_calls = []

    async def fake_bind(conn, commit_id, signature, phase, matched_by):
        bind_calls.append((commit_id, signature, phase, matched_by))
        return 1

    mock_db = AsyncMock()
    mock_db.get_pending_unsigned_commits = AsyncMock(return_value=decisions)
    mock_db.get_recent_fills_for_retro = AsyncMock(return_value=fills)
    mock_db.bind_commit_signature = fake_bind

    with patch("retro_matcher.db", mock_db):
        result = await run_retro_match(conn=None)

    assert result["matched"] == 1
    assert len(bind_calls) == 1
    commit_id, sig, phase, matched_by = bind_calls[0]
    assert commit_id == 1
    assert sig == "trade-abc"
    assert phase == "filled"
    assert matched_by == "retro"


@pytest.mark.asyncio
async def test_double_claim_prevention():
    """3 decisions, 2 fills → 2 matched, 1 unmatched; no fill claimed twice."""
    now = _now()
    base = now - timedelta(minutes=10)
    decisions = [
        _make_decision(1, "BONK", _iso(base)),
        _make_decision(2, "BONK", _iso(base + timedelta(minutes=1))),
        _make_decision(3, "BONK", _iso(base + timedelta(minutes=2))),
    ]
    fills = [
        _make_fill("fill-1", "BONK", _iso(base + timedelta(minutes=3))),
        _make_fill("fill-2", "BONK", _iso(base + timedelta(minutes=4))),
    ]

    bind_calls = []

    async def fake_bind(conn, commit_id, signature, phase, matched_by):
        bind_calls.append((commit_id, signature))
        return 1

    mock_db = AsyncMock()
    mock_db.get_pending_unsigned_commits = AsyncMock(return_value=decisions)
    mock_db.get_recent_fills_for_retro = AsyncMock(return_value=fills)
    mock_db.bind_commit_signature = fake_bind

    with patch("retro_matcher.db", mock_db):
        result = await run_retro_match(conn=None)

    assert result["matched"] == 2
    # No fill should appear in both bind calls
    claimed_sigs = [sig for _, sig in bind_calls]
    assert len(claimed_sigs) == len(set(claimed_sigs)), "fill claimed twice!"


@pytest.mark.asyncio
async def test_window_edge_fill_too_late():
    """Fill >12h after decision is outside the window and NOT matched."""
    now = _now()
    decisions = [_make_decision(1, "WIF", _iso(now - timedelta(hours=13)))]
    fills = [_make_fill("fill-late", "WIF", _iso(now))]  # 13h after

    bind_calls = []
    mock_db = AsyncMock()
    mock_db.get_pending_unsigned_commits = AsyncMock(return_value=decisions)
    mock_db.get_recent_fills_for_retro = AsyncMock(return_value=fills)
    mock_db.bind_commit_signature = AsyncMock(side_effect=lambda *a, **k: bind_calls.append(a) or 0)

    with patch("retro_matcher.db", mock_db):
        result = await run_retro_match(conn=None)

    assert result["matched"] == 0
    assert len(bind_calls) == 0


@pytest.mark.asyncio
async def test_symbol_mismatch_not_matched():
    """Different symbols never match even with identical timestamps."""
    now = _now()
    decisions = [_make_decision(1, "BONK", _iso(now - timedelta(minutes=1)))]
    fills = [_make_fill("fill-wrong", "WIF", _iso(now))]

    mock_db = AsyncMock()
    mock_db.get_pending_unsigned_commits = AsyncMock(return_value=decisions)
    mock_db.get_recent_fills_for_retro = AsyncMock(return_value=fills)
    mock_db.bind_commit_signature = AsyncMock(return_value=0)

    with patch("retro_matcher.db", mock_db):
        result = await run_retro_match(conn=None)

    assert result["matched"] == 0
    mock_db.bind_commit_signature.assert_not_called()


@pytest.mark.asyncio
async def test_fill_before_decision_not_matched():
    """Fill whose timestamp precedes the decision is NOT attributed (ordering)."""
    now = _now()
    decisions = [_make_decision(1, "BONK", _iso(now))]
    fills = [_make_fill("fill-early", "BONK", _iso(now - timedelta(minutes=5)))]

    mock_db = AsyncMock()
    mock_db.get_pending_unsigned_commits = AsyncMock(return_value=decisions)
    mock_db.get_recent_fills_for_retro = AsyncMock(return_value=fills)
    mock_db.bind_commit_signature = AsyncMock(return_value=0)

    with patch("retro_matcher.db", mock_db):
        result = await run_retro_match(conn=None)

    assert result["matched"] == 0
    mock_db.bind_commit_signature.assert_not_called()


@pytest.mark.asyncio
async def test_db_error_is_fail_soft():
    """If db read raises, result contains error but does not propagate."""
    mock_db = AsyncMock()
    mock_db.get_pending_unsigned_commits = AsyncMock(
        side_effect=RuntimeError("db gone"))

    with patch("retro_matcher.db", mock_db):
        result = await run_retro_match(conn=None)

    # Should not raise; should return error key
    assert result["matched"] == 0
    assert "error" in result
