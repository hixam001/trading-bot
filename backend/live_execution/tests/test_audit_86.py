"""
§86 backend regression tests: malformed RPC shapes must fail closed.

Everything here is hermetic: rpc()/httpx are faked, no sockets.
"""
from __future__ import annotations

import pytest

from live_execution import solana


class _Resp:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *a, **k):
        return self._responses.pop(0)


def _patch_http(monkeypatch, responses):
    monkeypatch.setattr(
        solana.httpx, "AsyncClient", lambda **kw: _Client(list(responses)))


# --- rpc(): a non-object JSON body is a failed endpoint, never .get() -------

async def test_rpc_skips_non_object_body_and_uses_next_endpoint(monkeypatch):
    monkeypatch.setattr(solana.config, "RPC_URLS",
                        ["https://a.example", "https://b.example"])
    _patch_http(monkeypatch, [_Resp([1]), _Resp({"result": {"value": 7}})])
    assert await solana.rpc("getBalance", ["addr"]) == {"value": 7}


async def test_rpc_all_non_object_bodies_returns_none(monkeypatch):
    monkeypatch.setattr(solana.config, "RPC_URLS", ["https://a.example"])
    _patch_http(monkeypatch, [_Resp("not-an-object")])
    assert await solana.rpc("getBalance", ["addr"]) is None


async def test_send_records_non_object_body_as_transport_attempt(monkeypatch):
    _patch_http(monkeypatch, [_Resp([1])])
    outcome = await solana.send_raw_transaction_detailed(
        b"raw", endpoints=["https://a.example"])
    assert outcome.signature is None
    assert outcome.attempts[0]["kind"] == "transport"
    assert await solana.send_raw_transaction(
        b"raw", endpoints=["https://a.example"]) is None


# --- balance/decimals/status readers: malformed value shapes -> None -------

@pytest.mark.parametrize("res", [
    [1], "string", 123, None,
    {}, {"value": None}, {"value": "12"}, {"value": {}}, {"value": True},
])
async def test_get_sol_balance_malformed_shapes_are_unknown(monkeypatch, res):
    async def fake_rpc(*a, **k):
        return res

    monkeypatch.setattr(solana, "rpc", fake_rpc)
    assert await solana.get_sol_balance("addr") is None


async def test_get_sol_balance_valid(monkeypatch):
    async def fake_rpc(*a, **k):
        return {"value": 1_500_000_000}

    monkeypatch.setattr(solana, "rpc", fake_rpc)
    assert await solana.get_sol_balance("addr") == pytest.approx(1.5)


@pytest.mark.parametrize("res", [
    [1], "string", 123, None,
    {}, {"value": []}, {"value": {"decimals": "6"}},
    {"value": {"decimals": True}}, {"value": {"decimals": -1}},
])
async def test_get_mint_decimals_malformed_shapes_are_unknown(monkeypatch, res):
    async def fake_rpc(*a, **k):
        return res

    monkeypatch.setattr(solana, "rpc", fake_rpc)
    assert await solana.get_mint_decimals("mint") is None


async def test_get_mint_decimals_valid_including_zero(monkeypatch):
    async def fake_rpc(*a, **k):
        return {"value": {"decimals": 0}}

    monkeypatch.setattr(solana, "rpc", fake_rpc)
    assert await solana.get_mint_decimals("mint") == 0


@pytest.mark.parametrize("res", [
    [1], "string", 123, None,
    {}, {"value": []}, {"value": [1]}, {"value": ["not-a-dict"]},
])
async def test_confirm_signature_malformed_status_shapes_are_unconfirmed(
        monkeypatch, res):
    async def fake_rpc(*a, **k):
        return res

    monkeypatch.setattr(solana, "rpc", fake_rpc)
    monkeypatch.setattr(solana.config, "CONFIRM_TIMEOUT_SECONDS", 0.0)

    out = await solana.confirm_signature("SIG")
    assert out["confirmed"] is False
    assert out["err"] == "not confirmed before timeout"


@pytest.mark.parametrize("res", [[1], "string", 123, None])
async def test_get_signature_status_malformed_shapes_are_unknown(monkeypatch, res):
    async def fake_rpc(*a, **k):
        return res

    monkeypatch.setattr(solana, "rpc", fake_rpc)
    assert await solana.get_signature_status("SIG") is None


@pytest.mark.parametrize("res", [
    {"value": 123}, {"value": {}}, {"value": "not-a-list"}, {"value": [1]},
])
async def test_get_token_balances_non_list_value_is_incomplete(monkeypatch, res):
    """A malformed `value` must not count as a successful program read."""
    async def fake_rpc(*a, **k):
        return res

    monkeypatch.setattr(solana, "rpc", fake_rpc)
    assert await solana.get_token_balances("addr") is None


@pytest.mark.parametrize("value", [None, "true", 1, [], {}])
async def test_is_blockhash_valid_non_bool_value_is_unknown(monkeypatch, value):
    async def fake_rpc(*a, **k):
        return {"value": value}

    monkeypatch.setattr(solana, "rpc", fake_rpc)
    assert await solana.is_blockhash_valid("hash") is None


@pytest.mark.parametrize("error", ["rate limited", {"code": -32603}, 429])
async def test_usdc_balance_non_object_error_does_not_crash(monkeypatch, error):
    class _RpcResp:
        status_code = 200

        def json(self):
            return {"error": error}

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **k):
            return _RpcResp()

    monkeypatch.setattr(solana.httpx, "AsyncClient", lambda **kw: _C())
    assert await solana.get_usdc_balance(
        "GsbwXfJraMomNxBcpR3DBDr4GEEr2kPpGEe1c1FQFPB1") is None
