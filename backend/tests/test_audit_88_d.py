"""
§88 Phase D — pending-buy expiry must re-query every RPC endpoint.

`rpc()` stops at the first endpoint that answers, so one lagging node could
report "signature not found" while another node already has the landed buy.
`get_signature_status_all` asks every deduped endpoint; the resolver only
expires a row when every answer is still unknown and the row is >= 90 s old.

Hermetic: no network; the per-endpoint status function is injected.
"""
from __future__ import annotations

import pytest

from live_execution import solana


@pytest.mark.asyncio
async def test_get_signature_status_all_queries_every_distinct_endpoint(
        monkeypatch):
    calls: list[str] = []

    async def status(sig, endpoints=None):
        endpoint = endpoints[0]
        calls.append(endpoint)
        if endpoint == "https://a.example/rpc":
            return {"err": None, "confirmationStatus": "confirmed"}
        if endpoint == "https://b.example/rpc":
            raise RuntimeError("endpoint down")
        return None

    monkeypatch.setattr(solana, "get_signature_status", status)

    results = await solana.get_signature_status_all(
        "SIG1",
        endpoints=[
            "https://a.example/rpc",
            "https://a.example/other-path",   # same host: deduped
            "https://b.example/rpc",
            "https://c.example/rpc",
        ],
    )

    # One query per distinct host, in order, including the failing endpoint.
    assert calls == [
        "https://a.example/rpc",
        "https://b.example/rpc",
        "https://c.example/rpc",
    ]
    assert results == [
        {"err": None, "confirmationStatus": "confirmed"},
        None,
        None,
    ]


@pytest.mark.asyncio
async def test_get_signature_status_all_is_empty_without_endpoints(
        monkeypatch):
    async def must_not_be_called(sig, endpoints=None):
        raise AssertionError("no endpoint should be queried")

    monkeypatch.setattr(solana, "get_signature_status", must_not_be_called)
    monkeypatch.setattr(solana.config, "RPC_URLS", [])
    assert await solana.get_signature_status_all("SIG1", endpoints=[]) == []
