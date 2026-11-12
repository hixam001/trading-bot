"""§94 S5: offline, read-only pre-upgrade authority validation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _write(state, filename, value):
    path = state / filename
    path.write_text(json.dumps(value))
    return path


def _buy(**extra):
    return dict(dict(kind="buy", idempotency_key="historical-buy", mint="dummy-mint",
                     usd_size=12.0, status="confirmed"), **extra)


def _commit(**extra):
    return dict(dict(kind="sell", hash="dummy-hash", nonce="dummy-nonce",
                     payload={"mint": "dummy-mint"}, status="sealed"), **extra)


@pytest.mark.parametrize("status", ["recorded", "sent", "confirmed", "failed", "closed", "pending", "expired"])
def test_historical_ledger_statuses_and_optional_defaults_load(tmp_path, status):
    from scripts import validate_state as vs
    from live_execution.models import ExecutionLedger

    row = _buy()
    row["status"] = status
    _write(tmp_path, "executions.json", {"records": [row]})
    assert ExecutionLedger(tmp_path / "executions.json")._load() == [row]
    assert vs.main(["--state-dir", str(tmp_path)]) == 0


@pytest.mark.parametrize("extra", [
    {"tokens_out": 3.0, "price_usd": 4.0, "signature": "", "ts": 1.0, "pnl_usd": None},
    {"rule_id": None},
    {"blockhash": "", "commit_hash": "", "commit_nonce": "", "commit_payload": {},
     "commit_sealed_at": None, "memo_signature": "", "memo_slot": None, "reason": ""},
    {"blockhash": "old-blockhash", "commit_hash": "old-hash", "commit_nonce": "old-nonce",
     "commit_payload": {"mint": "dummy-mint"}, "commit_sealed_at": 1.0,
     "memo_signature": "dummy-memo", "memo_slot": 5, "reason": ""},
])
def test_historical_ledger_field_generations_load(tmp_path, extra):
    from scripts import validate_state as vs
    from live_execution.models import ExecutionLedger

    row = _buy(**extra)
    _write(tmp_path, "executions.json", {"records": [row]})
    assert ExecutionLedger(tmp_path / "executions.json")._load() == [row]
    assert vs.main(["--state-dir", str(tmp_path)]) == 0


@pytest.mark.parametrize("extra", [
    {"status": "recorded", "usd_size": 14.0, "pnl_usd": 2.0},
    {"status": "closed", "usd_size": 0.0, "pnl_usd": None, "signature": "",
     "tokens_out": 3.0, "price_usd": 0.0, "note": "out-of-band: operator repair"},
    {"status": "closed", "usd_size": 14.0, "pnl_usd": 2.0, "rule_id": "outofband"},
    {"status": "recorded", "usd_size": 14.0, "pnl_usd": 2.0, "signature": "dummy-sell"},
])
def test_historical_close_generations_load(tmp_path, extra):
    from scripts import validate_state as vs
    from live_execution.models import ExecutionLedger

    row = dict(kind="close", idempotency_key="historical-close", mint="dummy-mint", **extra)
    _write(tmp_path, "ledger.json", {"records": [row]})
    assert ExecutionLedger(tmp_path / "ledger.json")._load() == [row]
    assert vs.main(["--state-dir", str(tmp_path)]) == 0


@pytest.mark.parametrize("status", ["sealed", "published", "bound", "failed"])
@pytest.mark.parametrize("memo_fields", [False, True])
@pytest.mark.parametrize("kind", ["buy", "sell"])
def test_historical_commit_generations_load(tmp_path, status, memo_fields, kind):
    from scripts import validate_state as vs
    from live_execution.commit_log import CommitLog

    row = _commit(signature=None, kind=kind)
    row["status"] = status
    if memo_fields:
        row.update(sealed_at=1.0, memo_signature="dummy-memo", memo_slot=5,
                   memo_published_at=2.0, notes=[{"text": "operator note", "at": 3.0}])
    if status == "failed":
        row["fail_reason"] = "historical rejection"
    _write(tmp_path, "commits.json", [row])
    assert CommitLog(tmp_path / "commits.json")._load() == [row]
    assert vs.main(["--state-dir", str(tmp_path)]) == 0


@pytest.mark.parametrize("status", ["pending", "confirmed", "failed"])
def test_legacy_sell_attempt_without_signed_at_loads(tmp_path, status):
    from scripts import validate_state as vs
    from live_execution.commit_log import CommitLog

    row = _commit(sell_attempt=dict(signature="dummy-sell", blockhash="dummy-blockhash",
                  status=status, fraction=1.0, tokens=3.0, proceeds_usd=14.0,
                  wallet="dummy-wallet", full_close=True))
    _write(tmp_path, "commits.json", [row])
    assert CommitLog(tmp_path / "commits.json")._load() == [row]
    assert vs.main(["--state-dir", str(tmp_path)]) == 0


def test_historical_failed_sell_attempt_history_is_preserved_on_load(tmp_path):
    from scripts import validate_state as vs
    from live_execution.commit_log import CommitLog
    attempt = dict(signature="dummy-sell", blockhash="dummy-blockhash", status="pending",
                   fraction=1.0, tokens=3.0, proceeds_usd=14.0, wallet="dummy-wallet", full_close=True)
    row = _commit(sell_attempt=attempt, sell_attempt_history=[dict(attempt, signature="dummy-old-sell", status="failed")])
    _write(tmp_path, "commits.json", [row])
    assert CommitLog(tmp_path / "commits.json")._load() == [row]
    assert vs.main(["--state-dir", str(tmp_path)]) == 0


@pytest.mark.parametrize("updates,expected", [
    ({"signed_at": False}, "sell_attempt.signed_at"),
    ({"release_candidate_since": float("nan")}, "sell_attempt.release_candidate_since"),
    ({"blockhash": None}, "sell_attempt.blockhash"),
    ({"fraction": 0.0}, "sell_attempt.fraction"),
    ({"wallet": ""}, "sell_attempt.wallet"),
    ({"full_close": 0}, "sell_attempt.full_close"),
    ({"status": "unknown"}, "sell_attempt.status"),
    ({"signature": ""}, "sell_attempt.signature"),
])
def test_invalid_sell_attempt_reports_precise_field(tmp_path, capsys, updates, expected):
    from scripts import validate_state as vs
    attempt = dict(signature="dummy-sell", blockhash="dummy-blockhash", status="pending",
                   fraction=1.0, tokens=3.0, proceeds_usd=14.0, wallet="dummy-wallet", full_close=True)
    attempt.update(updates)
    _write(tmp_path, "commits.json", [_commit(sell_attempt=attempt)])
    assert vs.main(["--state-dir", str(tmp_path)]) == 1
    assert f"index=0 field={expected} reason=" in capsys.readouterr().out


def test_invalid_signed_sell_mint_reports_precise_field(tmp_path, capsys):
    from scripts import validate_state as vs
    attempt = dict(signature="dummy-sell", blockhash="dummy-blockhash", status="pending",
                   fraction=1.0, tokens=3.0, proceeds_usd=14.0, wallet="dummy-wallet", full_close=True)
    _write(tmp_path, "commits.json", [_commit(payload={"mint": ""}, sell_attempt=attempt)])
    assert vs.main(["--state-dir", str(tmp_path)]) == 1
    assert "index=0 field=payload.mint reason=" in capsys.readouterr().out


@pytest.mark.parametrize("extras", [{}, {"sol_price_usd": 150.0, "total_usd": 163.0},
                                      {"reason": "operator deposit", "acknowledged_by": "operator"}])
def test_historical_wallet_snapshot_generations(tmp_path, extras):
    from scripts import validate_state as vs
    _write(tmp_path, "wallet_snapshot.json", dict(ts=1.0, usdc=13.0, sol=1.0, **extras))
    assert vs.main(["--state-dir", str(tmp_path)]) == 0


def test_reports_each_invalid_record_with_index_field_and_no_values(tmp_path, capsys):
    from scripts import validate_state as vs

    secret_marker = "dummy-sensitive-value-must-not-appear"
    _write(tmp_path, "executions.json", {"records": [
        _buy(usd_size=secret_marker),
        dict(_buy(), idempotency_key="another", price_usd=float("nan")),
        dict(_buy(), idempotency_key="third", ts=False),
    ]})
    assert vs.main(["--state-dir", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "executions.json records=3" in output
    for index, field in [(0, "usd_size"), (1, "price_usd"), (2, "ts")]:
        assert f"index={index} field={field}" in output
        assert "reason=" in output
    assert secret_marker not in output


@pytest.mark.parametrize("kind", ["accounting_identity", "close_signature"])
def test_cross_record_duplicate_authority_is_reported(tmp_path, capsys, kind):
    from scripts import validate_state as vs
    rows = [_buy(), _buy()] if kind == "accounting_identity" else [
        dict(kind="close", idempotency_key=f"close-{i}", mint="dummy-mint",
             usd_size=1, status="closed", signature="same-dummy-signature") for i in range(2)]
    _write(tmp_path, "executions.json", {"records": rows})
    assert vs.main(["--state-dir", str(tmp_path)]) == 1
    assert "index=1" in capsys.readouterr().out


@pytest.mark.parametrize("filename,value,field", [
    ("commits.json", [_commit(payload=[])], "payload"),
    ("wallet_snapshot.json", {"ts": 1, "usdc": 1, "sol": None}, "sol"),
    ("kill_switch.json", {"tripped": "false"}, "tripped"),
    ("break_state.json", {"taking": True, "break_until": float("inf")}, "break_until"),
    ("confirmations.json", {"confirmations": {"dummy-id": {"status": "approved"}}}, "id"),
    ("blocklist_state.json", {"mints": {"dummy-mint": []}}, "record"),
])
def test_sidecar_invalid_record_reports_field(tmp_path, capsys, filename, value, field):
    from scripts import validate_state as vs
    _write(tmp_path, filename, value)
    assert vs.main(["--state-dir", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert filename in output and f"field={field}" in output


def test_reads_all_sidecars_without_locks_creation_or_writes(tmp_path, monkeypatch):
    from scripts import validate_state as vs
    from live_execution import state_lock
    from live_execution.models import ExecutionLedger
    from live_execution.commit_log import CommitLog
    from live_execution.confirmation_queue import ConfirmationQueue

    _write(tmp_path, "executions.json", {"records": [_buy()]})
    _write(tmp_path, "commits.json", [_commit()])
    _write(tmp_path, "wallet_snapshot.json", {"ts": 1, "usdc": 1, "sol": 0})
    _write(tmp_path, "kill_switch.json", {"tripped": False, "reason": "", "ts": 1})
    _write(tmp_path, "break_state.json", {"taking": False, "break_until": 0})
    _write(tmp_path, "confirmations.json", {"confirmations": {
        "dummy-id": dict(id="dummy-id", mint="dummy-mint", decimals=6, usd_size=1,
                         proposed_at=1, expires_at=2, status="pending")}})
    _write(tmp_path, "blocklist_state.json", {"mints": {"dummy-mint": {
        "symbol": "DUMMY", "closes": [{"ts": 1, "pnl": -1, "loss": True, "rule": "exit_stop_loss", "book": "live"}]}}})
    _write(tmp_path, "safety_counters.json", {"total": {"quorum_failed": 1}, "rolling_24h": {},
                                              "last_reason": None, "last_at": None, "history": []})
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    def forbidden(*args, **kwargs):
        pytest.fail("validator attempted a lock or write")
    monkeypatch.setattr(state_lock, "state_lock", forbidden)
    for cls in (ExecutionLedger, CommitLog, ConfirmationQueue):
        monkeypatch.setattr(cls, "_save", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    for path in tmp_path.iterdir():
        path.chmod(0o444)
    tmp_path.chmod(0o555)
    try:
        assert vs.main(["--state-dir", str(tmp_path)]) == 0
        assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    finally:
        tmp_path.chmod(0o755)


def test_unreadable_and_malformed_exit_codes_do_not_print_contents(tmp_path, monkeypatch, capsys):
    from scripts import validate_state as vs
    path = _write(tmp_path, "executions.json", {"records": []})
    original = Path.read_text
    def unreadable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("dummy-secret-detail")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", unreadable)
    assert vs.main(["--state-dir", str(tmp_path)]) == 2
    assert "dummy-secret-detail" not in capsys.readouterr().out
    monkeypatch.setattr(Path, "read_text", original)
    path.write_text("{dummy-sensitive-malformed-input")
    assert vs.main(["--state-dir", str(tmp_path)]) == 1
    assert "dummy-sensitive" not in capsys.readouterr().out


def test_cli_runs_from_arbitrary_cwd(tmp_path):
    _write(tmp_path, "executions.json", {"records": [_buy()]})
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_state.py"),
                             "--state-dir", str(tmp_path)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "executions.json records=1" in result.stdout


def test_cli_does_not_create_python_import_caches(tmp_path):
    _write(tmp_path, "executions.json", {"records": [_buy()]})
    watched = [ROOT / "scripts", ROOT / "backend" / "live_execution"]
    before = {str(p) for directory in watched for p in directory.rglob("*.pyc")}
    env = dict(os.environ)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_state.py"),
                             "--state-dir", str(tmp_path)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert {str(p) for directory in watched for p in directory.rglob("*.pyc")} == before


@pytest.mark.parametrize("valid", [True, False])
def test_preflight_runs_same_validators(tmp_path, valid):
    from scripts import preflight_deploy as pf
    state = tmp_path / "fixture-state"
    state.mkdir()
    _write(state, "executions.json", {"records": [_buy() if valid else dict(_buy(), ts=float("nan"))]})
    checks = pf.run_preflight({"LIVE_EXECUTION_STATE_DIR": str(state)}, tmp_path,
                              include_compose=False)
    check = next(c for c in checks if c.name == "state validation")
    assert check.status == (pf.PASS if valid else pf.FAIL)


def test_preflight_missing_state_does_not_create_it(tmp_path):
    from scripts import preflight_deploy as pf
    missing = tmp_path / "not-created"
    check = pf.check_state_validation({"LIVE_EXECUTION_STATE_DIR": str(missing)}, tmp_path)
    assert check.status == pf.PASS
    assert not missing.exists()


def test_nonexistent_cli_directory_is_unreadable_not_valid(tmp_path):
    from scripts import validate_state as vs
    assert vs.main(["--state-dir", str(tmp_path / "missing")]) == 2
    assert not (tmp_path / "missing").exists()


def test_unknown_secret_sidecars_and_known_symlinks_are_not_opened(tmp_path, monkeypatch):
    from scripts import validate_state as vs
    secret = tmp_path / ".fomo_privy.json"
    secret.write_text('{"refresh_token":"dummy-sensitive-marker"}')
    (tmp_path / "commits.json").symlink_to(secret)
    original = Path.read_text
    def guarded(self, *args, **kwargs):
        if self in (secret, tmp_path / "commits.json"):
            pytest.fail("validator followed a symlink or opened secret state")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", guarded)
    assert vs.main(["--state-dir", str(tmp_path)]) == 2


@pytest.mark.parametrize("field,value", [("usd_size", False), ("price_usd", float("inf")),
                                        ("ts", None), ("tokens_out", "3")])
def test_optional_legacy_fields_reject_malformed_present_values(tmp_path, field, value):
    from scripts import validate_state as vs
    _write(tmp_path, "executions.json", {"records": [_buy(**{field: value})]})
    assert vs.main(["--state-dir", str(tmp_path)]) == 1


def test_preflight_unreadable_state_is_a_failure(tmp_path, monkeypatch):
    from scripts import preflight_deploy as pf
    original = Path.iterdir
    def unreadable(self):
        if self == tmp_path:
            raise PermissionError("dummy-sensitive-error")
        return original(self)
    monkeypatch.setattr(Path, "iterdir", unreadable)
    check = pf.check_state_validation({"LIVE_EXECUTION_STATE_DIR": str(tmp_path)}, tmp_path)
    assert check.status == pf.FAIL
    assert "dummy-sensitive" not in check.detail
