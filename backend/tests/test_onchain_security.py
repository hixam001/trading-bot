"""
tests/test_onchain_security.py — the on-chain authority fallback + provider
quota fast-fail (2026-08-29).

The fallback had shipped DEAD since creation: it POSTed {"method",
"params"} WITHOUT the JSON-RPC envelope (jsonrpc/id). api.mainnet-beta
answers that with HTTP 200 + EMPTY BODY (rate-limit masquerading as
success), publicnode with 400 "Parse error" — so every read failed at
resp.json() and security_clear lived on "unknown" forever while the
operator's Birdeye key was quota-exhausted. These tests pin the envelope
shape, the empty-200 rotation, and the Birdeye quota-body fast-fail so
neither can regress silently again.

All offline: httpx.AsyncClient is faked per the test_crowd.py pattern.
"""
from __future__ import annotations

import json

import config
import data_providers.onchain_security as ocs
from data_providers.base import _looks_like_quota_error

MINT = "Mint1111111111111111111111111111111111111"

# A real jsonParsed SPL mint account (shape verified live against
# api.mainnet-beta.solana.com, 2026-08-29): MEW — both authorities null
# (= revoked, good) on a type:"mint" parsed account.
_MEW_PARSED = {
    "decimals": 5, "freezeAuthority": None, "isInitialized": True,
    "mintAuthority": None, "supply": "8888547972267961", "type": "mint",
}


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self._text = text if text else (json.dumps(payload) if payload is not None else "")
        self.headers = {"content-type": "application/json"}

    @property
    def text(self) -> str:
        return self._text

    def json(self):
        if not self._text:
            raise ValueError("no body")
        return self._payload


class RecordingClient:
    """Fakes httpx.AsyncClient; routes per-endpoint results, records posts."""

    def __init__(self, results):
        # results: {endpoint_url_prefix: [FakeResponse, ...] (popped in order)}
        self.results = results
        self.posts: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kw):
        for prefix, queue in self.results.items():
            if url.startswith(prefix) and queue:
                self.posts.append((url, json))
                return queue.pop(0)
        self.posts.append((url, json))
        return FakeResponse(500, {})


def _ok_rpc(info=_MEW_PARSED):
    """A well-formed JSON-RPC success carrying a parsed mint account."""
    return FakeResponse(200, {
        "jsonrpc": "2.0", "id": 1, "result": {
            "context": {"slot": 442584848},
            "value": {"data": {"parsed": {"info": info, "type": "mint"},
                               "program": "spl-token", "space": 82},
                      "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
        }})


def _install(monkeypatch, results):
    client = RecordingClient(results)
    monkeypatch.setattr(ocs.httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setattr(config, "ONCHAIN_RPC_URLS", list(results.keys()))
    return client


# --- envelope regression (THE bug) ---------------------------------------------

async def test_rpc_payload_carries_full_jsonrpc_envelope(monkeypatch):
    """jsonrpc: '2.0' + id MUST be present — omitting them makes mainnet-beta
    answer 200 + empty body (the bug that kept security_clear blind)."""
    client = _install(monkeypatch,
                      {"https://api.mainnet-beta.solana.com": [_ok_rpc()]})
    await ocs.get_authority_flags(MINT)
    url, payload = client.posts[0]
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1
    assert payload["method"] == "getAccountInfo"
    assert payload["params"][0] == MINT
    assert payload["params"][1] == {"encoding": "jsonParsed"}


async def test_flags_parse_from_parsed_mint(monkeypatch):
    _install(monkeypatch, {"https://api.mainnet-beta.solana.com": [_ok_rpc()]})
    flags = await ocs.get_authority_flags(MINT)
    assert flags == {"mint_authority_revoked": True,
                     "freeze_authority_revoked": True}   # null = revoked


async def test_live_authority_flags_preserved(monkeypatch):
    """A mint whose authorities are still live parses as NOT revoked."""
    live = dict(_MEW_PARSED,
                mintAuthority="Liv1111111111111111111111111111111111111",
                freezeAuthority="Frz111111111111111111111111111111111111111")
    _install(monkeypatch,
             {"https://api.mainnet-beta.solana.com": [_ok_rpc(live)]})
    flags = await ocs.get_authority_flags(MINT)
    assert flags == {"mint_authority_revoked": False,
                     "freeze_authority_revoked": False}


# --- empty-200 rotation (mainnet-beta rate-limit masquerade) -------------------

async def test_empty_200_rotates_to_next_rpc(monkeypatch):
    """A 200 with an empty body is a FAILURE, not 'no data' — the next
    endpoint must take over."""
    empty = FakeResponse(200, None, text="")
    client = _install(monkeypatch, {
        "https://api.mainnet-beta.solana.com": [empty],
        "https://solana-rpc.publicnode.com": [_ok_rpc()],
    })
    flags = await ocs.get_authority_flags(MINT)
    assert flags["mint_authority_revoked"] is True   # second endpoint answered
    assert len(client.posts) == 2


async def test_all_endpoints_empty_returns_unknown(monkeypatch):
    empty = FakeResponse(200, None, text="")
    _install(monkeypatch, {
        "https://api.mainnet-beta.solana.com": [empty],
        "https://solana-rpc.publicnode.com": [empty],
    })
    flags = await ocs.get_authority_flags(MINT)
    assert flags == {}          # unknown — never fabricated


async def test_non_200_error_rotates(monkeypatch):
    """A non-200 (e.g. publicnode's 400 on a malformed request) rotates on."""
    err = FakeResponse(400, {"error": {"code": -32700,
                                        "message": "Parse error"}})
    _install(monkeypatch, {
        "https://api.mainnet-beta.solana.com": [err],
        "https://solana-rpc.publicnode.com": [_ok_rpc()],
    })
    flags = await ocs.get_authority_flags(MINT)
    assert flags["mint_authority_revoked"] is True


async def test_non_mint_parsed_account_is_unusable(monkeypatch):
    """A parsed account that is not type 'mint' (e.g. a token account) is
    never interpreted as authority data."""
    token_account = {"owner": "SomeWallet", "mint": MINT, "type": "account"}
    empty = FakeResponse(200, None, text="")
    _install(monkeypatch, {
        "https://api.mainnet-beta.solana.com": [
            _ok_rpc(info=token_account)],
        "https://solana-rpc.publicnode.com": [empty],
    })
    flags = await ocs.get_authority_flags(MINT)
    assert flags == {}


# --- quota-body sniff (Birdeye fast-fail) --------------------------------------

class _QuotaResp:
    status_code = 400
    headers = {}
    text = '{"success":false,"message":"Compute units usage limit exceeded"}'


class _Generic400:
    status_code = 400
    headers = {}
    text = '{"success":false,"message":"invalid address"}'


class _Empty400:
    status_code = 400
    headers = {}
    text = ""


def test_quota_body_detected():
    assert _looks_like_quota_error(_QuotaResp()) is True


def test_generic_400_not_quota():
    assert _looks_like_quota_error(_Generic400()) is False


def test_empty_body_not_quota():
    assert _looks_like_quota_error(_Empty400()) is False


# --- birdeye surface self-disable on quota -------------------------------------

async def test_birdeye_trending_disables_on_quota_400(monkeypatch):
    """The live incident: Birdeye answered 400 'Compute units usage limit
    exceeded' on EVERY call; the trending lens burned 3 retries per cycle.
    Now the first quota body disables the lens for the session."""
    from data_providers.birdeye import BirdeyeProvider
    import data_providers.birdeye as be_mod

    calls = {"n": 0}

    class QuotaClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None, timeout=None):
            calls["n"] += 1
            return FakeResponse(400, None,
                                text=_QuotaResp.text)

        async def aclose(self):
            return None

    provider = BirdeyeProvider.__new__(BirdeyeProvider)
    provider._client = None
    provider._security_available = True
    provider._trending_available = True
    provider._sec_semaphore = __import__("asyncio").Semaphore(2)
    monkeypatch.setattr(be_mod.httpx, "AsyncClient", lambda **kw: QuotaClient())
    # record_call lives in base.py (fetch_json's module) — patch it there.
    import data_providers.base as base_mod
    monkeypatch.setattr(base_mod, "record_call",
                        _make_noop_record(monkeypatch))

    out = await provider.get_candidates(20)
    assert out == []
    assert provider._trending_available is False

    # Second call: session-disabled — no HTTP at all.
    n_before = calls["n"]
    out2 = await provider.get_candidates(20)
    assert out2 == [] and calls["n"] == n_before


def _make_noop_record(monkeypatch):
    async def _noop(provider, ok, rate_limited=False):
        return None
    return _noop


async def test_birdeye_security_disables_on_quota_400(monkeypatch):
    """Same fast-fail for token_security: quota body → session-disabled,
    fields stay UNKNOWN (never False), zero retries burned."""
    from data_providers.birdeye import BirdeyeProvider
    import data_providers.birdeye as be_mod

    calls = {"n": 0}

    class QuotaClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None, timeout=None):
            calls["n"] += 1
            return FakeResponse(400, None, text=_QuotaResp.text)

        async def aclose(self):
            return None

    provider = BirdeyeProvider.__new__(BirdeyeProvider)
    provider._client = None
    provider._security_available = True
    provider._trending_available = True
    provider._sec_semaphore = __import__("asyncio").Semaphore(2)
    monkeypatch.setattr(be_mod.httpx, "AsyncClient", lambda **kw: QuotaClient())
    import data_providers.base as base_mod
    monkeypatch.setattr(base_mod, "record_call",
                        _make_noop_record(monkeypatch))

    info = await provider.get_security_info(MINT)
    assert info.mint_authority_revoked is None      # unknown, not False
    assert info.freeze_authority_revoked is None
    assert provider._security_available is False
    assert calls["n"] == 1                          # one call, not three