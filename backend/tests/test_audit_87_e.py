"""
§87 Phase E — four small correctness fixes.

E1 perf_report flag-outcome section actually reads feed rows (the `db`
   NameError was swallowed, so it was always empty).
E2 a zero/missing token quantity is never fabricated as 1.0: the position is
   skipped for pricing/exits, alerted once, and the close mirror refuses to
   invent an exit price.
E3 `_live_portfolio` and `_journal_meta` share one aggregation, and a
   multi-row position's entry price is cost/tokens (not the first row's).
E4 an unreadable wallet/balance does not re-run the wallet-drop check twice.
"""
from __future__ import annotations

import json
import types

import pytest

import api.db as api_db
import run_live_cycle as rlc
from live_execution.models import ExecutionLedger
from scripts import perf_report as pf

MINT = "MintE2E2E2E2E2E2E2E2E2E2E2E2E2E2E2E2E"


class _CM:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


# ------------------------------------------------------------------ E1 ----

async def test_e1_flag_outcomes_read_feed_rows(tmp_path, monkeypatch):
    ledger = tmp_path / "executions.json"
    ledger.write_text(json.dumps({"records": [
        {"kind": "buy", "mint": MINT, "usd_size": 10.0, "ts": 100.0},
        {"kind": "close", "mint": MINT, "pnl_usd": 2.0, "ts": 200.0},
    ]}))
    monkeypatch.setattr(pf, "LEDGER_PATH", ledger)

    async def feed(conn, limit=2000):
        return [{"mint_address": MINT, "risk_flags": ["high_vol"]}]

    monkeypatch.setattr(api_db, "get_db", lambda: _CM())
    monkeypatch.setattr(api_db, "get_feed_events", feed)

    out = await pf.shadow_risk_flag_stats()

    assert "high_vol" in out, "feed rows must reach the flag-outcome section"
    assert out["high_vol"]["n"] == 1
    assert out["high_vol"]["win_rate"] == 1.0
    assert out["high_vol"]["avg_pnl_pct"] == pytest.approx(20.0)


async def test_e1_unreadable_feed_logs_and_returns_empty(tmp_path, monkeypatch,
                                                        caplog):
    ledger = tmp_path / "executions.json"
    ledger.write_text(json.dumps({"records": [
        {"kind": "buy", "mint": MINT, "usd_size": 10.0, "ts": 100.0},
        {"kind": "close", "mint": MINT, "pnl_usd": 2.0, "ts": 200.0},
    ]}))
    monkeypatch.setattr(pf, "LEDGER_PATH", ledger)

    async def feed(conn, limit=2000):
        raise RuntimeError("journal down")

    monkeypatch.setattr(api_db, "get_db", lambda: _CM())
    monkeypatch.setattr(api_db, "get_feed_events", feed)

    out = await pf.shadow_risk_flag_stats()

    assert out == {"untracked": {"n": 1, "win_rate": 1.0,
                                 "avg_pnl_pct": pytest.approx(20.0)}}
    assert any("flag outcomes empty" in r.message for r in caplog.records)


# ------------------------------------------------------------------ E2 ----

@pytest.fixture
def e2_env(tmp_path, monkeypatch):
    monkeypatch.setattr(rlc.live_config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(rlc, "_unresolved_qty_alerted", set())
    sent: list = []
    monkeypatch.setattr(
        "alerts.alert_nowait",
        lambda event, msg, key=None: sent.append((event, msg)))
    monkeypatch.setattr(rlc.wallet, "load_keypair", lambda: object())
    monkeypatch.setattr(rlc.wallet, "pubkey_string", lambda p: "OWNER")

    async def usdc(addr):
        return 100.0

    async def sol(addr):
        return 1.0

    async def reconcile(meta, ledger=None):
        return {"checked": True, "discrepancies": []}

    monkeypatch.setattr(rlc.solana, "get_usdc_balance", usdc)
    monkeypatch.setattr(rlc.solana, "get_sol_balance", sol)
    monkeypatch.setattr(rlc, "_reconcile_with_chain", reconcile)
    return tmp_path, sent


async def test_e2_zero_quantity_is_skipped_and_alerted_once(e2_env):
    tmp_path, sent = e2_env
    ledger = ExecutionLedger(tmp_path / "exec.json")
    ledger.record_buy("k-zero", MINT, 1.0, 0.0, 1.0, "sig")
    ledger.record_buy("k-real", "MintREAL", 2.0, 100.0, 0.02, "sig2")

    portfolio, meta, _ = await rlc._live_portfolio(ledger)

    assert [t.mint_address for t in portfolio.open_positions] == ["MintREAL"]
    assert MINT in meta                       # the row exists...
    assert all(t.quantity > 0 for t in portfolio.open_positions)
    assert len(sent) == 1 and "Unresolved quantity" in sent[0][1]

    # A second pass must not spam the same alert.
    await rlc._live_portfolio(ledger)
    assert len(sent) == 1


async def test_e2_scanner_never_prices_an_unknown_quantity(monkeypatch):
    calls: list = []

    async def priced(mint):
        calls.append(mint)
        return 6

    monkeypatch.setattr(rlc, "_cached_decimals", priced)
    monkeypatch.setattr(rlc, "_unresolved_qty_alerted", set())
    monkeypatch.setattr("alerts.alert_nowait", lambda *a, **k: None)
    m = {"price_usd": 0.02, "tokens": 0.0, "cost": 1.0, "opened_ts": 0.0}

    await rlc._scan_exits_for_position(object(), object(), {}, MINT, m)

    assert calls == [], "a zero-token row must never be priced or exit-checked"


async def test_e2_mirror_close_accepts_unknown_price(monkeypatch):
    captured = {}

    async def open_trades(conn):
        return [types.SimpleNamespace(mint_address=MINT, trade_id="t1",
                                      position_size_usd=2.0)]

    async def close(conn, **kw):
        captured.update(kw)

    monkeypatch.setattr(rlc.db, "get_db", lambda: _CM())
    monkeypatch.setattr(rlc.db, "get_open_trades", open_trades)
    monkeypatch.setattr(rlc.db, "close_trade_row", close)

    await rlc._mirror_live_close(MINT, exit_price_usd=None,
                                 pnl_usd=-0.5, rule_id="exit_stop_loss")

    assert captured["exit_price_usd"] is None
    assert captured["realized_pnl_usd"] == -0.5


# ------------------------------------------------------------------ E3 ----

def test_e3_multi_row_entry_price_is_cost_over_tokens(tmp_path):
    ledger = ExecutionLedger(tmp_path / "exec.json")
    ledger.record_buy("k1", MINT, 10.0, 100.0, 0.10, "s1")
    ledger.record_buy("k2", MINT, 30.0, 200.0, 0.15, "s2")

    meta = rlc._book_meta_from_ledger(ledger)

    assert meta[MINT]["tokens"] == pytest.approx(300.0)
    assert meta[MINT]["cost"] == pytest.approx(40.0)
    assert meta[MINT]["price_usd"] == pytest.approx(40.0 / 300.0)
    assert meta[MINT]["opened_ts"] == pytest.approx(
        ledger._load()[0]["ts"])


def test_e3_journal_meta_uses_the_same_aggregation(tmp_path):
    ledger = ExecutionLedger(tmp_path / "exec.json")
    ledger.record_buy("k1", MINT, 10.0, 100.0, 0.10, "s1")
    ledger.record_buy("k2", MINT, 30.0, 200.0, 0.15, "s2")

    meta = rlc._journal_meta(ledger, {MINT: {"chain_tokens": 250.0}})

    assert meta[MINT]["price_usd"] == pytest.approx(40.0 / 300.0)
    assert meta[MINT]["chain_tokens"] == 250.0


# ------------------------------------------------------------------ E4 ----

async def test_e4_wallet_failure_does_not_recheck_wallet_drop(
        tmp_path, monkeypatch):
    ledger = ExecutionLedger(tmp_path / "exec.json")
    monkeypatch.setattr(rlc.live_config, "STATE_DIR", tmp_path)
    calls: list = []

    def no_wallet():
        raise RuntimeError("no keypair")

    monkeypatch.setattr(rlc.wallet, "load_keypair", no_wallet)

    from live_execution import reconcile
    monkeypatch.setattr(reconcile, "check_wallet_drop",
                        lambda *a, **k: calls.append((a, k)))

    await rlc._live_portfolio(ledger)

    # The inner block never ran (wallet load failed); the removed outer
    # re-check must not run either. Balances are unknown → skip, not fake.
    assert calls == []
