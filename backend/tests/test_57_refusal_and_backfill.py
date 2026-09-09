"""
tests/test_57_refusal_and_backfill.py — §57 regressions.

Three surfaces this batch changed:

  1. The /api/* loud JSON-404 is INDEPENDENT of the frontend build. It used
     to live inside `if FRONTEND_DIST.exists():` — a buildless checkout
     (fresh clone, split deploy) silently lost the contract and unknown
     /api/* paths returned a bare 404 with no JSON detail. The catch-all is
     now registered unconditionally; pinned here by SOURCE.

  2. The per-candidate THINK_PROMPT carries the §57 refusal discipline
     (the §2 root cause: our model refused ~0% of gate-passers during the
     Aug 28–31 window vs the reference's 74% — everything the gate let
     through became a buy). The counter-case bar is pinned so it cannot be
     quietly dropped in a prompt touch-up.

  3. The ledger -> trades backfill (scripts/backfill_trades_mirror.py)
     pairs closes with their basis buys exactly like perf_report's honest
     denominator, never fabricates a % for no-basis rows, and is
     idempotent: re-running the insert is a no-op.

Hermetic: tmp sqlite DB, inline ledger rows, source inspection. No network.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest
import pytest_asyncio

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

sys.path.insert(0, str(BACKEND.parent / "scripts"))

import config                                       # noqa: E402
from api import db                                  # noqa: E402
from models import Trade                            # noqa: E402


@pytest_asyncio.fixture
async def fresh_db(tmp_path, monkeypatch):
    config.DB_PATH = tmp_path / "t57.db"
    monkeypatch.setattr(config, "INGESTED_KNOWLEDGE_DIR", tmp_path / "kb")
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")
    await db.init_db()
    yield


# ---------------------------------------------------------------------------
# 1 — the /api/* 404 contract is build-independent
# ---------------------------------------------------------------------------

def test_api_404_catch_all_is_registered_unconditionally():
    """The registration must be OUTSIDE `if FRONTEND_DIST.exists():` —
    otherwise a buildless checkout loses the loud JSON-404 for unknown
    /api/* paths (the exact CI ambiguity §57 fixes: those two 'failing'
    runs failed with a bare 404 that told nobody anything).

    Pinned structurally: the decorator must sit at MODULE level (column 0
    — anything inside the build-conditional block is indented), and it
    must precede the SPA fallback's decorator in source order."""
    from api import main as api_main

    src = inspect.getsource(api_main)
    lines = src.splitlines()
    api_route_line = next(
        (i for i, ln in enumerate(lines)
         if ln.strip().startswith('@app.get("/api/{full_path:path}"')),
        None)
    assert api_route_line is not None
    # module level: the decorator itself is unindented
    assert lines[api_route_line].startswith("@app.get"), (
        "the /api/* JSON-404 catch-all must be registered at module level, "
        "not inside the `if FRONTEND_DIST.exists():` block"
    )
    # and it precedes the SPA-only catch-all (which IS build-conditional)
    spa_line = next(
        (i for i, ln in enumerate(lines)
         if ln.strip().startswith('@app.get("/{full_path:path}"')),
        None)
    assert spa_line is not None
    assert api_route_line < spa_line
    # and the bare /api prefix is covered too
    assert '@app.get("/api", include_in_schema=False)' in src


def test_api_404_detail_contract_buildless(fresh_db, monkeypatch):
    """Unknown /api/* paths fail loudly with the JSON detail even when the
    app was built with NO frontend dist (forced here so the test is
    meaningful even on a checkout that HAS a build)."""
    import httpx
    from api import main as api_main

    monkeypatch.setattr(
        api_main, "FRONTEND_DIST",
        api_main.config.BASE_DIR.parent / "frontend" / "no-build")

    async def probe():
        transport = httpx.ASGITransport(app=api_main.app)
        async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1") as c:
            for bad in ("/api/nope", "/api/deeply/typoed/path"):
                r = await c.get(bad)
                assert r.status_code == 404, bad
                assert r.headers["content-type"].startswith(
                    "application/json"), bad
                assert "Unknown API path" in r.json()["detail"], bad

    asyncio.run(probe())


# ---------------------------------------------------------------------------
# 2 — the refusal discipline is pinned in the prompts
# ---------------------------------------------------------------------------

def test_think_prompt_carries_refusal_discipline():
    from llm.thinker import THINK_PROMPT

    # the counter-case bar
    assert "counter-case" in THINK_PROMPT
    # buy is the exception, not the default
    assert 'the verdict is "pass"' in THINK_PROMPT
    # and the JSON contract is unchanged (the parser depends on it)
    assert '"verdict": "buy" or "pass"' in THINK_PROMPT
    assert '{"thesis"' in THINK_PROMPT


def test_brain_system_prompt_pins_the_decline_posture():
    from llm.llm_brain import LLM_SYSTEM

    assert "PASS MOST OF THEM" in LLM_SYSTEM
    assert "never the default" in LLM_SYSTEM


# ---------------------------------------------------------------------------
# 3 — the backfill: honest basis pairing + idempotency
# ---------------------------------------------------------------------------

def _ledger_rows():
    """A miniature ledger with one full round-trip + one no-basis close."""
    m1 = "MINTAAA1111111111111111111111111111111111111"
    m2 = "MINTBBB2222222222222222222222222222222222222"
    return [
        {"kind": "buy", "mint": m1, "usd_size": 1.0, "tokens_out": 100.0,
         "price_usd": 0.01, "ts": 1000.0, "idempotency_key": "b1",
         "status": "closed"},
        {"kind": "close", "mint": m1, "usd_size": 0.75, "pnl_usd": -0.25,
         "price_usd": 0.0075, "ts": 2000.0, "idempotency_key": "c1",
         "status": "closed", "rule_id": "exit_stop_loss"},
        # no-basis close: nothing bought since the previous close
        {"kind": "close", "mint": m2, "usd_size": 0.5, "pnl_usd": -0.1,
         "price_usd": 0.005, "ts": 3000.0, "idempotency_key": "c2",
         "status": "closed", "rule_id": "exit_stop_loss"},
    ]


def test_backfill_pairs_basis_and_skips_no_basis():
    from backfill_trades_mirror import closed_trade_rows

    rows, skipped = closed_trade_rows(_ledger_rows())
    assert skipped == 1                      # the no-basis close is counted
    assert len(rows) == 1
    r = rows[0]
    assert r["mint_address"].startswith("MINTAAA")
    assert r["position_size_usd"] == 1.0     # the basis buy's cost
    assert r["realized_pnl_usd"] == -0.25
    assert r["realized_pnl_pct"] == -25.0     # pnl / basis, never a guess
    assert r["exit_reason"] == "exit_stop_loss"
    assert r["trade_id"].startswith("ledger-")


async def test_backfill_insert_is_idempotent(fresh_db):
    from backfill_trades_mirror import closed_trade_rows

    rows, _ = closed_trade_rows(_ledger_rows())
    trades = [Trade(**r, is_open=False) for r in rows]
    async with db.get_db() as conn:
        n1 = await db.insert_closed_trade_row(conn, trades[0])
        assert n1 == 1
        # re-insert: no-op, never a duplicate
        n2 = await db.insert_closed_trade_row(conn, trades[0])
        assert n2 == 0
        closed = await db.get_all_closed_trades(conn)
    assert len(closed) == 1
    assert closed[0].realized_pnl_pct == pytest.approx(-25.0)
