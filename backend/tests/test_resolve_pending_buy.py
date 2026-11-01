"""
§88 Phase F — operator resolution tool for one pending live buy.

Hermetic: tmp ledger/commit log, no network, no wallet. The fill-actuals
reader is injected; the real one is only reached in production.
"""
from __future__ import annotations

import asyncio
import json
import pytest

from live_execution.commit_log import CommitLog
from live_execution.models import ExecutionLedger
from scripts import resolve_pending_buy as rpb

KEY = "mint-pending-88f"
MINT = "MintResolve88AAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


class Tty:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _seed(tmp_path, *, signature: str = "STOREDSIG",
          commit: bool = True) -> tuple[ExecutionLedger, str]:
    ledger = ExecutionLedger(tmp_path / "executions.json")
    commit_hash = ""
    if commit:
        rec = CommitLog(tmp_path / "commits.json").seal(
            "buy", {"mint": MINT, "usd": 2.5})
        commit_hash = rec["hash"]
    ledger.record_pending_buy(
        idempotency_key=KEY, mint=MINT, usd_size=2.5, tokens_out=10.0,
        price_usd=0.25, signature=signature, blockhash="BH88",
        commit_hash=commit_hash,
    )
    return ledger, commit_hash


def _args(tmp_path, *extra):
    return rpb.parse_args(
        [KEY, "--state-dir", str(tmp_path), *extra])


def _audit(tmp_path) -> list[dict]:
    path = tmp_path / "resolve_pending_buy_audit.log"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _run(args, **kwargs):
    return asyncio.run(rpb.resolve(args, **kwargs))


def test_expire_requires_confirmation_on_non_tty(tmp_path):
    ledger, _ = _seed(tmp_path)
    rc = _run(_args(tmp_path, "--expire"), stdin=Tty(False))
    assert rc == 1
    assert len(ledger.pending_buys()) == 1
    assert _audit(tmp_path)[-1]["result"] == "refused"


def test_expire_prompt_accepts_the_action_word(tmp_path):
    ledger, commit_hash = _seed(tmp_path)
    rc = _run(_args(tmp_path, "--expire"), stdin=Tty(True),
              input_fn=lambda prompt: "expire")
    assert rc == 0
    assert ledger.pending_buys() == []
    row = ledger.get_by_idempotency_key(KEY)
    assert row.status == "expired"
    assert "operator expired" in row.reason
    commit = next(c for c in CommitLog(tmp_path / "commits.json").all()
                  if c["hash"] == commit_hash)
    assert commit["status"] == "failed"
    audit = _audit(tmp_path)[-1]
    assert audit["result"] == "expired"
    assert audit["key"] == KEY
    assert audit["ledger"] == str(tmp_path / "executions.json")


def test_expire_prompt_rejects_anything_else(tmp_path):
    ledger, _ = _seed(tmp_path)
    rc = _run(_args(tmp_path, "--expire"), stdin=Tty(True),
              input_fn=lambda prompt: "yes")
    assert rc == 1
    assert len(ledger.pending_buys()) == 1


def test_expire_yes_settles_without_a_tty(tmp_path):
    ledger, _ = _seed(tmp_path)
    rc = _run(_args(tmp_path, "--expire", "--yes"), stdin=Tty(False))
    assert rc == 0
    assert ledger.pending_buys() == []


def test_unknown_key_is_nonzero_and_audited(tmp_path):
    _seed(tmp_path)
    args = rpb.parse_args(
        ["missing-key", "--state-dir", str(tmp_path), "--expire", "--yes"])
    rc = _run(args, stdin=Tty(False))
    assert rc == 1
    assert _audit(tmp_path)[-1]["result"] == "not-found"


def test_adopt_reads_actuals_and_binds_the_commit(tmp_path, monkeypatch):
    ledger, commit_hash = _seed(tmp_path)
    seen = {}

    async def actuals(side, mint, signature, owner=None):
        seen.update(side=side, mint=mint, signature=signature, owner=owner)
        return {"tokens_delta": 12.5, "usdc_delta": 2.4}

    monkeypatch.setattr(rpb.live_config, "EXPECTED_WALLET_ADDRESS", "OWNER88")
    monkeypatch.setattr(rpb, "_fill_actuals", actuals)

    rc = _run(_args(tmp_path, "--adopt-from-sig", "NEWSIG", "--yes"),
              stdin=Tty(False))

    assert rc == 0
    assert seen == {"side": "buy", "mint": MINT, "signature": "NEWSIG",
                    "owner": "OWNER88"}
    row = ledger.get_by_idempotency_key(KEY)
    assert row.status == "confirmed"
    assert row.usd_size == pytest.approx(2.4)
    assert row.tokens_out == pytest.approx(12.5)
    assert row.price_usd == pytest.approx(2.4 / 12.5)
    assert row.signature == "NEWSIG"
    commit = next(c for c in CommitLog(tmp_path / "commits.json").all()
                  if c["hash"] == commit_hash)
    assert commit["status"] == "bound"
    assert commit["signature"] == "NEWSIG"
    audit = _audit(tmp_path)[-1]
    assert audit["result"] == "adopted"
    assert audit["usd_size"] == pytest.approx(2.4)
    assert audit["tokens_out"] == pytest.approx(12.5)


def test_adopt_without_a_value_uses_the_stored_signature(tmp_path, monkeypatch):
    ledger, _ = _seed(tmp_path, signature="STOREDSIG")
    seen = {}

    async def actuals(side, mint, signature, owner=None):
        seen["signature"] = signature
        return {"tokens_delta": 10.0, "usdc_delta": 2.5}

    monkeypatch.setattr(rpb, "_fill_actuals", actuals)
    rc = _run(_args(tmp_path, "--adopt-from-sig", "--yes"), stdin=Tty(False))
    assert rc == 0
    assert seen["signature"] == "STOREDSIG"
    row = ledger.get_by_idempotency_key(KEY)
    assert row.status == "confirmed"


def test_adopt_with_no_signature_refuses(tmp_path, monkeypatch):
    ledger, _ = _seed(tmp_path, signature="")

    async def actuals(*args, **kwargs):
        raise AssertionError("actuals must not be read without a signature")

    monkeypatch.setattr(rpb, "_fill_actuals", actuals)
    rc = _run(_args(tmp_path, "--adopt-from-sig", "--yes"), stdin=Tty(False))
    assert rc == 1
    assert len(ledger.pending_buys()) == 1
    assert _audit(tmp_path)[-1]["reason"] == "no signature"


def test_adopt_refuses_when_actuals_are_unknown(tmp_path, monkeypatch):
    ledger, _ = _seed(tmp_path)

    async def actuals(side, mint, signature, owner=None):
        return {"tokens_delta": None, "usdc_delta": 2.4}

    monkeypatch.setattr(rpb, "_fill_actuals", actuals)
    rc = _run(_args(tmp_path, "--adopt-from-sig", "--yes"), stdin=Tty(False))
    assert rc == 1
    assert len(ledger.pending_buys()) == 1
    audit = _audit(tmp_path)[-1]
    assert audit["result"] == "refused"
    assert "actuals" in audit["reason"]


def test_cli_rejects_both_actions(tmp_path):
    with pytest.raises(SystemExit):
        rpb.parse_args([KEY, "--expire", "--adopt-from-sig", "SIG"])
