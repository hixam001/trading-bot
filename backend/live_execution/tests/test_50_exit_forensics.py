"""
tests/test_50_exit_forensics.py — §50 Phase 0: exit-rule forensics + live sell gate.

Pins the §50 Phase-0 contracts:
  1. ExecutionRecord.rule_id roundtrip — reduce_position/close_out_of_band
     store the exit rule on the close record; pre-§50 rows load with None.
  2. tranches_taken — the tranche counter derived from the ledger itself
     (kills the latent re-trim-every-60s bug on the live path).
  3. sell_risk_gate min_clip_usd override — the LIVE book floors clips at
     the §45 equity-proportional live ticket instead of paper's $25, while
     the default path stays bit-identical for every paper expectation.
  4. last_close_ts / closes_since — the sell-gate's cooldown + ceiling
     inputs, computed from the ledger.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from live_execution.models import ExecutionLedger, ExecutionRecord
from rule_engine.exits import ExitDecision, sell_risk_gate


class Clock:
    """Auto-advancing: every record gets a distinct timestamp (a frozen clock
    would stamp a NEW buy and OLD closes identically, making order-based
    assertions meaningless — real ts always advances)."""

    def __init__(self, start: float = 1_000_000.0, step: float = 1.0):
        self.now = start
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _buy(ledger, key="b1", mint="MINT", cost=0.50, tokens=100.0):
    ledger.record_buy(key, mint, cost, tokens, cost / tokens, "sig",
                      status="confirmed")


# ---------------------------------------------------------------------------


def test_reduce_position_stores_rule_id(tmp_path):
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=Clock())
    _buy(ledger)
    rec = ledger.reduce_position("MINT", 0.33, 0.20, rule_id="exit_take_profit")
    assert rec.rule_id == "exit_take_profit"
    row = [r for r in ledger._load() if r["kind"] == "close"][-1]
    assert row["rule_id"] == "exit_take_profit"
    # and the from_json roundtrip keeps it
    assert ExecutionRecord.from_json(row).rule_id == "exit_take_profit"


def test_pre_50_rows_load_with_none_rule_id(tmp_path):
    """A hand-written pre-§50 close row (no rule_id key) must load, not crash."""
    import json as _json
    p = tmp_path / "exec.json"
    p.write_text(_json.dumps({"records": [
        {"kind": "close", "idempotency_key": "close-legacy", "mint": "MINT",
         "usd_size": 0.5, "tokens_out": 100.0, "price_usd": 0.0,
         "signature": "", "status": "closed", "ts": 1.0, "pnl_usd": -0.1},
    ]}))
    ledger = ExecutionLedger(p)
    rec = ExecutionRecord.from_json(ledger._load()[0])
    assert rec.rule_id is None


def test_out_of_band_close_carries_outofband_rule_id(tmp_path):
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=Clock())
    _buy(ledger)
    ledger.close_out_of_band("MINT", proceeds_usd=0.20, note="manual")
    closes = [r for r in ledger._load() if r["kind"] == "close"]
    assert closes[-1]["rule_id"] == "outofband"


def test_tranches_taken_counts_tp_closes_since_current_open_buy(tmp_path):
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=Clock())
    # position 1: open, one TP trim already taken against it
    _buy(ledger, "b1", cost=0.50)
    ledger.reduce_position("MINT", 0.33, 0.20, rule_id="exit_take_profit")
    assert ledger.tranches_taken("MINT") == 1
    # second TP tranche -> 2
    ledger.reduce_position("MINT", 0.33, 0.15, rule_id="exit_take_profit")
    assert ledger.tranches_taken("MINT") == 2
    # a full stop-loss close does NOT add a tranche; nothing open afterwards
    ledger.reduce_position("MINT", 1.0, 0.10, full_close=True,
                           rule_id="exit_stop_loss")
    assert ledger.tranches_taken("MINT") == 0
    # position 2 (a NEW open buy): history must reset — old TP closes are
    # older than the new buy's ts
    _buy(ledger, "b2", cost=0.40)
    assert ledger.tranches_taken("MINT") == 0


def test_last_close_ts_and_closes_since(tmp_path):
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=Clock(1_000_000.0))
    _buy(ledger, "b1")
    ledger.reduce_position("MINT", 1.0, 0.30, full_close=True,
                           rule_id="exit_stop_loss")
    close_ts = ledger.last_close_ts("MINT")
    assert close_ts is not None and close_ts > 1_000_000.0
    assert ledger.last_close_ts("NOPE") is None
    assert ledger.closes_since(999_999.0) == 1
    assert ledger.closes_since(close_ts + 0.5) == 0



# --- sell gate: live min-clip override ------------------------------------------


def _trim(value_usd: float) -> ExitDecision:
    return ExitDecision("exit_take_profit", "trim", 0.33, "test trim")


def test_sell_gate_default_clip_is_paper_25():
    """Default path: bit-identical paper behavior — a $0.17 trim is held."""
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    gated, note = sell_risk_gate(_trim(0.17), 0.17, None, 0, now)
    assert gated.action == "hold"
    assert note == "min clip"


def test_sell_gate_live_clip_override_passes_small_trim():
    """§50: the live book floors at the equity-proportional ticket instead —
    a $0.17 trim on a $0.50 book ($0.10 floor) is a legitimate clip."""
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    gated, note = sell_risk_gate(_trim(0.17), 0.17, None, 0, now,
                                 min_clip_usd=0.10)
    assert gated.action == "trim"
    assert note == ""


def test_sell_gate_live_clip_still_holds_dust():
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    gated, note = sell_risk_gate(_trim(0.05), 0.05, None, 0, now,
                                 min_clip_usd=0.10)
    assert gated.action == "hold"
    assert note == "min clip"


def test_sell_gate_risk_off_bypasses_even_live_clip():
    stop = ExitDecision("exit_stop_loss", "close_full", 1.0, "hard stop")
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    gated, note = sell_risk_gate(stop, 0.05, now - timedelta(minutes=1),
                                 99, now, min_clip_usd=0.10)
    assert gated.action == "close_full"
    assert "bypass" in note


def test_sell_gate_cooldown_uses_last_exit_regardless_of_clip():
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    recent = now - timedelta(minutes=10)
    gated, note = sell_risk_gate(_trim(5.0), 5.0, recent, 0, now,
                                 min_clip_usd=0.10)
    assert gated.action == "hold"
    assert "cooldown" in note


# --- live _manage wiring: source-pinned ------------------------------------------


def test_manage_feeds_tranches_and_gates_and_passes_rule_id():
    """§50 Phase 0 wiring contract on the live exit path, source-pinned like
    test_pipeline_parity: _manage derives tranches_taken from the ledger,
    applies sell_risk_gate with the §45 live clip floor, and threads the
    exit rule through place_order into the ledger's close records."""
    import inspect
    import run_live_cycle as rlc

    src = inspect.getsource(rlc._manage)
    # tranche counter from the ledger, not a hardcoded 0
    assert "tranches = ledger.tranches_taken(mint)" in src
    assert "tranches_taken=tranches," in src
    # the sell gate with the live clip override
    assert "sell_risk_gate(" in src
    assert "min_clip_usd=min_clip," in src
    assert "live_config.min_live_ticket_usd(" in src
    # a held gate decision stops the sell (never falls through to place_order)
    assert 'if gated.action == "hold":' in src
    # the exit rule reaches the executor -> the ledger close record
    assert "rule_id=decision.rule_id," in src


def test_place_sell_signature_carries_rule_id():
    import inspect
    from live_execution import executor

    assert "rule_id" in inspect.signature(executor.place_sell).parameters
    assert "rule_id" in inspect.signature(executor.place_order).parameters
    assert "rule_id" in inspect.signature(
        ExecutionLedger.reduce_position).parameters
