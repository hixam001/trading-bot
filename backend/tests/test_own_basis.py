"""
tests for crowd.read_own_basis — A4 (omo audit §28): reading the bot's OWN
position accounting back from the fomo.family thesis feed.

reference: fomo.server.ts readOwnBasis — invested = max(0, value - unrealized),
matched on the raw board by handle (case-insensitive), capped at 10 mints.
Hermetic: _thesis_payload is faked, no network.
"""
from __future__ import annotations

import pytest

import config
from data_providers import crowd


def _row(who, size, unrealized, realized=0.0, closed=False, text="solid tape"):
    return {"who": who, "text": text, "size_usd": size,
            "unrealized_usd": unrealized, "realized_usd": realized,
            "pnl_pct": 0.0, "closed": closed}


def _patch_payload(monkeypatch, payloads):
    """payloads: {mint: payload dict or None}"""
    async def fake_payload(mint):
        return payloads.get(mint)
    monkeypatch.setattr(crowd, "_thesis_payload", fake_payload)


@pytest.fixture
def own_handle(monkeypatch):
    monkeypatch.setattr(config, "FOMO_OWN_HANDLE", "Bert")
    return "Bert"


async def test_disabled_without_handle(monkeypatch):
    monkeypatch.setattr(config, "FOMO_OWN_HANDLE", "")
    _patch_payload(monkeypatch, {"M": {"rows": [_row("Bert", 5.0, 1.0)],
                                       "total": 1}})
    assert await crowd.read_own_basis([{"mint": "M", "symbol": "m"}]) == []


async def test_own_row_matched_case_insensitively(own_handle, monkeypatch):
    _patch_payload(monkeypatch, {"M": {"rows": [
        _row("someoneElse", 9.0, 2.0),
        _row("bert", 5.0, 1.0, realized=0.5),
    ], "total": 2}})
    rows = await crowd.read_own_basis([{"mint": "M", "symbol": "$mem"}])
    assert len(rows) == 1
    r = rows[0]
    assert r["mint"] == "M"
    assert r["symbol"] == "MEM"
    assert r["value_usd"] == 5.0
    assert r["unrealized_usd"] == 1.0
    assert r["realized_usd"] == 0.5
    assert r["closed"] is False


async def test_invested_is_value_minus_unrealized_floored_at_zero(
        own_handle, monkeypatch):
    _patch_payload(monkeypatch, {
        "M1": {"rows": [_row("Bert", 5.0, 1.0)], "total": 1},
        "M2": {"rows": [_row("Bert", 2.0, 9.0)], "total": 1},   # deep loss
    })
    rows = await crowd.read_own_basis([{"mint": "M1"}, {"mint": "M2"}])
    by_mint = {r["mint"]: r for r in rows}
    assert by_mint["M1"]["invested_usd"] == pytest.approx(4.0)
    assert by_mint["M2"]["invested_usd"] == 0.0   # floored, never negative


async def test_at_prefix_on_either_side_still_matches(own_handle, monkeypatch):
    monkeypatch.setattr(config, "FOMO_OWN_HANDLE", "@Bert")
    _patch_payload(monkeypatch, {"M": {"rows": [_row("bert", 5.0, 0.0)],
                                       "total": 1}})
    rows = await crowd.read_own_basis([{"mint": "M"}])
    assert len(rows) == 1


async def test_feed_unanswered_contributes_nothing(own_handle, monkeypatch):
    _patch_payload(monkeypatch, {"M1": None,
                                 "M2": {"rows": [_row("Bert", 3.0, 0.0)],
                                        "total": 1}})
    rows = await crowd.read_own_basis([{"mint": "M1"}, {"mint": "M2"}])
    assert [r["mint"] for r in rows] == ["M2"]


async def test_no_own_posting_on_board_is_skipped(own_handle, monkeypatch):
    _patch_payload(monkeypatch, {"M": {"rows": [_row("stranger", 9.0, 1.0)],
                                       "total": 1}})
    assert await crowd.read_own_basis([{"mint": "M"}]) == []


async def test_capped_at_ten_mints(own_handle, monkeypatch):
    seen = []

    async def counting_payload(mint):
        seen.append(mint)
        return {"rows": [_row("Bert", 1.0, 0.0)], "total": 1}

    monkeypatch.setattr(crowd, "_thesis_payload", counting_payload)
    picks = [{"mint": f"M{i}"} for i in range(14)]
    rows = await crowd.read_own_basis(picks)
    assert len(rows) == 10
    assert len(seen) == 10


async def test_empty_and_missing_mints_are_skipped(own_handle, monkeypatch):
    _patch_payload(monkeypatch, {"M": {"rows": [_row("Bert", 1.0, 0.0)],
                                       "total": 1}})
    rows = await crowd.read_own_basis([{"mint": ""}, {"symbol": "x"},
                                       {"mint": "M"}])
    assert [r["mint"] for r in rows] == ["M"]


async def test_own_basis_ignores_substantive_filter(own_handle, monkeypatch):
    """Our own thesis may be a single emoji and still carry valid
    accounting — read_own_basis matches on the RAW rows."""
    _patch_payload(monkeypatch, {"M": {"rows": [_row("Bert", 5.0, 1.0,
                                                     text="🚀")],
                                       "total": 1}})
    rows = await crowd.read_own_basis([{"mint": "M"}])
    assert len(rows) == 1
    # ...while the conviction feed still drops the junk text:
    theses = await crowd.fetch_fomo_theses("M")
    assert theses["theses"] == []
    assert theses["total"] == 1
