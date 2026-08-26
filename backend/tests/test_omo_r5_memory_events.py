from __future__ import annotations

import pytest

import config
from api import db
from data_providers.mock import MockProvider
from llm.thinker import build_think_prompt
from llm.thinker import Thinker
from main import run_tick
from tests.test_rules import make_candidate


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "omo-r5.db")
    monkeypatch.setattr(config, "DATA_BACKEND", "mock")


@pytest.mark.asyncio
async def test_memory_recall_increments_hits_and_event_roundtrip(env):
    await db.init_db()
    async with db.get_db() as conn:
        memory_id = await db.upsert_memory(conn, "BONK", "avoid chasing thin liquidity", 2.0)
        first = await db.recall_memories(conn, topic="BONK")
        second = await db.recall_memories(conn, topic="BONK")
        await db.insert_event(conn, "read", "2026-08-26T00:00:00+00:00", payload={"count": 2})
        events = await db.get_recent_events(conn)

    assert first[0]["id"] == memory_id
    assert first[0]["hits"] == 1
    assert second[0]["hits"] == 2
    assert events[0]["kind"] == "read"
    assert events[0]["payload"] == {"count": 2}


@pytest.mark.asyncio
async def test_invalid_memory_and_event_are_rejected(env):
    await db.init_db()
    async with db.get_db() as conn:
        with pytest.raises(ValueError):
            await db.upsert_memory(conn, "", "missing topic")
        with pytest.raises(ValueError):
            await db.insert_event(conn, "unknown", "2026-08-26T00:00:00+00:00")


@pytest.mark.asyncio
async def test_memory_context_is_injected_without_changing_template_logic(env):
    candidate = make_candidate(symbol="BONK")
    prompt = build_think_prompt(candidate, "Memory (context only): BONK: avoid chasing thin liquidity")
    assert "avoid chasing thin liquidity" in prompt


@pytest.mark.asyncio
async def test_mock_tick_records_omo_r5_stage_events(env):
    await db.init_db()
    await run_tick(MockProvider(), Thinker())
    async with db.get_db() as conn:
        events = await db.get_recent_events(conn, limit=500)

    kinds = {event["kind"] for event in events}
    assert {"read", "thought", "did", "refused"} <= kinds
