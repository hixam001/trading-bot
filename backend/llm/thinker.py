"""
llm/thinker.py — the omo-style THINK stage (operator decision 2026-08-23).

Before any rule evaluation, qwen3 reads a candidate's tape and writes a
structured pre-trade assessment:

    {"thesis": "...", "invalidation": "...", "verdict": "buy" | "pass"}

A trade requires **verdict == "buy" AND every rule passes** — the exact
think→gate intersection omotrades runs. Either side alone refuses, and the
refusal is journalled as loudly as an entry.

Fail-closed by design:
  * Ollama unreachable / unparsable output -> verdict falls back to the
    deterministic template thinker, tagged 'degraded' in its source — never
    an invented verdict.
  * DATA_BACKEND=mock always uses the template thinker — tests never touch
    Ollama.

The invalidation sentence is stored with the trade for the
exit_thesis_invalidated review; the LLM NEVER opens, closes, or sizes
anything by itself.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

import config
from models import Candidate

log = logging.getLogger(__name__)

THINK_PROMPT = """\
You are the pre-trade analyst for a Solana memecoin book. Study the token \
data below and decide whether to take a small position NOW.

Token: {symbol} ({name})
price ${price_usd} | liquidity ${liquidity_usd} | mcap ${market_cap_usd}
1h: vol ${volume_1h_usd}, {buys_1h} buys vs {sells_1h} sells, change {chg_1h}%
24h vol ${volume_24h_usd} | age {age_hours}h | twitter:{twitter} telegram:{telegram} site:{site}
{social_line}
{web_line}

Respond with STRICT JSON only (no prose outside it):
{{"thesis": "1-2 sentences: why attention exists, citing ONLY the numbers above",
  "invalidation": "one sentence: the specific move that proves this wrong",
  "verdict": "buy" or "pass"}}

Be conservative: refuse hype without substance, refuse tokens whose crowd \
is already leaving. /no_think"""


@dataclass
class ThinkResult:
    thesis: str
    invalidation: str
    verdict: str                     # "buy" | "pass"
    source: str                      # "ollama:<model>" | "template" | "degraded:*"
    grounding_flags: list[str] = field(default_factory=list)

    @property
    def wants_entry(self) -> bool:
        return self.verdict == "buy"


def _candidate_view(c: Candidate) -> dict:
    def yn(v):
        return "yes" if v else ("no" if v is False else "?")
    social_line = ""
    if c.social_interest is not None:
        social_line = "Social read (evidence only): interest looks " + str(c.social_interest) + ". " + str(c.social_note)
    web_line = ""
    if c.web_summary:
        web_line = "Web (last 24h): " + str(c.web_summary)
    return {
        "symbol": c.symbol,
        "name": c.name or c.symbol,
        "price_usd": c.price_usd,
        "liquidity_usd": c.liquidity_usd,
        "market_cap_usd": c.market_cap_usd,
        "volume_24h_usd": c.volume_24h_usd,
        "volume_1h_usd": c.volume_1h_usd,
        "buys_1h": c.buys_1h,
        "sells_1h": c.sells_1h,
        "chg_1h": c.price_change_1h_pct,
        "age_hours": c.age_hours,
        "twitter": yn(c.has_twitter),
        "telegram": yn(c.has_telegram),
        "site": yn(c.has_website),
        "social_line": social_line,
        "web_line": web_line,
    }
def build_think_prompt(c: Candidate) -> str:
    return THINK_PROMPT.format(**_candidate_view(c))


def template_think(c: Candidate) -> ThinkResult:
    """
    Deterministic offline thinker (mock mode / hermetic tests). Buys when
    hourly flow leads and the tape isn't rolling over; otherwise passes.
    Grounded by construction: it only restates the numbers it was given.
    """
    buys = c.buys_1h or 0
    sells = c.sells_1h or 0
    chg = c.price_change_1h_pct or 0.0
    vol = c.volume_1h_usd or 0.0
    verdict = "buy" if (buys > sells and chg >= -5.0) else "pass"
    return ThinkResult(
        thesis=(f"{c.symbol}: 1h vol ${vol:,.0f} with {buys} buys vs "
                f"{sells} sells, 1h change {chg:+.1f}% at ${c.price_usd}."),
        invalidation=(f"wrong if 1h change drops below {chg - 10:.1f}% or "
                      f"sells lead by more than 20%"),
        verdict=verdict,
        source="template",
    )


def parse_verdict_json(raw: str) -> Optional[tuple[str, str, str]]:
    """Extract (thesis, invalidation, verdict) from raw model text."""
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return None
    verdict = str(obj.get("verdict") or "").strip().lower()
    thesis = str(obj.get("thesis") or "").strip()
    invalidation = str(obj.get("invalidation") or "").strip()
    if verdict not in ("buy", "pass") or not thesis or not invalidation:
        return None
    return thesis, invalidation, verdict


class Thinker:
    """omo's think stage on local qwen3. One client per process."""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._ollama_ok: Optional[bool] = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(config.OLLAMA_TIMEOUT_SECONDS)
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def check_ollama_health(self) -> bool:
        try:
            resp = await self.client.get(
                config.OLLAMA_TAGS_ENDPOINT, timeout=httpx.Timeout(5.0)
            )
            ok = resp.status_code == 200 and config.MODEL_NAME in resp.text
        except httpx.HTTPError:
            ok = False
        if ok != self._ollama_ok:
            log.info("Ollama health: %s (model %s)", "UP" if ok else "DOWN",
                     config.MODEL_NAME)
        self._ollama_ok = ok
        return ok

    async def _ollama_generate(self, prompt: str) -> Optional[str]:
        try:
            resp = await self.client.post(
                config.OLLAMA_GENERATE_ENDPOINT,
                json={
                    "model": config.MODEL_NAME,
                    "prompt": prompt,
                    "stream": False,
                    "think": False,          # qwen3 thinking dominates latency
                    "options": {
                        "temperature": 0.2,
                        "num_ctx": config.OLLAMA_NUM_CTX,
        "num_predict": config.OLLAMA_NUM_PREDICT,
                    },
                },
            )
            resp.raise_for_status()
            text = (resp.json().get("response") or "").strip()
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
            return text.strip() or None
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("ollama generate failed: %s", exc)
            return None

    async def think(self, c: Candidate) -> ThinkResult:
        """
        The pre-trade verdict. Mock mode -> template thinker (offline,
        deterministic). Live mode -> qwen3; any failure degrades to the
        template with a 'degraded' tag rather than guessing a verdict.
        """
        if config.DATA_BACKEND != "live":
            return template_think(c)

        if self._ollama_ok is None:
            await self.check_ollama_health()
        if not self._ollama_ok:
            result = template_think(c)
            result.source = "degraded:ollama-down"
            return result

        raw = await self._ollama_generate(build_think_prompt(c))
        parsed = parse_verdict_json(raw) if raw else None
        if parsed is None:
            fallback = template_think(c)
            fallback.source = "degraded:unparsable"
            return fallback

        thesis, invalidation, verdict = parsed
        return ThinkResult(thesis, invalidation, verdict,
                           source=f"ollama:{config.MODEL_NAME}")