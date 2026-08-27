"""
tests for live_execution.solana.get_usdc_balance — REF-R11 micro-bootstrap
real-funding check.

A MISSING USDC token account is a 0.0 balance (not an error); an unreadable
balance (every RPC failing) is None and callers must refuse. Hermetic: the
httpx client is faked, no network.
"""
from __future__ import annotations

import pytest

from live_execution import solana


class _FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, responses):
        # responses: list of _FakeResp returned in order per endpoint
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        if not self._responses:
            raise RuntimeError("no more canned responses")
        return self._responses.pop(0)


def _patch_client(monkeypatch, responses):
    monkeypatch.setattr(solana, "usdc_ata_for", lambda owner: "ATA111")
    monkeypatch.setattr(
        solana.httpx, "AsyncClient",
        lambda timeout=None: _FakeClient(responses))


async def test_balance_parsed_from_token_account(monkeypatch):
    body = {"result": {"value": {"amount": "2500000", "decimals": 6}}}
    _patch_client(monkeypatch, [_FakeResp(200, body)])
    assert await solana.get_usdc_balance("OWNER") == pytest.approx(2.5)


async def test_missing_account_is_zero(monkeypatch):
    body = {"error": {"message": "could not find account"}}
    _patch_client(monkeypatch, [_FakeResp(200, body)])
    assert await solana.get_usdc_balance("OWNER") == 0.0


async def test_all_rpcs_fail_returns_none(monkeypatch):
    # One endpoint per RPC_URLS entry, all erroring -> None (fail closed).
    n = len(solana.config.RPC_URLS)
    _patch_client(monkeypatch,
                  [_FakeResp(200, {"error": {"message": "boom"}})] * n)
    assert await solana.get_usdc_balance("OWNER") is None
