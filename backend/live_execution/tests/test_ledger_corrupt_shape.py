"""
tests/test_ledger_corrupt_shape.py — A1 (repo audit):
ExecutionLedger._load corrupt-shape contract.

A valid-JSON-but-wrong-shape ledger file (a bare list from an older
format, a JSON scalar, a dict without "records") must hit the SAME loud
RuntimeError as unparseable JSON — never a bare AttributeError leaking
out of data.get(), and never an empty-looking book (which would forget
open exposure + idempotency history).
"""
from __future__ import annotations

import json

import pytest

from live_execution.models import ExecutionLedger


@pytest.mark.parametrize("payload", [
    "[]",                    # bare list (an older format's shape)
    '"a string"',            # JSON scalar
    "42",
    "null",
    '{"other": 1}',          # dict without "records"
])
def test_wrong_shape_raises_runtime_error(tmp_path, payload):
    led = ExecutionLedger(tmp_path / "executions.json")
    led.path.write_text(payload)
    with pytest.raises(RuntimeError, match="corrupt"):
        led._load()


def test_unparseable_json_raises_runtime_error(tmp_path):
    led = ExecutionLedger(tmp_path / "executions.json")
    led.path.write_text("{not json")
    with pytest.raises(RuntimeError, match="corrupt"):
        led._load()


def test_missing_file_is_empty(tmp_path):
    led = ExecutionLedger(tmp_path / "executions.json")
    assert led._load() == []


def test_valid_shape_loads(tmp_path):
    led = ExecutionLedger(tmp_path / "executions.json")
    led.path.write_text(json.dumps({"records": [
        {"kind": "buy", "idempotency_key": "k", "mint": "MINT",
         "usd_size": 1.0},
    ]}, indent=2))
    rows = led._load()
    assert len(rows) == 1
    assert rows[0]["kind"] == "buy"