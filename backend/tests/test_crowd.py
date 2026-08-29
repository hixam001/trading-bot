"""
tests/test_crowd.py — crowd conviction feeds (fomo.fun board, pump.fun
comments) and their wiring into crowd_heat.

ALL offline: HTTP is faked via a stub AsyncClient; no live network anywhere.
Covers: the reference heat formula + clamps, fail-soft degradation (unconfigured token,
HTTP 500), response parsing (responseObject.items / authorTrade fields),
cache behavior, source priority (fomo > pumpfun > proxy), and the crowd_heat
rule consuming real feed heat with a tagged detail.
"""
from __future__ import annotations

import asyncio
import json
import pytest

import config
import data_providers.crowd as crowd
from models import Candidate
from rule_engine import rules as rules_mod


MINT = "Mint1111111111111111111111111111111111111"


def make_candidate(**overrides) -> Candidate:
    base = dict(
        symbol="TEST", mint_address=MINT, price_usd=0.001,
        liquidity_usd=50_000.0, volume_24h_usd=100_000.0,
        market_cap_usd=100_000.0, volume_1h_usd=20_000.0,
        buys_1h=300, sells_1h=200, price_change_1h_pct=5.0,
        age_hours=48.0, has_twitter=True, has_telegram=True,
    )
    base.update(overrides)
    return Candidate(**base)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """Reset module globals + caches so tests never see each other's data."""
    monkeypatch.setattr(crowd, "_sessions", {})
    monkeypatch.setattr(crowd, "_fomo_cache", crowd._TtlCache(60))
    monkeypatch.setattr(crowd, "_BENCHED_UNTIL", {})
    monkeypatch.setattr(crowd, "_CONSECUTIVE_ERRORS", {})
    monkeypatch.setattr(crowd, "_CONSECUTIVE_REJECTIONS", {})
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "")


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._text = text
        import json as _json
        if not self._text and self._payload:
            self._text = _json.dumps(self._payload)

    @property
    def text(self) -> str:
        return self._text

    def json(self):
        return self._payload


class FakeClient:
    """Replaces httpx.AsyncClient inside data_providers.crowd."""

    get_result = None
    post_result = None
    get_calls = []
    post_calls = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, **kw):
        FakeClient.get_calls.append(url)
        return FakeClient.get_result or FakeResponse(200, {})

    async def post(self, url, **kw):
        FakeClient.post_calls.append((url, kw))
        return FakeClient.post_result or FakeResponse(200, {})


def install_fake_http(monkeypatch, get=None, post=None):
    FakeClient.get_result = get
    FakeClient.post_result = post
    FakeClient.get_calls = []
    FakeClient.post_calls = []
    monkeypatch.setattr(crowd.httpx, "AsyncClient", FakeClient)


# --- heat formula ----------------------------------------------------------------

def test_heat_from_count_matches_reference_formula():
    assert crowd.heat_from_count(0) == 20
    assert crowd.heat_from_count(2) == 36
    assert crowd.heat_from_count(5) == 60
    assert crowd.heat_from_count(10) == 100
    assert crowd.heat_from_count(99) == 100          # clamped
    assert crowd.heat_from_count(-3) == 20           # floor at zero items


# --- fomo feed ----------------------------------------------------------------------

async def test_fomo_unconfigured_returns_none(monkeypatch):
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "")
    assert await crowd.fetch_fomo_theses(MINT) is None


async def test_fomo_parses_response_object_items(monkeypatch):
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "refresh-token")
    install_fake_http(
        monkeypatch,
        get=FakeResponse(200, {"responseObject": {"items": [
            {"userHandle": "whale", "ticker": "WHALE",
             "comment": {"comment": "floor holds", "numLikes": 3,
                         "olderThesis": 0, "newerThesis": 0},
             "authorTrade": {"usdValue": 12000.0, "unrealizedPnlUsd": 3400.0,
                             "percentageUnrealizedPnl": 39.5,
                             "realizedPnlUsd": 0.0, "closedAt": None}},
            {"displayName": "anon", "ticker": "WHALE",
             "comment": {"comment": "gm"},
             "authorTrade": {"usdValue": 5.0, "realizedPnlUsd": -2.0,
                             "closedAt": "2026-08-20"}},
        ]}}),
        post=FakeResponse(200, {"token": "x.y.z"}),
    )
    data = await crowd.fetch_fomo_theses(MINT)
    assert data is not None
    # "gm" is junk (<3 chars) -> dropped by _is_substantive:
    assert len(data["theses"]) == 1 and data["theses"][0]["who"] == "whale"
    assert data["total"] == 2          # older+newer+page = 0+0+2
    t = data["theses"][0]
    assert t["who"] == "whale"
    assert t["size_usd"] == 12_000.0
    assert t["unrealized_usd"] == 3_400.0
    assert t["pnl_pct"] == pytest.approx(39.5)

    # Cached second call: no new HTTP GET.
    calls_before = len(FakeClient.get_calls)
    again = await crowd.fetch_fomo_theses(MINT)
    assert again == data
    assert len(FakeClient.get_calls) == calls_before


def test_substantive_filter_drops_junk():
    assert crowd._is_substantive("trust the bot")            # real thesis
    assert not crowd._is_substantive("🚀")                    # emoji only
    assert not crowd._is_substantive("join https://discord.gg/abc")  # invite
    assert crowd._is_substantive("https://x.com/a but also real analysis here")


async def test_fomo_http_500_fails_soft(monkeypatch):
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "refresh-token")
    install_fake_http(monkeypatch, get=FakeResponse(500, {}),
                      post=FakeResponse(200, {"token": "t"}))
    assert await crowd.fetch_fomo_theses(MINT) is None


# --- refresh-token rotation persistence --------------------------------------

async def test_mint_persists_rotated_refresh_token(monkeypatch, tmp_path):
    """Privy rotates the token on mint — the new one must be persisted so
    the chain survives restarts."""
    state_file = tmp_path / "fomo_state.json"
    monkeypatch.setattr(config, "FOMO_PRIVY_STATE_FILE", str(state_file))
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "bootstrap-tok")

    captured = []

    class RotatingClient(FakeClient):
        async def post(self, url, **kw):
            body = kw.get("body") or kw.get("json") or {}
            captured.append(body.get("refresh_token"))
            return FakeResponse(200, {"token": "x.y.z",
                                      "refresh_token": "rotated-tok"})

    monkeypatch.setattr(crowd.httpx, "AsyncClient", RotatingClient)
    monkeypatch.setattr(crowd, "_sessions", {})

    app = crowd._PrivyApp(config.PRIVY_APP_ID, "ignored-uses-state")
    assert await crowd._mint_privy_session(app) is not None
    # First mint used the .env bootstrap...
    assert captured == ["bootstrap-tok"]
    persisted = json.loads(state_file.read_text())["refresh_token"]
    assert persisted == "rotated-tok"


async def test_second_mint_uses_rotated_token(monkeypatch, tmp_path):
    state_file = tmp_path / "fomo_state.json"
    monkeypatch.setattr(config, "FOMO_PRIVY_STATE_FILE", str(state_file))
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "bootstrap-tok")
    json.dump({"refresh_token": "persisted-tok"}, open(state_file, "w"))

    captured = []

    class RecordingClient(FakeClient):
        async def post(self, url, **kw):
            body = kw.get("body") or kw.get("json") or {}
            captured.append(body.get("refresh_token"))
            return FakeResponse(200, {"token": "j.w.t"})

    monkeypatch.setattr(crowd.httpx, "AsyncClient", RecordingClient)
    monkeypatch.setattr(crowd, "_sessions", {})

    await crowd._mint_privy_session(
        crowd._PrivyApp(config.PRIVY_APP_ID, "whatever"))
    assert captured == ["persisted-tok"]   # state file beats stale .env


def test_corrupt_state_file_falls_back_to_env(monkeypatch, tmp_path):
    state_file = tmp_path / "fomo_state.json"
    state_file.write_text("corrupt{")
    monkeypatch.setattr(config, "FOMO_PRIVY_STATE_FILE", str(state_file))
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "env-tok")

    assert crowd._load_persisted_refresh() == "env-tok"

async def test_fomo_falls_back_to_firecrawl_on_challenge(monkeypatch):
    """Direct GET returns the Cloudflare challenge page; Firecrawl stealth
    scrape returns the JSON. The parsed result must be identical."""
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "refresh-token")
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "fc-key")

    challenge = FakeResponse(
        403, text="<!DOCTYPE html><html class='no-js'>Just a moment...</html>")
    payload = {"responseObject": {"items": [
        {"userHandle": "whale", "ticker": "WHALE",
         "comment": {"comment": "floor holds"},
         "authorTrade": {"usdValue": 9000.0, "unrealizedPnlUsd": 1200.0}}]}}

    calls = {"direct": 0, "firecrawl": 0}

    class RoutedClient(FakeClient):
        async def get(self, url, headers=None, **kw):
            if url.startswith(config.FOMO_API_BASE):
                calls["direct"] += 1
                return challenge
            return FakeResponse(200, {})

        async def post(self, url, **kw):
            if "firecrawl" in url:
                calls["firecrawl"] += 1
                return FakeResponse(200, {"data": {
                    "rawHtml": __import__("json").dumps(payload),
                    "metadata": {"statusCode": 200}}})
            # privy session mint
            return FakeResponse(200, {"token": "x.y.z"})

    monkeypatch.setattr(crowd.httpx, "AsyncClient", RoutedClient)

    data = await crowd.fetch_fomo_theses(MINT)
    assert data is not None and len(data["theses"]) == 1
    assert data["theses"][0]["size_usd"] == 9_000.0
    assert calls == {"direct": 1, "firecrawl": 1}


async def test_quota_exhausted_firecrawl_fails_over(monkeypatch):
    """Firecrawl reports out-of-credits (402) -> benched -> next configured
    stealth scraper takes over automatically."""
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "refresh-token")
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "fc-key")
    monkeypatch.setattr(config, "SCRAPINGBEE_API_KEY", "sb-key")

    payload = {"responseObject": {"items": [
        {"userHandle": "whale", "ticker": "WHALE",
         "comment": {"comment": "floor holds"},
         "authorTrade": {"usdValue": 9000.0,
                         "unrealizedPnlUsd": 1200.0}}]}}

    calls = []

    class RoutedClient(FakeClient):
        async def get(self, url, headers=None, **kw):
            if url.startswith(config.FOMO_API_BASE):
                # Direct read is Cloudflare-challenged, as live-verified.
                return FakeResponse(
                    403, text="<!DOCTYPE html>Just a moment...</html>")
            if "app.scrapingbee.com" in url:
                calls.append("scrapingbee")
                import json as _json
                return FakeResponse(200, text=_json.dumps(payload))
            return FakeResponse(200, {})

        async def post(self, url, **kw):
            if "firecrawl" in url:
                calls.append("firecrawl")
                return FakeResponse(402, text="out of credits")
            return FakeResponse(200, {"token": "t"})

    monkeypatch.setattr(crowd.httpx, "AsyncClient", RoutedClient)

    data = await crowd.fetch_fomo_theses(MINT)
    assert data is not None and len(data["theses"]) == 1
    assert calls == ["firecrawl", "scrapingbee"]     # failover happened
    assert crowd._is_benched("firecrawl")            # and stayed failed-over


async def test_throttled_firecrawl_gets_short_backoff(monkeypatch):
    """A 429 is a transient RATE LIMIT, not credit exhaustion: firecrawl must
    fail over AND be benched only for the short throttle backoff — NOT the
    30-min credit bench. Regression for the 2026-08-27 Firecrawl sideline."""
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "refresh-token")
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "fc-key")
    monkeypatch.setattr(config, "SCRAPINGBEE_API_KEY", "sb-key")
    # Make the two bench durations unambiguous: long bench huge, backoff tiny.
    monkeypatch.setattr(config, "STEALTH_BENCH_SECONDS", 10_000.0)
    monkeypatch.setattr(config, "STEALTH_THROTTLE_BACKOFF_SECONDS", 0.02)

    payload = {"responseObject": {"items": [
        {"userHandle": "whale", "ticker": "WHALE",
         "comment": {"comment": "floor holds"},
         "authorTrade": {"usdValue": 9000.0,
                         "unrealizedPnlUsd": 1200.0}}]}}

    calls = []

    class RoutedClient(FakeClient):
        async def get(self, url, headers=None, **kw):
            if url.startswith(config.FOMO_API_BASE):
                return FakeResponse(
                    403, text="<!DOCTYPE html>Just a moment...</html>")
            if "app.scrapingbee.com" in url:
                calls.append("scrapingbee")
                import json as _json
                return FakeResponse(200, text=_json.dumps(payload))
            return FakeResponse(200, {})

        async def post(self, url, **kw):
            if "firecrawl" in url:
                calls.append("firecrawl")
                return FakeResponse(429, text="rate limited")
            return FakeResponse(200, {"token": "t"})

    monkeypatch.setattr(crowd.httpx, "AsyncClient", RoutedClient)

    data = await crowd.fetch_fomo_theses(MINT)
    assert data is not None and len(data["theses"]) == 1
    assert calls == ["firecrawl", "scrapingbee"]     # failover happened
    assert crowd._is_benched("firecrawl")            # benched right after 429

    # The short backoff expires quickly -> firecrawl is eligible again, which
    # would NOT be true if the 30-min credit bench had (wrongly) been applied.
    await asyncio.sleep(0.05)
    assert not crowd._is_benched("firecrawl")


def test_benched_scraper_is_skipped(monkeypatch):
    crowd._bench("firecrawl")

    def _fail(name):
        raise AssertionError(f"benched scraper {name} was called")

    chain = [("firecrawl", lambda u, h: (_fail("firecrawl"), None)[1]),
             ("zenrows", lambda u, h: {"ok": True})]
    enabled = [(n, fn) for n, fn in chain if not crowd._is_benched(n)]
    assert [n for n, _ in enabled] == ["zenrows"]


# --- enrichment: fomo board is the sole real source (pump deferred) -----------

async def test_enrich_uses_fomo_board(monkeypatch):
    async def fomo_ok(mint):
        return {"theses": [{"who": "w", "text": "t"}] * 3, "total": 3}

    monkeypatch.setattr(crowd, "fetch_fomo_theses", fomo_ok)

    c = make_candidate()
    await crowd.enrich_crowd_heat([c])
    assert c.fomo_heat == 44 and c.crowd_heat_source == "fomo"


async def test_enrich_no_feed_leaves_proxy_intact(monkeypatch):
    async def fomo_down(mint):
        return None
    monkeypatch.setattr(crowd, "fetch_fomo_theses", fomo_down)

    c = make_candidate()
    await crowd.enrich_crowd_heat([c])
    assert c.fomo_heat is None and c.crowd_heat_source == ""


# --- Item #3: author-P&L attribution — dumped theses discounted ----------------
# omo parity: an author who closed at a realized profit and keeps shilling is
# exit-liquidity marketing, not live conviction. KNOWN-data only — missing
# authorTrade keeps full credit (unknown ≠ dumped).

def test_is_dumped_known_data_only():
    open_win = {"closed": False, "realized_usd": 0.0}
    dumped = {"closed": True, "realized_usd": 250.0}
    closed_loss = {"closed": True, "realized_usd": -40.0}
    closed_zero = {"closed": True, "realized_usd": 0.0}
    no_trade = {}
    assert not crowd._is_dumped(open_win)       # still holds it
    assert crowd._is_dumped(dumped)             # exited at a profit
    assert not crowd._is_dumped(closed_loss)    # exited at a loss — not a dump
    assert not crowd._is_dumped(closed_zero)    # flat exit — not a dump
    assert not crowd._is_dumped(no_trade)       # unknown = full credit


async def test_dumped_thesis_discounted_from_effective_total(monkeypatch):
    """Weight 0.0: a closed-at-profit author contributes nothing to heat count."""
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "refresh-token")
    install_fake_http(
        monkeypatch,
        get=FakeResponse(200, {"responseObject": {"items": [
            {"userHandle": "holder", "comment": {"comment": "still long here",
                                                 "olderThesis": 0, "newerThesis": 0},
             "authorTrade": {"usdValue": 9000.0, "unrealizedPnlUsd": 100.0,
                             "closedAt": None}},
            {"userHandle": "dumper", "comment": {"comment": "this one goes to a dollar",
                                                 "olderThesis": 0, "newerThesis": 0},
             "authorTrade": {"usdValue": 0.0, "realizedPnlUsd": 800.0,
                             "closedAt": "2026-08-20"}},
        ]}}),
        post=FakeResponse(200, {"token": "x.y.z"}),
    )
    data = await crowd.fetch_fomo_theses(MINT)
    assert data is not None
    assert data["total"] == 2                       # board's own count unchanged
    assert data["dumped_count"] == 1
    assert data["effective_total"] == 1             # dumper counts for 0


async def test_dumped_thesis_half_credit_at_weight(monkeypatch):
    """Weight 0.5: a dumped thesis counts half — tunable without code change."""
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "refresh-token")
    monkeypatch.setattr(config, "FOMO_DUMPED_THESIS_WEIGHT", 0.5)
    install_fake_http(
        monkeypatch,
        get=FakeResponse(200, {"responseObject": {"items": [
            {"userHandle": "holder", "comment": {"comment": "still long here",
                                                 "olderThesis": 0, "newerThesis": 0},
             "authorTrade": {"usdValue": 9000.0, "unrealizedPnlUsd": 100.0,
                             "closedAt": None}},
            {"userHandle": "dumper", "comment": {"comment": "this one goes to a dollar",
                                                 "olderThesis": 0, "newerThesis": 0},
             "authorTrade": {"usdValue": 0.0, "realizedPnlUsd": 800.0,
                             "closedAt": "2026-08-20"}},
        ]}}),
        post=FakeResponse(200, {"token": "x.y.z"}),
    )
    data = await crowd.fetch_fomo_theses(MINT)
    assert data["dumped_count"] == 1
    assert data["effective_total"] == 2            # 2 - 1*0.5 -> round(1.5) = 2


async def test_unknown_author_trade_keeps_full_credit(monkeypatch):
    """No authorTrade object at all -> unknown, full credit (fail-soft)."""
    monkeypatch.setattr(config, "FOMO_PRIVY_REFRESH_TOKEN", "refresh-token")
    install_fake_http(
        monkeypatch,
        get=FakeResponse(200, {"responseObject": {"items": [
            {"userHandle": "anon", "comment": {"comment": "no trade data here",
                                              "olderThesis": 0, "newerThesis": 0}},
        ]}}),
        post=FakeResponse(200, {"token": "x.y.z"}),
    )
    data = await crowd.fetch_fomo_theses(MINT)
    assert data["dumped_count"] == 0
    assert data["effective_total"] == data["total"] == 1


async def test_enrich_consumes_effective_total(monkeypatch):
    """Heat comes from effective_total (discounted), not the raw board count."""
    async def fomo_data(mint):
        return {"theses": [{"who": "w", "text": "t"}] * 5, "total": 5,
                "dumped_count": 3, "effective_total": 2}
    monkeypatch.setattr(crowd, "fetch_fomo_theses", fomo_data)

    c = make_candidate()
    await crowd.enrich_crowd_heat([c])
    assert c.fomo_heat == crowd.heat_from_count(2)     # 36, not 60
    assert c.crowd_heat_source == "fomo"


async def test_enrich_falls_back_to_total_without_effective(monkeypatch):
    """Older payloads without effective_total keep the raw-count behavior."""
    async def fomo_data(mint):
        return {"theses": [{"who": "w", "text": "t"}] * 3, "total": 3}
    monkeypatch.setattr(crowd, "fetch_fomo_theses", fomo_data)

    c = make_candidate()
    await crowd.enrich_crowd_heat([c])
    assert c.fomo_heat == crowd.heat_from_count(3)     # 44 — unchanged contract


# --- the rule consumes real feed heat ---------------------------------------------------

def test_crowd_heat_rule_uses_real_feed_and_tags_source():
    r = rules_mod.crowd_heat(make_candidate(fomo_heat=44, crowd_heat_source="fomo"),
                             None, None)
    assert r.passed and "[fomo]" in r.detail and r.value == 44


def test_crowd_heat_rule_blocks_low_real_feed_heat():
    r = rules_mod.crowd_heat(make_candidate(fomo_heat=20, crowd_heat_source="fomo"),
                             None, None)
    assert not r.passed and "[fomo]" in r.detail


def test_crowd_heat_rule_proxy_tag_when_no_feed():
    r = rules_mod.crowd_heat(make_candidate(), None, None)
    assert r.passed and "[proxy]" in r.detail


# --- dead-provider fail-fast (2026-08-27) -----------------------------------------
# A provider that times out twice in a row is benched exactly like a 402, so a
# dead scraper costs two timeouts ONCE per process, never one full timeout per
# candidate per tick (the ~15-minute-tick bug: ScrapingBee ReadTimeouts were
# logged and ignored forever).

_SB_TEMPLATE = ("https://app.scrapingbee.com/api/v1/?api_key={api_key}"
                "&url={url}&stealth_proxy=true")


async def test_two_consecutive_timeouts_bench_provider(monkeypatch):
    monkeypatch.setattr(config, "SCRAPINGBEE_API_KEY", "sb-key")
    calls = {"n": 0}

    class TimeoutClient(FakeClient):
        async def get(self, url, headers=None, **kw):
            calls["n"] += 1
            raise crowd.httpx.ReadTimeout("simulated dead provider")

    monkeypatch.setattr(crowd.httpx, "AsyncClient", TimeoutClient)

    # attempt 1: counted, still transient
    assert await crowd._scrape_get_template(
        "scrapingbee", _SB_TEMPLATE, "sb-key", "https://origin.example/x") is None
    assert not crowd._is_benched("scrapingbee")
    assert crowd._CONSECUTIVE_ERRORS["scrapingbee"] == 1

    # attempt 2: streak hits the threshold -> benched like a 402
    assert await crowd._scrape_get_template(
        "scrapingbee", _SB_TEMPLATE, "sb-key", "https://origin.example/x") is None
    assert crowd._is_benched("scrapingbee")
    assert crowd._CONSECUTIVE_ERRORS["scrapingbee"] == 0
    assert calls["n"] == 2     # a third read skips it without any network call


async def test_single_timeout_is_transient_not_benched(monkeypatch):
    class TimeoutClient(FakeClient):
        async def get(self, url, headers=None, **kw):
            raise crowd.httpx.ReadTimeout("one-off")

    monkeypatch.setattr(crowd.httpx, "AsyncClient", TimeoutClient)
    assert await crowd._scrape_get_template(
        "scrapingbee", _SB_TEMPLATE, "sb-key", "https://origin.example/x") is None
    assert not crowd._is_benched("scrapingbee")
    assert crowd._CONSECUTIVE_ERRORS["scrapingbee"] == 1


async def test_success_resets_the_error_streak(monkeypatch):
    """timeout -> 200 -> timeout must NOT bench: the streak only counts
    consecutive failures."""
    outcomes = ["timeout", "ok", "timeout"]

    class FlakyClient(FakeClient):
        async def get(self, url, headers=None, **kw):
            outcome = outcomes.pop(0)
            if outcome == "timeout":
                raise crowd.httpx.ReadTimeout("flaky")
            return FakeResponse(200, text='{"responseObject": {"items": []}}')

    monkeypatch.setattr(crowd.httpx, "AsyncClient", FlakyClient)

    assert await crowd._scrape_get_template(
        "scrapingbee", _SB_TEMPLATE, "sb-key", "https://o.example/x") is None
    assert crowd._CONSECUTIVE_ERRORS["scrapingbee"] == 1
    assert await crowd._scrape_get_template(
        "scrapingbee", _SB_TEMPLATE, "sb-key", "https://o.example/x") is not None
    assert crowd._CONSECUTIVE_ERRORS["scrapingbee"] == 0
    assert await crowd._scrape_get_template(
        "scrapingbee", _SB_TEMPLATE, "sb-key", "https://o.example/x") is None
    assert crowd._CONSECUTIVE_ERRORS["scrapingbee"] == 1
    assert not crowd._is_benched("scrapingbee")


async def test_firecrawl_transport_error_fails_soft_and_benches(monkeypatch):
    """The firecrawl POST used to raise uncaught on transport errors; now it
    fails soft, counts the streak, and benches after two — like every other
    hop in the chain."""
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "fc-key")

    class DeadClient(FakeClient):
        async def post(self, url, **kw):
            raise crowd.httpx.ConnectTimeout("firecrawl unreachable")

    monkeypatch.setattr(crowd.httpx, "AsyncClient", DeadClient)

    assert await crowd._scrape_firecrawl("https://origin.example/x", {}) is None
    assert not crowd._is_benched("firecrawl")
    assert await crowd._scrape_firecrawl("https://origin.example/x", {}) is None
    assert crowd._is_benched("firecrawl")


# --- origin-rejection (403) fail-fast (2026-08-28) ---------------------------------
# A provider whose proxy keeps getting refused by the ORIGIN (HTTP 403 — it
# can't pass the endpoint's Cloudflare even with forwarded headers) is dead for
# THIS endpoint. Before this fix it was re-tried on every candidate (one wasted
# request + latency each, every tick) because only transport errors and 402/429
# benched a provider. Now two consecutive 403s bench it exactly like a 402.
# Kept in its own counter (_CONSECUTIVE_REJECTIONS) because _transport_success
# resets _CONSECUTIVE_ERRORS on any completed response — a 403 IS a completed
# response, so it must not clear the rejection streak, and vice versa.


async def test_two_consecutive_403s_bench_provider(monkeypatch):
    monkeypatch.setattr(config, "SCRAPINGBEE_API_KEY", "sb-key")
    calls = {"n": 0}

    class RejectClient(FakeClient):
        async def get(self, url, headers=None, **kw):
            calls["n"] += 1
            return FakeResponse(403, text="cloudflare challenge")

    monkeypatch.setattr(crowd.httpx, "AsyncClient", RejectClient)

    # attempt 1: counted, still transient
    assert await crowd._scrape_get_template(
        "scrapingbee", _SB_TEMPLATE, "sb-key", "https://origin.example/x") is None
    assert not crowd._is_benched("scrapingbee")
    assert crowd._CONSECUTIVE_REJECTIONS["scrapingbee"] == 1

    # attempt 2: streak hits the threshold -> benched like a 402
    assert await crowd._scrape_get_template(
        "scrapingbee", _SB_TEMPLATE, "sb-key", "https://origin.example/x") is None
    assert crowd._is_benched("scrapingbee")
    assert crowd._CONSECUTIVE_REJECTIONS["scrapingbee"] == 0
    assert calls["n"] == 2     # a third read skips it without any network call


async def test_single_403_is_transient_not_benched(monkeypatch):
    class RejectClient(FakeClient):
        async def get(self, url, headers=None, **kw):
            return FakeResponse(403, text="one-off challenge")

    monkeypatch.setattr(crowd.httpx, "AsyncClient", RejectClient)
    assert await crowd._scrape_get_template(
        "scrapingbee", _SB_TEMPLATE, "sb-key", "https://origin.example/x") is None
    assert not crowd._is_benched("scrapingbee")
    assert crowd._CONSECUTIVE_REJECTIONS["scrapingbee"] == 1


async def test_200_resets_the_rejection_streak(monkeypatch):
    """403 -> 200 -> 403 must NOT bench: the rejection streak only counts
    consecutive origin refusals; a 200 proves the proxy got through."""
    outcomes = ["403", "ok", "403"]

    class FlakyClient(FakeClient):
        async def get(self, url, headers=None, **kw):
            outcome = outcomes.pop(0)
            if outcome == "403":
                return FakeResponse(403, text="challenge")
            return FakeResponse(200, text='{"responseObject": {"items": []}}')

    monkeypatch.setattr(crowd.httpx, "AsyncClient", FlakyClient)

    assert await crowd._scrape_get_template(
        "scrapingbee", _SB_TEMPLATE, "sb-key", "https://o.example/x") is None
    assert crowd._CONSECUTIVE_REJECTIONS["scrapingbee"] == 1
    assert await crowd._scrape_get_template(
        "scrapingbee", _SB_TEMPLATE, "sb-key", "https://o.example/x") is not None
    assert crowd._CONSECUTIVE_REJECTIONS["scrapingbee"] == 0
    assert await crowd._scrape_get_template(
        "scrapingbee", _SB_TEMPLATE, "sb-key", "https://o.example/x") is None
    assert crowd._CONSECUTIVE_REJECTIONS["scrapingbee"] == 1
    assert not crowd._is_benched("scrapingbee")


async def test_403_does_not_clear_transport_streak_and_vice_versa(monkeypatch):
    """The two streak counters are independent: a 403 (completed response)
    resets the TRANSPORT streak via _transport_success but must not be counted
    as a transport error; a timeout resets nothing in the rejection counter."""
    outcomes = ["timeout", "403"]

    class MixedClient(FakeClient):
        async def get(self, url, headers=None, **kw):
            outcome = outcomes.pop(0)
            if outcome == "timeout":
                raise crowd.httpx.ReadTimeout("flaky")
            return FakeResponse(403, text="challenge")

    monkeypatch.setattr(crowd.httpx, "AsyncClient", MixedClient)

    # timeout -> transport streak 1, rejection streak untouched
    assert await crowd._scrape_get_template(
        "scrapingbee", _SB_TEMPLATE, "sb-key", "https://o.example/x") is None
    assert crowd._CONSECUTIVE_ERRORS["scrapingbee"] == 1
    assert crowd._CONSECUTIVE_REJECTIONS.get("scrapingbee", 0) == 0

    # 403 -> completed response clears transport streak, rejection streak 1
    assert await crowd._scrape_get_template(
        "scrapingbee", _SB_TEMPLATE, "sb-key", "https://o.example/x") is None
    assert crowd._CONSECUTIVE_ERRORS["scrapingbee"] == 0
    assert crowd._CONSECUTIVE_REJECTIONS["scrapingbee"] == 1
    assert not crowd._is_benched("scrapingbee")


async def test_direct_get_retries_transport_once(monkeypatch):
    """Reference parity: the direct read gets two transport attempts, but a
    real HTTP response (even 403) is never retried."""
    calls = {"n": 0}

    class FlakyClient(FakeClient):
        async def get(self, url, headers=None, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise crowd.httpx.ConnectError("connection reset")
            return FakeResponse(200, {"ok": True})

    monkeypatch.setattr(crowd.httpx, "AsyncClient", FlakyClient)
    assert await crowd._direct_get("https://origin.example/x", {}) == {"ok": True}
    assert calls["n"] == 2

    calls["n"] = 0

    class ForbiddenClient(FakeClient):
        async def get(self, url, headers=None, **kw):
            calls["n"] += 1
            return FakeResponse(403, text="denied")

    monkeypatch.setattr(crowd.httpx, "AsyncClient", ForbiddenClient)
    assert await crowd._direct_get("https://origin.example/x", {}) is None
    assert calls["n"] == 1


def test_stealth_timeout_matches_reference_budget():
    # the reference proxy call runs on a 25s budget; the old 45s per hop is
    # what let one dead provider stall a whole tick for ~15 minutes
    assert crowd._STEALTH_TIMEOUT.read == 25.0
    assert crowd._STEALTH_TIMEOUT.connect == 25.0


async def test_scrapingdog_forwards_privy_bearer(monkeypatch):
    """ScrapingDog custom_headers=true must forward the Privy bearer to the
    origin (ScrapingDog docs: pass the headers on the request + custom_headers
    =true, no extra cost). Mirrors the ScrapeOps keep_headers pattern that is
    verified live to carry the bearer through prod-api Cloudflare."""
    monkeypatch.setattr(config, "SCRAPINGDOG_API_KEY", "sd-key")
    seen = {}

    class CaptureClient(FakeClient):
        async def get(self, url, headers=None, **kw):
            seen["url"] = url
            seen["headers"] = headers or {}
            return FakeResponse(200, text='{"responseObject": {"items": []}}')

    monkeypatch.setattr(crowd.httpx, "AsyncClient", CaptureClient)

    out = await crowd._scrape_scrapingdog(
        "https://prod-api.fomo.family/x",
        {"authorization": "Bearer privy-token", "user-agent": "ua"})
    assert out is not None
    assert "custom_headers=true" in seen["url"]
    assert "api_key=sd-key" in seen["url"]
    # the bearer is forwarded as a request header to the provider
    assert seen["headers"].get("authorization") == "Bearer privy-token"

