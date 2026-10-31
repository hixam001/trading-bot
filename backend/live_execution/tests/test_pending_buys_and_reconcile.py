"""
§87 C: pending-buy ledger semantics + reconcile treatment.

A buy whose send outcome is unknown is journaled `pending`:
  * it is NOT an open position (no tokens / cost basis to price or exit),
  * but it DOES hold a slot and count against exposure / the daily deploy cap,
  * a provably expired pending buy releases its daily-deploy cost,
  * settling is one-way and idempotent,
  * reconcile treats an on-chain balance covered by a pending buy as
    explained, and a truly unjournaled holding raises a rate-limited alert.
"""
from __future__ import annotations

from live_execution import reconcile as reconcile_mod
from live_execution.models import ExecutionLedger
from live_execution.reconcile import reconcile


def _ledger(tmp_path, now=1_700_000_000.0):
    return ExecutionLedger(tmp_path / "exec.json", now_fn=lambda: now)


def _pending(ledger, **kw):
    args = dict(
        idempotency_key="k-pending",
        mint="MINTAAA",
        usd_size=2.5,
        tokens_out=10.0,
        price_usd=0.25,
        signature="SIGPENDING",
        blockhash="BH1",
    )
    args.update(kw)
    return ledger.record_pending_buy(**args)


def test_pending_counts_caps_but_not_positions(tmp_path):
    ledger = _ledger(tmp_path)
    _pending(ledger)

    # No tokens / cost basis: the exit engine must never see this row.
    assert ledger.open_token_amounts() == {}
    assert "MINTAAA" not in ledger._OPEN  # status is not open
    # But the slot/exposure/daily cap ARE committed.
    assert ledger.open_positions() == {"MINTAAA": 2.5}
    assert ledger.total_open_exposure() == 2.5
    assert ledger.deployed_today_usd() == 2.5


def test_expired_pending_releases_daily_deploy(tmp_path):
    ledger = _ledger(tmp_path)
    _pending(ledger)
    assert ledger.deployed_today_usd() == 2.5

    assert ledger.settle_pending_buy("k-pending", "expired",
                                     reason="blockhash invalid") is True
    # Provably unlandable: the daily cap no longer counts it. It is not an
    # open position either (it never was).
    assert ledger.deployed_today_usd() == 0.0
    assert ledger.open_positions() == {}
    assert ledger.pending_buys() == []


def test_settle_is_one_way_and_idempotent(tmp_path):
    ledger = _ledger(tmp_path)
    _pending(ledger)

    assert ledger.settle_pending_buy(
        "k-pending", "confirmed", usd_size=2.7, tokens_out=9.0,
        price_usd=0.3) is True
    # A second settle (another cycle / operator) must not overwrite it.
    assert ledger.settle_pending_buy("k-pending", "failed") is False
    rec = ledger.get_by_idempotency_key("k-pending")
    assert rec.status == "confirmed"
    assert rec.usd_size == 2.7 and rec.tokens_out == 9.0
    # Confirmed is a normal open position now.
    assert ledger.open_token_amounts() == {"MINTAAA": 9.0}
    assert ledger.open_positions() == {"MINTAAA": 2.7}


def test_settle_unknown_key_returns_false(tmp_path):
    ledger = _ledger(tmp_path)
    _pending(ledger)
    assert ledger.settle_pending_buy("nope", "confirmed") is False
    assert len(ledger.pending_buys()) == 1


def test_reconcile_explains_balance_covered_by_pending_buy(monkeypatch):
    alerts = []
    monkeypatch.setattr(reconcile_mod, "_unjournaled_alert_last", {})
    monkeypatch.setattr("alerts.alert_nowait",
                        lambda event, msg, key=None: alerts.append((event, msg)))
    meta = {}
    report = reconcile(meta, {"MINTAAA": 10.0}, pending_mints={"MINTAAA"})
    assert report["checked"] is True
    assert report["discrepancies"] == []
    assert alerts == []
    assert "MINTAAA" not in meta


def test_reconcile_unjournaled_holding_alerts_once_per_cooldown(monkeypatch):
    alerts = []
    monkeypatch.setattr(reconcile_mod, "_unjournaled_alert_last", {})
    monkeypatch.setattr("alerts.alert_nowait",
                        lambda event, msg, key=None: alerts.append((event, msg)))
    meta = {}
    report = reconcile(meta, {"MINTBBB": 7.0}, now_fn=lambda: 1000.0)
    [d] = report["discrepancies"]
    assert d["kind"] == "unjournaled"
    assert "MINTBBB" not in meta          # never fabricated into the book
    assert len(alerts) == 1
    assert "Unjournaled on-chain holding" in alerts[0][1]

    # Inside the 900s cooldown: still reported, but not re-enqueued.
    reconcile(meta, {"MINTBBB": 7.0}, now_fn=lambda: 1500.0)
    assert len(alerts) == 1

    # After the cooldown: alerts again.
    reconcile(meta, {"MINTBBB": 7.0}, now_fn=lambda: 1901.0)
    assert len(alerts) == 2
