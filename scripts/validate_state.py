#!/usr/bin/env python3
"""Read-only, offline pre-upgrade checks for persisted trading authority.

Only known state filenames are read. Secret sidecars and unrecognized files
are never opened. No store mutation, lock acquisition, repair, or network call
is made. Exit codes: 0 valid, 1 invalid, 2 unreadable.
"""
from __future__ import annotations

import sys

# Read-only includes Python's optional import cache, even when the operator
# did not export PYTHONDONTWRITEBYTECODE before invoking the CLI.
sys.dont_write_bytecode = True

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from live_execution.commit_log import validate_commits  # noqa: E402
from live_execution.models import PendingConfirmation, validate_execution_records  # noqa: E402
from live_execution.state_validation import finite_number, validate_wallet_snapshot  # noqa: E402


@dataclass(frozen=True)
class Issue:
    index: int
    field: str
    reason: str


@dataclass
class FileReport:
    name: str
    count: int = 0
    issues: list[Issue] = field(default_factory=list)
    unreadable: bool = False


KNOWN_FILES = frozenset({
    "executions.json", "ledger.json", "commits.json", "wallet_snapshot.json",
    "confirmations.json", "blocklist_state.json", "kill_switch.json", "break_state.json",
    "safety_counters.json",
})

# Field names from trusted validators only. Never echo a record value, an
# exception from JSON decoding, a mint/key supplied by state, or an OS error.
VALIDATOR_FIELDS = frozenset({
    "kind", "status", "mint", "idempotency_key", "signature", "usd_size",
    "tokens_out", "price_usd", "ts", "pnl_usd", "commit_sealed_at", "blockhash",
    "commit_hash", "commit_nonce", "memo_signature", "reason", "commit_payload",
    "hash", "nonce", "payload", "sealed_at", "memo_published_at", "sell_attempt",
    "sell_attempt.fraction", "sell_attempt.tokens", "sell_attempt.proceeds_usd",
    "sell_attempt.signed_at", "sell_attempt.release_candidate_since",
    "sell_attempt.blockhash", "sell_attempt.wallet", "sell_attempt.full_close",
    "sell_attempt.status", "sell_attempt.signature", "payload.mint",
    "usdc", "sol", "wallet", "wallet_address", "owner",
})


def _issue(report, index, name, reason="invalid type, shape, or numeric value"):
    report.issues.append(Issue(index, name, reason))


def _validator_issue(report, index, exc, row=None):
    # Existing validators use fixed messages, but explicitly allowlist their
    # field label so future changes cannot expose state values through this CLI.
    message = str(exc).rsplit(": ", 1)[-1]
    label = message.split(" ", 1)[0]
    if isinstance(row, dict):
        attempt = row.get("sell_attempt")
        if message == "signed sell mint identity":
            label = "kind" if row.get("kind") != "sell" else "payload.mint"
        elif message == "sell blockhash":
            label = "sell_attempt.blockhash"
        elif message == "sell_attempt" and isinstance(attempt, dict):
            label = ("sell_attempt.status" if attempt.get("status") not in
                     ("pending", "confirmed", "failed") else "sell_attempt.signature")
        elif message == "sell terms" and isinstance(attempt, dict):
            if not 0 < attempt["fraction"] <= 1:
                label = "sell_attempt.fraction"
            elif not isinstance(attempt.get("wallet"), str) or not attempt["wallet"]:
                label = "sell_attempt.wallet"
            else:
                label = "sell_attempt.full_close"
    if label not in VALIDATOR_FIELDS:
        label = "record"
    _issue(report, index, label)


def _authority_rows(report, rows, validator):
    if not isinstance(rows, list):
        _issue(report, 0, "records", "expected a record list")
        return
    report.count = len(rows)
    identities, signatures = set(), set()
    for index, row in enumerate(rows):
        # Retain the runtime validators as the source of truth. Validate
        # separately to report every bad row, including rows after the first.
        try:
            validator([row])
        except (RuntimeError, TypeError, ValueError) as exc:
            if isinstance(row, dict) and validator is validate_commits:
                if row.get("kind") not in ("buy", "sell"):
                    _issue(report, index, "kind")
                    continue
                if row.get("status") not in ("sealed", "published", "bound", "failed"):
                    _issue(report, index, "status")
                    continue
            _validator_issue(report, index, exc, row)
            continue
        if validator is validate_execution_records:
            identity = (row["kind"], row["idempotency_key"])
            if identity in identities:
                _issue(report, index, "idempotency_key", "duplicate accounting identity")
            identities.add(identity)
            if row["kind"] == "close" and row.get("signature"):
                signature = row["signature"]
                if signature in signatures:
                    _issue(report, index, "signature", "duplicate settled signature")
                signatures.add(signature)


def _confirmations(report, value):
    if not isinstance(value, dict) or not isinstance(value.get("confirmations"), dict):
        _issue(report, 0, "confirmations", "expected a confirmations object")
        return
    rows = value["confirmations"]
    report.count = len(rows)
    for index, (key, row) in enumerate(rows.items()):
        if not isinstance(row, dict):
            _issue(report, index, "record")
            continue
        bad = False
        for name in ("id", "mint"):
            if not isinstance(row.get(name), str) or not row[name]:
                _issue(report, index, name)
                bad = True
        if row.get("id") != key:
            _issue(report, index, "id", "confirmation identity differs from mapping key")
            bad = True
        for name in ("usd_size", "proposed_at", "expires_at"):
            if not finite_number(row.get(name)) or row[name] < 0:
                _issue(report, index, name)
                bad = True
        if type(row.get("decimals")) is not int or not 0 <= row["decimals"] <= 255:
            _issue(report, index, "decimals")
            bad = True
        if row.get("status", "pending") not in ("pending", "approved", "denied", "expired", "consumed"):
            _issue(report, index, "status")
            bad = True
        for name in ("approved_at", "consumed_at"):
            if row.get(name) is not None and (not finite_number(row[name]) or row[name] < 0):
                _issue(report, index, name)
                bad = True
        if not isinstance(row.get("quote_snapshot", {}), dict):
            _issue(report, index, "quote_snapshot")
            bad = True
        if row.get("operation") is not None:
            try:
                from live_execution.trade_intent import canonical_operation
                canonical_operation(**row["operation"])
            except (TypeError, ValueError):
                _issue(report, index, "operation")
                bad = True
        if not bad:
            try:
                PendingConfirmation.from_json(row)
            except (TypeError, ValueError):
                _issue(report, index, "record", "confirmation does not load")


def _blocklist(report, value):
    # Do not call blocklist._load(): its corruption recovery renames files.
    if not isinstance(value, dict) or not isinstance(value.get("mints", {}), dict):
        _issue(report, 0, "mints", "expected a mint mapping")
        return
    rows = value.get("mints", {})
    report.count = len(rows)
    for index, (key, row) in enumerate(rows.items()):
        if not isinstance(key, str) or not key or not isinstance(row, dict):
            _issue(report, index, "record")
            continue
        for name in ("symbol", "reason", "blocked_at", "kind"):
            if name in row and not isinstance(row[name], str):
                _issue(report, index, name)
        closes = row.get("closes", [])
        if not isinstance(closes, list):
            _issue(report, index, "closes")
            continue
        for close in closes:
            if not isinstance(close, dict):
                _issue(report, index, "closes", "close history entry is not an object")
                continue
            for name in ("ts", "pnl"):
                if not finite_number(close.get(name)):
                    _issue(report, index, "closes." + name)
            if not isinstance(close.get("loss"), bool):
                _issue(report, index, "closes.loss")


def _single(report, value):
    report.count = 1
    if not isinstance(value, dict):
        _issue(report, 0, "record", "expected an object")
        return
    if report.name == "wallet_snapshot.json":
        try:
            validate_wallet_snapshot(value)
        except RuntimeError as exc:
            _validator_issue(report, 0, exc)
    elif report.name == "kill_switch.json":
        if not isinstance(value.get("tripped"), bool):
            _issue(report, 0, "tripped")
    elif report.name == "break_state.json":
        if not isinstance(value.get("taking"), bool):
            _issue(report, 0, "taking")
        if not finite_number(value.get("break_until")):
            _issue(report, 0, "break_until")
    elif report.name == "safety_counters.json":
        for name in ("total", "rolling_24h"):
            counters = value.get(name, {})
            if not isinstance(counters, dict) or any(type(n) is not int or n < 0 for n in counters.values()):
                _issue(report, 0, name)
        if not isinstance(value.get("history", []), list):
            _issue(report, 0, "history")


def validate_directory(state_dir: Path) -> list[FileReport]:
    """Return all issues without creating a directory, file, or lock."""
    state_dir = Path(state_dir)
    try:
        names = {p.name for p in state_dir.iterdir()}
    except OSError:
        return [FileReport("state-directory", issues=[Issue(0, "directory", "unreadable")], unreadable=True)]
    reports = []
    for name in sorted(names & KNOWN_FILES):
        report = FileReport(name)
        reports.append(report)
        path = state_dir / name
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError("state sidecar symlinks are not inspected")
            value = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            report.unreadable = True
            _issue(report, 0, "file", "unreadable regular state file")
            continue
        except (ValueError, UnicodeError):
            _issue(report, 0, "json", "invalid JSON encoding or syntax")
            continue
        if name in ("executions.json", "ledger.json"):
            rows = value.get("records") if isinstance(value, dict) else None
            _authority_rows(report, rows, validate_execution_records)
        elif name == "commits.json":
            _authority_rows(report, value, validate_commits)
        elif name == "confirmations.json":
            _confirmations(report, value)
        elif name == "blocklist_state.json":
            _blocklist(report, value)
        else:
            _single(report, value)
    return reports


def result_code(reports):
    if any(r.unreadable for r in reports):
        return 2
    return 1 if any(r.issues for r in reports) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(
        os.getenv("LIVE_EXECUTION_STATE_DIR") or str(BACKEND / "live_execution" / "state")))
    args = parser.parse_args(argv)
    reports = validate_directory(args.state_dir)
    for report in reports:
        print(f"{report.name} records={report.count} " + ("INVALID" if report.issues else "VALID"))
        for issue in report.issues:
            print(f"  index={issue.index} field={issue.field} reason={issue.reason}")
    if not reports:
        print("No known state files found; no files created.")
    return result_code(reports)


if __name__ == "__main__":
    raise SystemExit(main())
