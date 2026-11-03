"""§91 A1 — strict per-endpoint signature-status classification.

`get_signature_status_all` collapses "endpoint answered not found" and
"endpoint failed" into the same `None`. The strict helper keeps them apart:
`ABSENT` is proof, `UNREACHABLE` is unknown, and a found status is the dict.
All tests are hermetic: `solana.rpc` is stubbed, keyed by the single endpoint
the helper passes through.
"""
from __future__ import annotations

import pytest

from live_execution import solana


def _stub_rpc(monkeypatch, by_endpoint):
    """Replace solana.rpc with a per-endpoint stub; return the call log."""
    calls: list[dict] = []

    async def fake_rpc(method, params, timeout=15.0, endpoints=None):
        assert endpoints and len(endpoints) == 1
        endpoint = endpoints[0]
        calls.append(
            {"method": method, "params": params, "endpoint": endpoint})
        value = by_endpoint[endpoint]
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(solana, "rpc", fake_rpc)
    return calls


@pytest.mark.asyncio
async def test_none_result_is_unreachable(monkeypatch):
    _stub_rpc(monkeypatch, {"https://a.example/rpc": None})
    results = await solana.get_signature_status_all_strict(
        "SIG1", endpoints=["https://a.example/rpc"])
    assert len(results) == 1
    assert results[0] is solana.UNREACHABLE


@pytest.mark.asyncio
async def test_null_value_is_absent(monkeypatch):
    _stub_rpc(
        monkeypatch,
        {"https://a.example/rpc": {"context": {}, "value": [None]}},
    )
    results = await solana.get_signature_status_all_strict(
        "SIG1", endpoints=["https://a.example/rpc"])
    assert len(results) == 1
    assert results[0] is solana.ABSENT


@pytest.mark.asyncio
async def test_status_dict_is_returned(monkeypatch):
    status = {"slot": 9, "err": None, "confirmationStatus": "confirmed"}
    _stub_rpc(
        monkeypatch,
        {"https://a.example/rpc": {"context": {}, "value": [status]}},
    )
    results = await solana.get_signature_status_all_strict(
        "SIG1", endpoints=["https://a.example/rpc"])
    assert results == [status]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        "not-a-dict",
        42,
        [],
        {},
        {"value": None},
        {"value": "not-a-list"},
        {"value": []},
        {"value": [None, None]},
        {"value": [1]},
        {"value": ["status"]},
        {"context": {}, "value": [1, 2]},
    ],
)
async def test_malformed_results_are_unreachable(monkeypatch, result):
    _stub_rpc(monkeypatch, {"https://a.example/rpc": result})
    results = await solana.get_signature_status_all_strict(
        "SIG1", endpoints=["https://a.example/rpc"])
    assert len(results) == 1
    assert results[0] is solana.UNREACHABLE


@pytest.mark.asyncio
async def test_query_exception_is_unreachable_and_never_raises(
        monkeypatch, caplog):
    _stub_rpc(
        monkeypatch,
        {"https://a.example/rpc": RuntimeError("endpoint down")},
    )
    results = await solana.get_signature_status_all_strict(
        "SIG1", endpoints=["https://a.example/rpc"])
    assert len(results) == 1
    assert results[0] is solana.UNREACHABLE
    assert "status query failed" in caplog.text


@pytest.mark.asyncio
async def test_endpoint_order_is_preserved_and_calls_use_the_contract(
        monkeypatch):
    first = {"slot": 1, "confirmationStatus": "confirmed"}
    calls = _stub_rpc(
        monkeypatch,
        {
            "https://a.example/rpc": {"value": [None]},
            "https://b.example/rpc": {"value": [first]},
            "https://c.example/rpc": None,
        },
    )
    results = await solana.get_signature_status_all_strict(
        "SIG1",
        endpoints=[
            "https://a.example/rpc",
            "https://b.example/rpc",
            "https://c.example/rpc",
        ],
    )
    assert results[0] is solana.ABSENT
    assert results[1] == first
    assert results[2] is solana.UNREACHABLE
    assert [c["endpoint"] for c in calls] == [
        "https://a.example/rpc",
        "https://b.example/rpc",
        "https://c.example/rpc",
    ]
    for call in calls:
        assert call["method"] == "getSignatureStatuses"
        assert call["params"] == [
            ["SIG1"], {"searchTransactionHistory": True}]


@pytest.mark.asyncio
async def test_same_host_endpoints_are_deduped_to_one_entry(monkeypatch):
    calls = _stub_rpc(
        monkeypatch,
        {
            "https://a.example/rpc": {"value": [None]},
            "https://b.example/rpc": {"value": [None]},
        },
    )
    results = await solana.get_signature_status_all_strict(
        "SIG1",
        endpoints=[
            "https://a.example/rpc",
            "https://a.example/other-path",
            "https://b.example/rpc",
        ],
    )
    assert [c["endpoint"] for c in calls] == [
        "https://a.example/rpc",
        "https://b.example/rpc",
    ]
    assert len(results) == 2
    assert all(result is solana.ABSENT for result in results)


@pytest.mark.asyncio
async def test_empty_endpoint_list_returns_empty_without_querying(
        monkeypatch):
    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("no endpoint should be queried")

    monkeypatch.setattr(solana, "rpc", must_not_be_called)
    monkeypatch.setattr(
        solana.config, "RPC_URLS", ["https://configured.example/rpc"])
    assert await solana.get_signature_status_all_strict(
        "SIG1", endpoints=[]) == []


@pytest.mark.asyncio
async def test_legacy_helper_still_returns_none_for_unreachable(
        monkeypatch):
    """Regression guard: the old compatibility contract is unchanged."""
    _stub_rpc(monkeypatch, {"https://a.example/rpc": None})
    assert await solana.get_signature_status_all(
        "SIG1", endpoints=["https://a.example/rpc"]) == [None]


def test_sentinels_have_no_truth_value():
    assert repr(solana.ABSENT) == "<ABSENT>"
    assert repr(solana.UNREACHABLE) == "<UNREACHABLE>"
    assert solana.ABSENT is not solana.UNREACHABLE
    with pytest.raises(TypeError):
        bool(solana.ABSENT)
    with pytest.raises(TypeError):
        bool(solana.UNREACHABLE)
