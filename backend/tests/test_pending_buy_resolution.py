"""
§87 C: resolving buys whose send outcome was unknown.

`resolve_pending_buys` must:
  * adopt a confirmed late land with its ACTUAL fill amounts and journal it,
  * fall back to the stored quote snapshot only when actuals are unreadable,
  * leave the row pending when cost/token amounts are unknowable (never
    fabricate a basis),
  * mark a landed error failed (fee paid),
  * expire ONLY when the blockhash is provably invalid (False, not None),
  * leave unknown/None status and unknown blockhash pending, with a 5-minute
    unresolved alert.

Hermetic: no network, mocked wallet/RPC/commit log/DB/alerts.
"""
from __future__ import annotations

import types

import pytest

import alerts
import run_live_cycle as rlc
from live_execution import solana
from live_execution.commit_log import CommitLog
from live_execution.models import ExecutionLedger


class _FakeCommitLog:
    instances: list["_FakeCommitLog"] = []

    def __init__(self, path):
        self.path = path
        self.binds: list[tuple[str, str]] = []
        self.bind_allow_failed: list[bool] = []
        self.fails: list[tuple[str, str]] = []
        _FakeCommitLog.instances.append(self)

    def bind(self, commit_hash, signature, allow_failed=False):
        self.binds.append((commit_hash, signature))
        self.bind_allow_failed.append(allow_failed)

    def fail(self, commit_hash, reason):
        self.fails.append((commit_hash, reason))


class _Conn:
    pass


class _CM:
    async def __aenter__(self):
        return _Conn()

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def env(tmp_path, monkeypatch):
    _FakeCommitLog.instances = []
    journaled: list[dict] = []
    alerts_sent: list[tuple] = []

    monkeypatch.setattr(rlc, "CommitLog", _FakeCommitLog)
    monkeypatch.setattr(rlc, "db", types.SimpleNamespace(get_db=lambda: _CM()))
    monkeypatch.setattr(rlc.wallet, "load_keypair", lambda: object())
    monkeypatch.setattr(rlc.wallet, "pubkey_string", lambda payer: "OWNER")

    async def fake_journal(conn, **kw):
        journaled.append(kw)

    monkeypatch.setattr(rlc, "_journal_live_commit", fake_journal)
    monkeypatch.setattr(
        alerts, "alert_nowait",
        lambda event, msg, key=None: alerts_sent.append((event, msg)))

    state = types.SimpleNamespace(
        dir=tmp_path, journaled=journaled, alerts=alerts_sent,
        ledger=ExecutionLedger(tmp_path / "exec.json"))
    return state


def _pending(ledger, **kw):
    args = dict(
        idempotency_key="k1",
        mint="MINTAAA",
        usd_size=2.5,
        tokens_out=10.0,
        price_usd=0.25,
        signature="SIG1",
        blockhash="BH1",
        commit_hash="HASH1",
        commit_nonce="NONCE1",
        commit_payload={"kind": "buy"},
        commit_sealed_at=1_699_999_900.0,
        memo_signature="MEMO1",
        memo_slot=7,
        reason="send outcome unresolved",
    )
    args.update(kw)
    return ledger.record_pending_buy(**args)


async def test_confirmed_late_buy_is_adopted_with_actuals(env, monkeypatch):
    ledger = env.ledger
    _pending(ledger)

    async def status(sig, endpoints=None):
        return {"slot": 9, "err": None, "confirmationStatus": "confirmed"}

    async def actuals(side, mint, sig, owner=None):
        assert side == "buy" and mint == "MINTAAA" and sig == "SIG1"
        return {"usdc_delta": 2.7, "tokens_delta": 9.0}

    monkeypatch.setattr(rlc.solana, "get_signature_status", status)
    monkeypatch.setattr(rlc, "_fill_actuals", actuals)

    stats = await rlc.resolve_pending_buys(ledger)

    assert stats == {"adopted": 1, "failed": 0, "expired": 0, "pending": 0}
    rec = ledger.get_by_idempotency_key("k1")
    assert rec.status == "confirmed"
    assert rec.usd_size == pytest.approx(2.7)
    assert rec.tokens_out == pytest.approx(9.0)
    assert rec.price_usd == pytest.approx(2.7 / 9.0)
    # Actuals win, and the position is now fully managed.
    assert ledger.open_token_amounts() == {"MINTAAA": pytest.approx(9.0)}
    [commit] = _FakeCommitLog.instances
    assert commit.binds == [("HASH1", "SIG1")]
    assert commit.bind_allow_failed == [True]
    [journal] = env.journaled
    assert journal["mint_address"] == "MINTAAA"
    assert journal["verdict"] == "buy" and journal["entry_allowed"] is True
    assert journal["result"].signature == "SIG1"
    assert any("Late buy adopted" in m for _, m in env.alerts)


async def test_late_adoption_rebinds_a_previously_failed_commit(
        env, monkeypatch):
    """§90-C3: expiry marks the commit failed; a later chain-proven fill must
    still be bindable instead of leaving a failed commit beside a managed
    position."""
    log = CommitLog(env.dir / "commits.json")
    sealed = log.seal("buy", {"mint": "MINTAAA", "usd": 2.5})
    assert log.fail(sealed["hash"], "pending buy expired") is True
    ledger = env.ledger
    _pending(ledger, commit_hash=sealed["hash"])

    async def status(sig, endpoints=None):
        return {"slot": 9, "err": None, "confirmationStatus": "confirmed"}

    async def actuals(side, mint, sig, owner=None):
        return {"usdc_delta": 2.7, "tokens_delta": 9.0}

    monkeypatch.setattr(rlc.solana, "get_signature_status", status)
    monkeypatch.setattr(rlc, "_fill_actuals", actuals)
    # Use the real commit log (the env fixture normally fakes it).
    monkeypatch.setattr(rlc, "CommitLog", CommitLog)
    monkeypatch.setattr(rlc.live_config, "STATE_DIR", env.dir)

    stats = await rlc.resolve_pending_buys(ledger)

    assert stats["adopted"] == 1
    row = CommitLog(env.dir / "commits.json").all()[0]
    assert row["status"] == "bound"
    assert row["signature"] == "SIG1"
    assert "fail_reason" not in row


async def test_confirmed_with_unreadable_actuals_uses_quote_snapshot(
        env, monkeypatch):
    ledger = env.ledger
    _pending(ledger)

    async def status(sig, endpoints=None):
        return {"err": None, "confirmationStatus": "finalized"}

    async def no_actuals(side, mint, sig, owner=None):
        raise RuntimeError("tx unreadable")

    monkeypatch.setattr(rlc.solana, "get_signature_status", status)
    monkeypatch.setattr(rlc, "_fill_actuals", no_actuals)

    stats = await rlc.resolve_pending_buys(ledger)

    assert stats["adopted"] == 1
    rec = ledger.get_by_idempotency_key("k1")
    assert rec.status == "confirmed"
    # Documented fallback: the stored quote snapshot, never a guess.
    assert rec.usd_size == pytest.approx(2.5)
    assert rec.tokens_out == pytest.approx(10.0)
    assert rec.price_usd == pytest.approx(0.25)


async def test_confirmed_but_unknowable_amounts_stays_pending(env, monkeypatch):
    ledger = env.ledger
    # A degenerate snapshot (nothing usable) + unreadable actuals.
    _pending(ledger, usd_size=0.0, tokens_out=0.0, price_usd=0.0)

    async def status(sig, endpoints=None):
        return {"err": None, "confirmationStatus": "confirmed"}

    async def no_actuals(side, mint, sig, owner=None):
        return {"usdc_delta": None, "tokens_delta": None}

    monkeypatch.setattr(rlc.solana, "get_signature_status", status)
    monkeypatch.setattr(rlc, "_fill_actuals", no_actuals)

    stats = await rlc.resolve_pending_buys(ledger)

    assert stats["pending"] == 1 and stats["adopted"] == 0
    rec = ledger.get_by_idempotency_key("k1")
    assert rec.status == "pending"
    assert ledger.open_token_amounts() == {}
    assert all(c.binds == [] for c in _FakeCommitLog.instances)
    assert env.journaled == []


async def test_landed_error_is_failed_and_commit_closed(env, monkeypatch):
    ledger = env.ledger
    _pending(ledger)

    async def status(sig, endpoints=None):
        return {"err": {"InstructionError": [3, "Custom"]},
                "confirmationStatus": "confirmed"}

    monkeypatch.setattr(rlc.solana, "get_signature_status", status)

    stats = await rlc.resolve_pending_buys(ledger)

    assert stats == {"adopted": 0, "failed": 1, "expired": 0, "pending": 0}
    rec = ledger.get_by_idempotency_key("k1")
    assert rec.status == "failed" and "landed-failed" in rec.reason
    assert ledger.open_positions() == {}
    [commit] = _FakeCommitLog.instances
    assert commit.fails and commit.fails[0][0] == "HASH1"


async def test_provably_invalid_blockhash_expires_and_releases_caps(
        env, monkeypatch):
    import time as _time
    old = _time.time() - 400.0
    ledger = ExecutionLedger(env.dir / "exec_old.json", now_fn=lambda: old)
    _pending(ledger)
    assert ledger.deployed_today_usd() == 2.5

    async def status(sig, endpoints=None):
        return None

    async def status_all(sig, endpoints=None):
        return [solana.ABSENT, solana.ABSENT]

    async def invalid(blockhash, endpoints=None):
        assert blockhash == "BH1"
        return False

    monkeypatch.setattr(rlc.solana, "get_signature_status", status)
    monkeypatch.setattr(
        rlc.solana, "get_signature_status_all_strict", status_all)
    monkeypatch.setattr(rlc.solana, "is_blockhash_valid", invalid)

    stats = await rlc.resolve_pending_buys(ledger)

    assert stats == {"adopted": 0, "failed": 0, "expired": 1, "pending": 0}
    rec = ledger.get_by_idempotency_key("k1")
    assert rec.status == "expired"
    assert ledger.deployed_today_usd() == 0.0
    assert ledger.open_positions() == {}
    [commit] = _FakeCommitLog.instances
    assert commit.fails and "blockhash" in commit.fails[0][1]


async def test_invalid_blockhash_on_a_young_row_does_not_expire(
        env, monkeypatch):
    """§88 D: one endpoint's invalid blockhash is not enough inside 90 s."""
    ledger = env.ledger
    _pending(ledger)

    async def status(sig, endpoints=None):
        return None

    async def status_all(sig, endpoints=None):
        return [solana.ABSENT, solana.ABSENT]

    async def invalid(blockhash, endpoints=None):
        return False

    monkeypatch.setattr(rlc.solana, "get_signature_status", status)
    monkeypatch.setattr(
        rlc.solana, "get_signature_status_all_strict", status_all)
    monkeypatch.setattr(rlc.solana, "is_blockhash_valid", invalid)

    stats = await rlc.resolve_pending_buys(ledger)

    assert stats == {"adopted": 0, "failed": 0, "expired": 0, "pending": 1}
    assert ledger.get_by_idempotency_key("k1").status == "pending"
    assert ledger.deployed_today_usd() == 2.5


async def test_invalid_blockhash_recheck_adopts_a_lagging_endpoint_fill(
        env, monkeypatch):
    """The first endpoint says not-found; the re-query finds the landed fill."""
    ledger = env.ledger
    _pending(ledger)

    async def status(sig, endpoints=None):
        return None

    async def status_all(sig, endpoints=None):
        return [solana.ABSENT,
                {"err": None, "confirmationStatus": "confirmed"}]

    async def invalid(blockhash, endpoints=None):
        return False

    async def actuals(side, mint, sig, owner=None):
        return {"usdc_delta": 2.7, "tokens_delta": 9.0}

    monkeypatch.setattr(rlc.solana, "get_signature_status", status)
    monkeypatch.setattr(
        rlc.solana, "get_signature_status_all_strict", status_all)
    monkeypatch.setattr(rlc.solana, "is_blockhash_valid", invalid)
    monkeypatch.setattr(rlc, "_fill_actuals", actuals)

    stats = await rlc.resolve_pending_buys(ledger)

    assert stats == {"adopted": 1, "failed": 0, "expired": 0, "pending": 0}
    rec = ledger.get_by_idempotency_key("k1")
    assert rec.status == "confirmed"
    assert rec.tokens_out == pytest.approx(9.0)
    assert ledger.open_token_amounts() == {"MINTAAA": pytest.approx(9.0)}


async def test_invalid_blockhash_with_failed_recheck_stays_pending(
        env, monkeypatch):
    """An unreadable multi-endpoint re-query is unknown, never expired."""
    import time as _time
    old = _time.time() - 400.0
    ledger = ExecutionLedger(env.dir / "exec_old.json", now_fn=lambda: old)
    _pending(ledger)

    async def status(sig, endpoints=None):
        return None

    async def status_all(sig, endpoints=None):
        raise RuntimeError("all endpoints down")

    async def invalid(blockhash, endpoints=None):
        return False

    monkeypatch.setattr(rlc.solana, "get_signature_status", status)
    monkeypatch.setattr(
        rlc.solana, "get_signature_status_all_strict", status_all)
    monkeypatch.setattr(rlc.solana, "is_blockhash_valid", invalid)

    stats = await rlc.resolve_pending_buys(ledger)

    assert stats == {"adopted": 0, "failed": 0, "expired": 0, "pending": 1}
    assert ledger.get_by_idempotency_key("k1").status == "pending"


def _patch_status_all(monkeypatch, statuses):
    """Patch the strict helper for §91 tests.

    The legacy helper is also stubbed to its old collapsed semantics
    (`None` for absent OR unreachable) so a pre-fix cycle still runs without
    network and fails on the classification under test, not on a socket.
    """
    async def strict(sig, endpoints=None):
        return list(statuses)

    async def legacy(sig, endpoints=None):
        return [s if isinstance(s, dict) else None for s in statuses]

    monkeypatch.setattr(
        rlc.solana, "get_signature_status_all_strict", strict)
    monkeypatch.setattr(rlc.solana, "get_signature_status_all", legacy)


@pytest.mark.parametrize(
    "statuses",
    [
        [solana.ABSENT, solana.UNREACHABLE],
        [solana.UNREACHABLE, solana.UNREACHABLE],
    ],
    ids=["one-unreachable", "all-unreachable"],
)
async def test_invalid_blockhash_with_an_unknown_endpoint_stays_pending(
        env, monkeypatch, statuses):
    """Absence is proven only when EVERY endpoint answered 200+null."""
    import time as _time
    old = _time.time() - 400.0
    ledger = ExecutionLedger(env.dir / "exec_unreachable.json",
                             now_fn=lambda: old)
    _pending(ledger)

    async def status(sig, endpoints=None):
        return None

    async def invalid(blockhash, endpoints=None):
        return False

    monkeypatch.setattr(rlc.solana, "get_signature_status", status)
    monkeypatch.setattr(rlc.solana, "is_blockhash_valid", invalid)
    _patch_status_all(monkeypatch, statuses)

    stats = await rlc.resolve_pending_buys(ledger)

    assert stats == {"adopted": 0, "failed": 0, "expired": 0, "pending": 1}
    assert ledger.get_by_idempotency_key("k1").status == "pending"
    assert ledger.deployed_today_usd() == 2.5


async def test_invalid_blockhash_with_all_endpoints_absent_expires(
        env, monkeypatch):
    import time as _time
    old = _time.time() - 400.0
    ledger = ExecutionLedger(env.dir / "exec_absent.json", now_fn=lambda: old)
    _pending(ledger)

    async def status(sig, endpoints=None):
        return None

    async def invalid(blockhash, endpoints=None):
        return False

    monkeypatch.setattr(rlc.solana, "get_signature_status", status)
    monkeypatch.setattr(rlc.solana, "is_blockhash_valid", invalid)
    _patch_status_all(monkeypatch, [solana.ABSENT, solana.ABSENT])

    stats = await rlc.resolve_pending_buys(ledger)

    assert stats == {"adopted": 0, "failed": 0, "expired": 1, "pending": 0}
    assert ledger.get_by_idempotency_key("k1").status == "expired"
    assert ledger.deployed_today_usd() == 0.0


async def test_invalid_blockhash_with_absent_and_found_status_adopts(
        env, monkeypatch):
    ledger = env.ledger
    _pending(ledger)

    async def status(sig, endpoints=None):
        return None

    async def invalid(blockhash, endpoints=None):
        return False

    async def actuals(side, mint, sig, owner=None):
        return {"usdc_delta": 2.7, "tokens_delta": 9.0}

    monkeypatch.setattr(rlc.solana, "get_signature_status", status)
    monkeypatch.setattr(rlc.solana, "is_blockhash_valid", invalid)
    monkeypatch.setattr(rlc, "_fill_actuals", actuals)
    _patch_status_all(
        monkeypatch,
        [solana.ABSENT,
         {"err": None, "confirmationStatus": "confirmed"}],
    )

    stats = await rlc.resolve_pending_buys(ledger)

    assert stats == {"adopted": 1, "failed": 0, "expired": 0, "pending": 0}
    rec = ledger.get_by_idempotency_key("k1")
    assert rec.status == "confirmed"
    assert rec.tokens_out == pytest.approx(9.0)


async def test_unknown_blockhash_never_expires_and_alerts_after_5min(
        env, monkeypatch):
    import time as _time
    old = _time.time() - 400.0
    ledger = ExecutionLedger(env.dir / "exec_old.json", now_fn=lambda: old)
    _pending(ledger)

    async def status(sig, endpoints=None):
        return None

    async def unknown(blockhash, endpoints=None):
        return None

    status_all_calls: list[str] = []

    async def status_all(sig, endpoints=None):
        status_all_calls.append(sig)
        return [solana.ABSENT, solana.ABSENT]

    monkeypatch.setattr(rlc.solana, "get_signature_status", status)
    monkeypatch.setattr(rlc.solana, "is_blockhash_valid", unknown)
    monkeypatch.setattr(
        rlc.solana, "get_signature_status_all_strict", status_all)

    stats = await rlc.resolve_pending_buys(ledger)

    assert stats == {"adopted": 0, "failed": 0, "expired": 0, "pending": 1}
    # Unknown validity must never even reach the expiry re-query: every
    # endpoint saying ABSENT cannot make an unknown blockhash provably invalid.
    assert status_all_calls == []
    rec = ledger.get_by_idempotency_key("k1")
    assert rec.status == "pending"       # unknown is never "expired"
    assert rec.blockhash == "BH1"
    assert any("still unresolved after" in m for _, m in env.alerts)
    assert all(c.fails == [] for c in _FakeCommitLog.instances)


async def test_unreadable_status_with_unknown_blockhash_stays_pending(
        env, monkeypatch):
    ledger = env.ledger
    _pending(ledger)

    async def status(sig, endpoints=None):
        raise RuntimeError("rpc down")

    async def unknown(blockhash, endpoints=None):
        return None

    monkeypatch.setattr(rlc.solana, "get_signature_status", status)
    monkeypatch.setattr(rlc.solana, "is_blockhash_valid", unknown)

    stats = await rlc.resolve_pending_buys(ledger)

    assert stats["pending"] == 1
    assert ledger.get_by_idempotency_key("k1").status == "pending"


async def test_no_pending_is_a_noop(env):
    stats = await rlc.resolve_pending_buys(env.ledger)
    assert stats == {"adopted": 0, "failed": 0, "expired": 0, "pending": 0}
    assert _FakeCommitLog.instances == []
