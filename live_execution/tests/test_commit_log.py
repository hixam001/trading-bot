"""
tests for live_execution.commit_log — REF-R11 seal -> published -> bound.

The local pre-broadcast intent log now carries the on-chain memo fields. A
commit moves sealed -> published (memo confirmed) -> bound (fill confirmed);
a commit whose memo could not be published is marked failed with a reason and
never bound. All transitions are hermetic (tmp files, injected clock).
"""
from __future__ import annotations

import hashlib
import json

import pytest

from live_execution.commit_log import CommitLog


@pytest.fixture
def log(tmp_path):
    return CommitLog(tmp_path / "commits.json")


def test_seal_records_hash_and_memo_fields_null(log):
    rec = log.seal("buy", {"mint": "M", "usd": 1.5})
    assert rec["status"] == "sealed"
    assert rec["memo_signature"] is None
    assert rec["memo_slot"] is None
    assert rec["memo_published_at"] is None
    # Hash is sha256(nonce|canonical_payload) — recomputed by hand.
    canonical = json.dumps({"mint": "M", "usd": 1.5}, sort_keys=True)
    expect = hashlib.sha256(
        (rec["nonce"] + "|" + canonical).encode()).hexdigest()
    assert rec["hash"] == expect


def test_seal_to_published_to_bound(log):
    rec = log.seal("buy", {"mint": "M"})
    assert log.record_memo(rec["hash"], "MEMOSIG", 42) is True
    assert log.bind(rec["hash"], "FILLSIG") is True
    rows = log.all()
    assert rows[0]["status"] == "bound"
    assert rows[0]["memo_signature"] == "MEMOSIG"
    assert rows[0]["memo_slot"] == 42
    assert rows[0]["signature"] == "FILLSIG"


def test_record_memo_only_from_sealed(log):
    rec = log.seal("buy", {"mint": "M"})
    log.record_memo(rec["hash"], "MEMOSIG", 1)
    # Already published -> a second record_memo is refused.
    assert log.record_memo(rec["hash"], "OTHER", 2) is False
    rows = log.all()
    assert rows[0]["memo_signature"] == "MEMOSIG"


def test_fail_marks_reason_and_blocks_bind(log):
    rec = log.seal("buy", {"mint": "M"})
    assert log.fail(rec["hash"], "memo: rpc down") is True
    rows = log.all()
    assert rows[0]["status"] == "failed"
    assert rows[0]["fail_reason"] == "memo: rpc down"
    # A failed commit can never be bound.
    assert log.bind(rec["hash"], "FILLSIG") is False


def test_bind_unknown_hash_refused(log):
    assert log.bind("deadbeef", "FILLSIG") is False


def test_persists_across_reload(tmp_path):
    path = tmp_path / "commits.json"
    a = CommitLog(path)
    rec = a.seal("buy", {"mint": "M"})
    a.record_memo(rec["hash"], "MEMOSIG", 7)
    # A brand-new instance reads the same file.
    b = CommitLog(path)
    assert b.all()[0]["status"] == "published"
    assert b.all()[0]["memo_slot"] == 7
