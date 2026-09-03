"""
llm/social.py - realtime social read, provider-agnostic (the Grok stage).

the reference runs a realtime model over the social layer because memecoin attention
forms on the timeline before it shows on the tape. This module is that
stage - with one rigid rule: the read is EVIDENCE ONLY. It classifies
interest (organic | peaked | unclear) and returns one grounded sentence.
It returns NO verdict and may never flip one; only the thinker+gate decide.

RIGID PROVIDER SYSTEM: Groq, xAI (Grok), OpenRouter, Cerebras and any other
OpenAI-compatible gateway speak the same /chat/completions protocol, so the
client below is generic. Switching providers is ONLY a .env edit:

    SOCIAL_LLM_BASE_URL=https://api.groq.com/openai/v1    # today
    SOCIAL_LLM_BASE_URL=https://api.x.ai/v1               # tomorrow (Grok)
    SOCIAL_LLM_API_KEY=...
    SOCIAL_LLM_MODEL=llama-3.3-70b-versatile | grok-3-mini | ...

Empty key = stage disabled; dead endpoint or unparsable output = skipped.
Fail-soft like every feed: the think stage just reasons without the line.

Data inputs today: tape windows + crowd heat from the fomo board. When a
posts feed is ever wired (X/Nitter replacement), its lines join the same
prompt via append_evidence() - nothing else changes.

§51 (2026-09-02): the read is STAGED — `read_social_one` runs for a single
candidate from stage 5 of `gate_candidate_staged` (only all-passed,
thinker-bound candidates; the head-of-board batch read `enrich_social` no
longer runs in the pipeline). Usage journaling: each read's LLMResult is
appended to a module-level queue that decision_pipeline drains after the
gate loop (`take_queued_usages`) — the tick inserts them into
llm_call_usage exactly as the old batch path did. Free-tier arithmetic:
Groq's free plan allows 1,000 requests/day; the staged population is the
handful of all-passed candidates per tick, comfortably inside that budget.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

import httpx

import config
from models import Candidate
from llm.client import GroqClient, LLMResult

log = logging.getLogger(__name__)

ALLOWED_INTEREST = ("organic", "peaked", "unclear")

SYSTEM_PROMPT = (
    "You are a realtime attention analyst for Solana memecoins. You classify"
    " whether interest looks ORGANIC, already PEAKED, or UNCLEAR, citing"
    " ONLY numbers given to you. You never give trading advice and never"
    " say buy or sell. Reply with STRICT JSON only."
)


def _user_prompt(c: Candidate) -> str:
    def n(v, suffix=""):
        return ("?" if v is None else f"{v}{suffix}")
    return (
        f"Token {c.symbol} tape right now:\n"
        f"5m {n(c.price_change_5m_pct, chr(37))} | 1h {n(c.price_change_1h_pct, chr(37))} | "
        f"6h {n(c.price_change_6h_pct, chr(37))} | 24h {n(c.price_change_24h_pct, chr(37))}\n"
        f"1h vol ${n(c.volume_1h_usd)} with {n(c.buys_1h)} buys vs {n(c.sells_1h)} sells\n"
        f"age {n(c.age_hours, chr(104))} | crowd heat {n(c.fomo_heat)}/100 ({c.crowd_heat_source or chr(112)+chr(114)+chr(111)+chr(120)+chr(121)}) | "
        f"paid boost {chr(121)+chr(101)+chr(115) if c.boosted else chr(110)+chr(111)}\n"
        f"Classify the attention. STRICT JSON only:\n"
        f"{{\"interest\": \"organic|peaked|unclear\", \"note\": \"one sentence citing the numbers above\"}}"
    )


def parse_social(raw: str) -> Optional[tuple[str, str]]:
    """Extract (interest, note); None unless interest is in the allowed set."""
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return None
    interest = str(obj.get("interest") or "").strip().lower()
    note = str(obj.get("note") or "").strip()[:300]
    if interest not in ALLOWED_INTEREST or not note:
        return None
    return interest, note


class SocialReader:
    """Generic OpenAI-compatible client wrapper."""

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._groq = GroqClient(client=client)

    @property
    def enabled(self) -> bool:
        return bool(config.SOCIAL_LLM_API_KEY)

    async def health(self) -> bool:
        """Cheap GET /models probe, cached per process like the thinker does."""
        return await self._groq.health()

    async def read(self, c: Candidate) -> Optional[tuple[str, str, LLMResult]]:
        """One social read. Returns (interest, note, usage) or None on any failure."""
        if not self.enabled or not await self.health():
            return None
            
        result = await self._groq.complete_json(
            task="social_read",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=_user_prompt(c),
            budget=200
        )
        parsed = parse_social(result.text) if result and result.text else None
        if not parsed:
            return None
        return parsed[0], parsed[1], result


# ---------------------------------------------------------------------------
# §51 — STAGED single-candidate read + the usage queue
# ---------------------------------------------------------------------------
# The staged gate (decision_pipeline.gate_candidate_staged stage 5) calls
# read_social_one per candidate; each successful read's LLMResult lands
# here, and the tick drains it into llm_call_usage via take_queued_usages().
_queued_usages: list[LLMResult] = []


def take_queued_usages() -> list[LLMResult]:
    """Hand the queued social usages to decision_pipeline for journaling,
    then clear the queue."""
    drained, _queued_usages[:] = list(_queued_usages), []
    return drained


async def read_social_one(c: Candidate,
                          client: Optional[httpx.AsyncClient] = None) -> None:
    """§51: social-read ONE candidate (stage 5 of the staged gate) and
    queue its LLM usage for journaling. Fail-soft: any failure or a disabled
    stage (empty SOCIAL_LLM_API_KEY) leaves c.social_interest None — the
    thinker runs without the social line. Never raises."""
    if not config.SOCIAL_LLM_API_KEY:
        return          # stage off — don't even build a reader
    reader = SocialReader(client=client)
    if not reader.enabled:
        return
    try:
        res = await reader.read(c)
    except Exception as exc:
        log.info("social read errored for %s: %s", c.symbol, exc)
        return
    if res is None:
        return
    c.social_interest, c.social_note, usage = res
    # Store mint alongside the usage for the DB insert in the tick (same
    # convention the old batch path used).
    setattr(usage, "mint_address", c.mint_address)
    _queued_usages.append(usage)


async def enrich_social(
    candidates: list[Candidate],
    client: Optional[httpx.AsyncClient] = None,
    limit: Optional[int] = None,
) -> tuple[int, list[LLMResult]]:
    """Social-read the head of the board, fail-soft. Returns (applied count, usages).
    Disabled entirely (0 calls) when SOCIAL_LLM_API_KEY is empty."""
    reader = SocialReader(client=client)
    if not reader.enabled:
        return 0, []
    picks = [c for c in candidates if c.social_interest is None]
    picks = picks[: (limit if limit is not None else config.SOCIAL_READ_PER_TICK)]
    if not picks:
        return 0, []
    sem = asyncio.Semaphore(4)

    async def one(c: Candidate):
        async with sem:
            return await reader.read(c)

    results = await asyncio.gather(*(one(c) for c in picks), return_exceptions=True)
    applied = 0
    usages = []
    for cand, res in zip(picks, results):
        if isinstance(res, Exception) or res is None:
            continue
        cand.social_interest, cand.social_note, usage = res
        # Store mint alongside the usage for the DB insert in main.py
        setattr(usage, "mint_address", cand.mint_address)
        usages.append(usage)
        applied += 1
    if applied:
        log.info("social read: %s/%s candidates classified", applied, len(picks))
    return applied, usages
