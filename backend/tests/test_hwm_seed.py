"""
tests/test_hwm_seed.py — A7 (repo audit): high-water marks survive a restart.

The HWM dict is per-process state; without seeding, every restart reset
every trailing exit to its ENTRY price — a position that rallied and
pulled back while the cycle was down could never fire
exit_trail_give_back until it made a new high. The seed restores marks
from the shared trades table's OPEN rows (the live mirror persists
high_water_usd on every _manage pass), never lowers an in-memory mark,
and is fail-soft: any mirror trouble leaves the marks unchanged
(cold start applies), never raises into the caller.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

import config as live_config
import run_live_cycle as rlc


@pytest_asyncio.fixture
async def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(live_config, "DB_PATH", tmp_path / "hwm_seed.db")
    monkeypatch.setattr(live_config, "DATA_BACKEND", "mock")
    from api import db
    await db.init_db()
    yield db


def _trade(mint: str, entry: float = 1.0):
    from models import Trade
    return Trade(
        trade_id=f"live-{mint[:8]}", symbol=mint[:6], mint_address=mint,
        opened_at=datetime.now(timezone.utc).isoformat(),
        entry_price_usd=entry, position_size_usd=entry * 100.0,
        quantity=100.0, candidate_snapshot={}, thesis="seed test",
        is_open=True,
    )


MINT = "MINTDDD1111111111111111111111111111111111111"


async def test_seed_restores_hwm_from_open_mirror_rows(_db):
    from api import db
    trade = _trade(MINT, entry=1.0)
    async with db.get_db() as conn:
        # try_insert_open_trade returns the affected ROWCOUNT, not the id.
        assert await db.try_insert_open_trade(conn, trade) == 1
        # the position rallied to 2.0 while the cycle was up; the mirror
        # persists that peak on every _manage pass.
        await db.update_high_water(conn, trade.trade_id, 2.0)
    hwm = await rlc._seed_hwm_from_mirror({})
    assert hwm[MINT] == pytest.approx(2.0)


async def test_seed_never_lowers_an_in_memory_mark(_db):
    from api import db
    trade = _trade(MINT, entry=1.0)
    async with db.get_db() as conn:
        assert await db.try_insert_open_trade(conn, trade) == 1
        await db.update_high_water(conn, trade.trade_id, 2.0)
    hwm = await rlc._seed_hwm_from_mirror({MINT: 3.0})
    assert hwm[MINT] == pytest.approx(3.0)


async def test_seed_is_fail_soft(_db, monkeypatch):
    import contextlib
    from api import db

    @contextlib.asynccontextmanager
    async def boom():
        raise RuntimeError("mirror unreadable")
        yield  # pragma: no cover

    monkeypatch.setattr(db, "get_db", boom)
    hwm = await rlc._seed_hwm_from_mirror({"keep": 1.0})
    # unchanged marks, no exception escaped
    assert hwm == {"keep": 1.0}


async def test_once_mode_seeds_hwm_on_first_use(monkeypatch):
    """§86: seeding used to live only in the long-running startup, so
    `--once` never restored trailing marks. _hwm_for_cycle must seed on the
    first call, then reuse the same dict."""
    calls = []

    async def fake_seed(existing):
        calls.append(existing)
        return {MINT: 2.0}

    monkeypatch.setattr(rlc, "_seed_hwm_from_mirror", fake_seed)
    monkeypatch.delattr(rlc.run_cycle, "_hwm", raising=False)

    hwm = await rlc._hwm_for_cycle()
    assert hwm == {MINT: 2.0}
    assert calls == [{}]
    assert rlc.run_cycle._hwm is hwm

    again = await rlc._hwm_for_cycle()
    assert again is hwm
    assert len(calls) == 1


# --- §84: a new peak is persisted, not just kept in memory -------------------

async def test_scan_persists_new_high_water_to_mirror(_db, monkeypatch):
    from api import db

    trade = _trade(MINT, entry=1.0)
    async with db.get_db() as conn:
        assert await db.try_insert_open_trade(conn, trade) == 1

    async def fake_decimals(mint):
        return 6

    class FakeJupiter:
        async def get_current_price(self, mint, decimals=None):
            return 1.5

    class FakeLedger:
        def tranches_taken(self, mint):
            return 0

    monkeypatch.setattr(rlc, "_cached_decimals", fake_decimals)
    hwm: dict = {}
    m = {"opened_ts": datetime.now(timezone.utc).timestamp(), "price_usd": 1.0,
         "cost": 100.0, "tokens": 100.0}
    await rlc._scan_exits_for_position(FakeJupiter(), FakeLedger(), hwm, MINT, m)

    assert hwm[MINT] == pytest.approx(1.5)
    async with db.get_db() as conn:
        cur = await conn.execute(
            "SELECT high_water_usd FROM trades WHERE trade_id = ?",
            (trade.trade_id,))
        row = await cur.fetchone()
    assert row["high_water_usd"] == pytest.approx(1.5)
