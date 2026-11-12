"""§94 S1: positive evidence releases dropped exits without new authority."""
from unittest.mock import AsyncMock

import pytest

from live_execution import executor as ex, solana
from live_execution.commit_log import CommitLog
from live_execution.models import ExecutionLedger
from tests.test_security_sell_recovery import MINT, SUCCESS, sell_env


@pytest.fixture
def dropped(sell_env, monkeypatch):
    ledger, commits = sell_env
    clock = [1000.0]
    commits.now_fn = lambda: clock[0]
    monkeypatch.setattr(ex, "CommitLog", lambda path: CommitLog(path, now_fn=lambda: clock[0]))
    monkeypatch.setattr(ex.config, "RPC_URLS", ["https://one.invalid", "https://two.invalid"])
    monkeypatch.setattr(solana, "get_signature_status_all_strict", AsyncMock(return_value=[solana.ABSENT] * 2))
    boundary = AsyncMock(return_value=ex.OrderResult(status="failed", signature="dropped-signature", blockhash="expired-blockhash", reason="not confirmed before timeout", unresolved=True))
    monkeypatch.setattr(ex, "_broadcast_and_confirm", boundary)
    return ledger, commits, clock, boundary


async def _first(dropped):
    ledger, commits, clock, boundary = dropped
    result = await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ledger)
    assert result.unresolved
    assert len(commits.pending_sells(MINT)) == 1
    clock[0] += 1000


def _one_authorization(boundary):
    assert boundary.await_count == 1
    assert ex.memo.publish_commit_memo.await_count == 1
    assert ex._get_json.await_count == 1


async def test_dropped_exit_releases_after_two_observations_and_next_call_sells(dropped):
    ledger, commits, clock, boundary = dropped
    await _first(dropped)
    first = await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ledger)
    assert first.unresolved
    candidate = commits.pending_sells(MINT)[0]["sell_attempt"]["release_candidate_since"]
    assert candidate == clock[0]
    clock[0] += 60
    result = await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ExecutionLedger(ledger.path))
    assert not result.unresolved
    assert "released: all endpoints absent" in result.reason
    assert not commits.pending_sells(MINT)
    history = commits.all()[0]["sell_attempt_history"]
    assert history[0]["status"] == "failed"
    assert "released:" in history[0]["note"]
    _one_authorization(boundary)
    assert all(c.kwargs["commitment"] == "finalized" for c in solana.is_blockhash_valid.call_args_list)
    assert {tuple(c.kwargs["endpoints"]) for c in solana.is_blockhash_valid.call_args_list} == {("https://one.invalid",), ("https://two.invalid",)}
    boundary.return_value = ex.OrderResult(status="filled", signature="new-signature")
    result = await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ledger)
    assert result.status == "filled"
    assert boundary.await_count == 2


@pytest.mark.parametrize("unmet", ["single", "same_host", "same_host_ports", "empty", "short", "unreachable", "malformed", "success_conflict", "error", "valid", "unknown", "zero", "exception", "blockhash_missing", "young", "legacy", "future", "second_too_soon"])
async def test_each_missing_release_fact_holds_without_second_authorization(dropped, monkeypatch, unmet):
    ledger, commits, clock, boundary = dropped
    await _first(dropped)
    if unmet == "single":
        monkeypatch.setattr(ex.config, "RPC_URLS", ["https://one.invalid"])
    elif unmet == "same_host":
        monkeypatch.setattr(ex.config, "RPC_URLS", ["https://one.invalid/a", "https://one.invalid/b"])
    elif unmet == "same_host_ports":
        monkeypatch.setattr(ex.config, "RPC_URLS", ["https://one.invalid:443/a", "https://one.invalid:8899/b"])
    elif unmet in ("empty", "short", "unreachable", "malformed", "success_conflict", "error"):
        failure = dict(SUCCESS, err="AccountNotFound")
        solana.get_signature_status_all_strict.return_value = {
            "empty": [], "short": [solana.ABSENT], "unreachable": [solana.ABSENT, solana.UNREACHABLE],
            "malformed": [solana.ABSENT, {}], "success_conflict": [SUCCESS, failure], "error": [solana.ABSENT, failure],
        }[unmet]
    elif unmet in ("valid", "unknown", "zero"):
        solana.is_blockhash_valid.side_effect = [False, {"valid": True, "unknown": None, "zero": 0}[unmet]] * 8
    elif unmet == "exception":
        solana.is_blockhash_valid.side_effect = TimeoutError("offline fixture")
    elif unmet in ("legacy", "future", "young", "blockhash_missing"):
        rows = commits.all()
        attempt = rows[0]["sell_attempt"]
        if unmet == "legacy":
            attempt.pop("signed_at", None)
        elif unmet == "blockhash_missing":
            attempt["blockhash"] = ""
        else:
            attempt["signed_at"] = clock[0] + (1000 if unmet == "future" else -1)
        commits._save(rows)
    for _ in range(3):
        result = await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ExecutionLedger(ledger.path))
        assert result.unresolved
        _one_authorization(boundary)
        if unmet != "second_too_soon":
            clock[0] += 60
    assert commits.pending_sells(MINT)
    assert "resolve_pending_sell.py" in result.reason
    assert "age" in result.reason


async def test_late_success_before_release_is_adopted(dropped):
    ledger, commits, clock, boundary = dropped
    await _first(dropped)
    assert (await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ledger)).unresolved
    clock[0] += 60
    solana.get_signature_status_all_strict.return_value = [solana.ABSENT, SUCCESS]
    result = await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ledger)
    assert result.status == "filled"
    assert not commits.pending_sells(MINT)
    _one_authorization(boundary)


async def test_negative_observation_resets_release_candidate(dropped):
    ledger, commits, clock, boundary = dropped
    await _first(dropped)
    await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ledger)
    clock[0] += 60
    solana.is_blockhash_valid.return_value = None
    assert (await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ledger)).unresolved
    clock[0] += 60
    solana.is_blockhash_valid.return_value = False
    assert (await ex.place_sell(MINT, "DUMMY", 1 / 3, ledger=ledger)).unresolved
    assert commits.pending_sells(MINT)[0]["sell_attempt"]["release_candidate_since"] == clock[0]
    _one_authorization(boundary)


@pytest.mark.parametrize("value,expected", [("0", 600), ("-1", 600), ("garbage", 600), ("nan", 600), ("inf", 600), ("1", 300), ("301", 301), ("900", 900)])
def test_release_age_configuration_is_conservative(value, expected):
    from live_execution.config import _sell_attempt_release_min_age
    assert _sell_attempt_release_min_age(value) == expected


async def test_blockhash_commitment_is_explicit_and_default_unchanged(monkeypatch):
    rpc = AsyncMock(return_value={"value": False})
    monkeypatch.setattr(solana, "rpc", rpc)
    assert await solana.is_blockhash_valid("dummy") is False
    assert rpc.call_args.args[1][1]["commitment"] == "confirmed"
    assert await solana.is_blockhash_valid("dummy", commitment="finalized") is False
    assert rpc.call_args.args[1][1]["commitment"] == "finalized"
