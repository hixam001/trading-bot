"""
§88 Phase F — a stop signal must never start a NEW entry.

The in-flight cycle is allowed to finish exits/management (the §87 B
guarantee), but once SIGTERM/SIGINT has set the stop event no new buy may be
sent. These tests exercise the real `run_cycle` entry boundary with the read/
brain/gate/execute dependencies stubbed, so a regression that moved the check
after `place_order` (or dropped it) fails loudly.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import decision_pipeline
import run_live_cycle as rlc
from models import Candidate, PortfolioState

MINT = "MintStop88AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture
def stopped_cycle_env(tmp_path, monkeypatch):
    """Minimal hermetic cycle whose only candidate passes every gate."""
    monkeypatch.setattr(rlc.live_config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(rlc, "_unresolved_qty_alerted", set())
    monkeypatch.setattr(rlc.paper_config, "LLM_BRAIN", False)

    async def fake_portfolio(ledger):
        return (PortfolioState(cash_usd=1000.0), {},
                {"checked": False, "discrepancies": []})

    async def fake_hwm():
        return {}

    async def fake_manage(*args, **kwargs):
        return None

    async def fake_crosscheck(meta):
        return []

    async def noop(*args, **kwargs):
        return None

    class StubJupiter:
        pass

    class StubThinker:
        async def aclose(self):
            pass

    monkeypatch.setattr(rlc, "_live_portfolio", fake_portfolio)
    monkeypatch.setattr(rlc, "_hwm_for_cycle", fake_hwm)
    monkeypatch.setattr(rlc, "JupiterProvider", StubJupiter)
    monkeypatch.setattr(rlc, "_manage", fake_manage)
    monkeypatch.setattr(rlc, "_crosscheck_basis", fake_crosscheck)
    monkeypatch.setattr(rlc, "_mirror_live_trades", noop)
    monkeypatch.setattr(rlc, "_journal_cycle_regime", noop)
    monkeypatch.setattr(rlc, "_journal_feed_event", noop)
    monkeypatch.setattr(rlc, "_snapshot_funnel", noop)
    monkeypatch.setattr(rlc, "compute_market_regime",
                        lambda candidates: SimpleNamespace(regime_ok=True))
    # Fail the optional DB passes instead of touching a real database; every
    # one of them is fail-soft by contract.
    monkeypatch.setattr(rlc.db, "get_db",
                        lambda: (_ for _ in ()).throw(RuntimeError("db off")))

    candidate = Candidate(
        symbol="STOP", mint_address=MINT, price_usd=1.0,
        liquidity_usd=100_000.0, volume_24h_usd=100_000.0,
        market_cap_usd=1_000_000.0, decimals=6,
    )

    async def fake_read(provider):
        return [candidate]

    async def fake_enrich(candidates):
        return None

    async def fake_gate(candidate, portfolio, regime, rules):
        return SimpleNamespace(all_passed=True, rules=[],
                               not_evaluated_rule_ids=[])

    async def fake_think(candidate, thinker, memory_line=""):
        return SimpleNamespace(wants_entry=True, thesis="stop test",
                               invalidation="", verdict="buy",
                               source="test", break_taking=False)

    async def fake_apply_break(think):
        return False

    monkeypatch.setattr(decision_pipeline, "read_candidates", fake_read)
    monkeypatch.setattr(decision_pipeline, "enrich_candidates", fake_enrich)
    monkeypatch.setattr(decision_pipeline, "gate_candidate_staged", fake_gate)
    monkeypatch.setattr(decision_pipeline, "think_candidate", fake_think)
    monkeypatch.setattr(decision_pipeline, "apply_break", fake_apply_break)
    monkeypatch.setattr(decision_pipeline, "drain_social_usages", lambda: [])

    from data_providers import crowd
    monkeypatch.setattr(crowd, "publish_chain_status", lambda: None)

    async def must_not_place_order(**kwargs):
        raise AssertionError(
            f"place_order was called after shutdown was requested: {kwargs}")

    monkeypatch.setattr(rlc, "place_order", must_not_place_order)

    return SimpleNamespace(provider=object(), thinker=StubThinker())


@pytest.mark.parametrize("mode", ["explicit", "default"])
async def test_set_stop_skips_entry_and_logs(
        stopped_cycle_env, caplog, mode):
    env = stopped_cycle_env
    caplog.set_level(logging.INFO, logger=rlc.log.name)

    if mode == "explicit":
        stop = asyncio.Event()
        stop.set()
        outcome = await rlc.run_cycle(
            provider=env.provider, thinker=env.thinker, stop=stop)
    else:
        # No `stop=` argument: the process-wide event the signal handler sets
        # must be honoured by the default path too.
        rlc._STOP_EVENT.set()
        try:
            outcome = await rlc.run_cycle(
                provider=env.provider, thinker=env.thinker)
        finally:
            rlc._STOP_EVENT.clear()

    assert outcome["entries"] == [
        {"symbol": "STOP", "status": "skipped", "reason": "shutting down"}]
    assert "entry skipped: shutting down" in caplog.text


def test_stop_check_is_before_place_order_in_the_entry_path():
    """Static backstop for the behavioural test above."""
    src = __import__("inspect").getsource(rlc.run_cycle)
    assert "stop_event = stop if stop is not None else _STOP_EVENT" in src
    assert src.index("stop_event.is_set()") < src.index("await place_order(")
