"""
llm/thinker.py — the omo-style THINK stage (operator decision 2026-08-23).

Before any rule evaluation, qwen3 reads a candidate's tape and writes a
structured pre-trade assessment:

    {"thesis": "...", "invalidation": "...", "verdict": "buy" | "pass"}

A trade requires **verdict == "buy" AND every rule passes** — the exact
think→gate intersection omotrades runs. Either side alone refuses, and the
refusal is journalled as loudly as an entry.

Fail-closed by design:
    * DeepSeek unreachable / unparsable output -> deterministic template
        explanation with verdict forced to 'pass'.
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

import config
from llm.client import DeepSeekJSONClient
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
{crowd_line}
{memory_line}

Respond with STRICT JSON only (no prose outside it):
{{"thesis": "1-2 sentences: why attention exists, citing ONLY the numbers above",
  "invalidation": "one sentence: the specific move that proves this wrong",
  "verdict": "buy" or "pass",
  "break": {{"taking": false, "minutes": 15, "reason": "why"}} }}

Be conservative: refuse hype without substance, refuse tokens whose crowd \
is already leaving. Weigh any crowd claims by whether the author is actually up on their position. /no_think"""


@dataclass
class ThinkResult:
    thesis: str
    invalidation: str
    verdict: str                     # "buy" | "pass"
    source: str                      # "deepseek:<model>" | "template" | "degraded:*"
    grounding_flags: list[str] = field(default_factory=list)
    break_taking: bool = False
    break_minutes: int = 0
    break_reason: str = ""

    @property
    def wants_entry(self) -> bool:
        return self.verdict == "buy"


def _candidate_view(c: Candidate, memory_line: str = "") -> dict:
    def yn(v):
        return "yes" if v else ("no" if v is False else "?")
    social_line = ""
    if c.social_interest is not None:
        social_line = "Social read (evidence only): interest looks " + str(c.social_interest) + ". " + str(c.social_note)
    web_line = ""
    if c.web_summary:
        web_line = "Web (last 24h): " + str(c.web_summary)
        
    crowd_line = ""
    if getattr(c, "fomo_theses", None):
        lines = []
        for t in c.fomo_theses:
            who = t.get("who", "unknown")
            size = t.get("size_usd", 0.0)
            pnl_usd = t.get("realized_usd", 0.0) if t.get("closed") else t.get("unrealized_usd", 0.0)
            pnl_pct = t.get("pnl_pct", 0.0)
            text = t.get("text", "")
            direction = "up" if pnl_usd >= 0 else "down"
            pnl_usd_abs = abs(pnl_usd)
            lines.append(f"@{who} on {c.symbol} — holding ${size:,.2f}, {direction} ${pnl_usd_abs:,.2f} ({pnl_pct:+.1f}%): \"{text}\"")
        if lines:
            crowd_line = "Crowd theses (fomo.fun):\n" + "\n".join(lines)
            
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
        "crowd_line": crowd_line,
        "memory_line": memory_line,
    }
def build_think_prompt(c: Candidate, memory_line: str = "") -> str:
    return THINK_PROMPT.format(**_candidate_view(c, memory_line))


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


def parse_verdict_json(raw: str) -> Optional[tuple[str, str, str, dict]]:
    """Extract (thesis, invalidation, verdict, break_obj) from raw model text."""
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
    break_obj = obj.get("break") or {}
    if not isinstance(break_obj, dict):
        break_obj = {}
    return thesis, invalidation, verdict, break_obj


class Thinker:
    """OMO think stage using DeepSeek with a fail-closed fallback."""

    def __init__(self) -> None:
        self._deepseek = DeepSeekJSONClient()

    async def aclose(self) -> None:
        await self._deepseek.aclose()

    async def think(self, c: Candidate, memory_line: str = "") -> ThinkResult:
        """
        The pre-trade verdict. Mock mode -> template thinker (offline,
        deterministic). Live mode -> DeepSeek; any failure degrades to a
        deterministic pass.
        """
        if config.DATA_BACKEND != "live":
            return template_think(c)

        result = await self._deepseek.complete_json(
            "You are a conservative pre-trade analyst. Reply with strict JSON only.",
            build_think_prompt(c, memory_line),
        )
        parsed = parse_verdict_json(result.text) if result else None
        if parsed is not None:
            thesis, invalidation, verdict, break_obj = parsed
            return ThinkResult(
                thesis, invalidation, verdict,
                source=f"deepseek:{config.DEEPSEEK_MODEL}",
                break_taking=bool(break_obj.get("taking")),
                break_minutes=int(break_obj.get("minutes") or 0),
                break_reason=str(break_obj.get("reason") or ""),
            )

        # A template explains the refusal but cannot approve an entry when
        # the configured live provider did not answer validly.
        fallback = template_think(c)
        fallback.verdict = "pass"
        fallback.source = "degraded:deepseek-unavailable"
        return fallback