"""
llm/llm_brain.py — the reference-style brain (operator decision 2026-08-27).

Ports the LLM *reasoning layer* of the reference repository onto our infra so the bot
thinks like the reference: role-routed models, a richly-prompted single tick that reads
the whole book + tape + crowd + web, and a structured JSON output (thoughts /
actions / verdicts / theses / watchlist / remember / fomo / break). The wallet
is fed in as context exactly like the reference's positionBlock, which is what makes the
brain identical whether the book is paper or (later) live — the execution layer
is the only thing that changes at the switch.

Defense-first invariants this module UPHELDS (see .clinerules):
  * Every field of the model's JSON is schema/type/range validated before it is
    used (rule 1). A verdict that fails validation is discarded, never guessed.
  * Fail closed (rule 2): any unreachable provider, unparsable body, or missing
    verdict degrades to the deterministic template PASS. A bad model answer can
    never become a buy.
  * The LLM remains a VETO/INPUT only. `wants_entry` is True only for a valid
    call == "buying"; main.py still requires `gate.all_passed AND wants_entry`.
    This module never opens, closes, sizes, or touches execution (rule 3).
  * Every degradation is logged with a reason (rule 6).

What this module deliberately does NOT port from the reference: its persona lore
and its "positions only exist on-chain" wallet model — our book of record stays
the paper/live engine; the reference's execution posture is not cloned, only its
reasoning.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import config
from llm.client import (
    LLMClient,
    LLMResult,
    MainGroqClient,
    GroqClient,
    build_main_client,
)
from models import Candidate



# ---------------------------------------------------------------------------
# Role-based model routing — port of the reference's models.server.ts.
#
# the reference is "not one model": each stage declares the mind it was designed around
# plus an ordered fallback chain, and resolution is HONEST — the router returns
# the model it actually used and whether the stage ran degraded, and never
# claims a model it could not reach. We map that onto our two real providers
# (DeepSeek main + Groq) instead of the reference's claude/grok/gemini gateway ids.
# ---------------------------------------------------------------------------

MODEL_ROLES = ("reasoning", "realtime", "narration")


@dataclass
class ResolvedRole:
    role: str
    provider: str            # provider actually used ("" when none)
    model: str               # model actually used ("" when none)
    declared: str            # the provider this role was designed around
    degraded: bool           # True when a fallback ran instead of the declared
    label: str               # short human label for the feed / decision log


# Provider ids a role may use, in preference order; the first is "declared".
_ROLE_CHAINS: dict[str, tuple[str, ...]] = {
    # The long think: thesis formation + pre-trade verdict. Declared on the
    # configured main provider; Groq is the warm fallback.
    "reasoning": ("main", "groq"),
    # Live social read — the isolated social Groq client (evidence only).
    "realtime": ("social",),
    # The thought stream. Same mind as reasoning so words match the decision.
    "narration": ("main", "groq"),
}

# Providers the gateway has already rejected this process. Not retried (the reference).
_UNAVAILABLE: set[str] = set()


def _is_unsupported_model_error(result: Optional[LLMResult]) -> bool:
    """True when a failure means 'this model is not served here' (the reference's
    isUnsupportedModelError) — as opposed to a transient rate-limit/timeout,
    which is NOT a reason to permanently downgrade the mind."""
    if result is None:
        return False
    reason = (result.degradation_reason or "").lower()
    if "timeout" in reason or "rate" in reason:
        return False
    return any(k in reason for k in (
        "model_not_found", "invalid model", "unsupported model",
        "does not exist", "model",
    ))


def _build_provider(pid: str, timeout_override: Optional[float] = None) -> Optional[LLMClient]:
    """Instantiate the client for a logical provider id, or None when keyless.
    `timeout_override` lets a heavy stage (the brain) use a longer read timeout
    than the provider default without changing the per-candidate thinker."""
    if pid == "main":
        client = build_main_client()
    elif pid == "groq":
        client = MainGroqClient() if config.GROQ_API_KEY else None
    elif pid == "social":
        client = GroqClient() if config.SOCIAL_LLM_API_KEY else None
    else:
        client = None
    if client is not None and timeout_override is not None:
        client.timeout_seconds = timeout_override
    return client


async def run_role(
    role: str,
    task: str,
    system_prompt: str,
    user_prompt: str,
    budget: Optional[int] = None,
    json_mode: bool = True,
    timeout_seconds: Optional[float] = None,
) -> tuple[Optional[LLMResult], ResolvedRole]:
    """Run one stage against its role's provider chain (runRole). Tries each
    provider in order; an unsupported-model error benches that provider for the
    process; any other failure just falls through for this call. Returns the
    first valid result + honest resolution (never claims an unreachable model).
    `timeout_seconds` overrides the provider default for heavy stages.
    """
    declared = _ROLE_CHAINS[role][0]
    chain = [p for p in _ROLE_CHAINS[role] if p not in _UNAVAILABLE]
    fallback = ResolvedRole(role, "", "", declared, True, "template")

    for pid in chain:
        client = _build_provider(pid, timeout_override=timeout_seconds)
        if client is None or not client.api_key:
            continue
        result = await client.complete_json(
            task=task, system_prompt=system_prompt,
            user_prompt=user_prompt, budget=budget, json_mode=json_mode,
        )
        if result is not None and result.text and not result.degradation_reason:
            degraded = pid != declared
            label = f"{client.provider}:{client.model}" + (
                f" (routed from {declared})" if degraded else "")
            return result, ResolvedRole(
                role, client.provider, client.model, declared, degraded, label)
        if _is_unsupported_model_error(result):
            _UNAVAILABLE.add(pid)
        if result is not None:
            log.warning("llm_brain role=%s provider=%s degraded: %s",
                        role, pid, result.degradation_reason)
        await client.aclose()

    return None, fallback


# ---------------------------------------------------------------------------
# The brain tick prompt — port of brain.server.ts SYSTEM, adapted: we keep the
# trading discipline (hard filters, decision buckets, ground truth, price-talk
# rules, output contract) and drop the reference's persona lore. Ground-truth rules are
# what make the output auditable: every number must be copied from the snapshot.
# ---------------------------------------------------------------------------

LLM_SYSTEM = """You are an autonomous memecoin trader for a Solana paper book.
You trade SOLANA MEMECOINS, reading a live screener, the crowd board, the web,
and your own open positions. Your job each tick is to grade the screener rows
you are given and show your work. You are a memecoin trader, not an analyst and
not a newsletter.

WHAT YOU NEVER TALK ABOUT (hard filter — mentioning any of it is a failure):
- exchange/DEX infrastructure or protocol business: raydium/orca/jupiter/pump.fun
  as companies, their fees, revenue, TVL, tokenomics, upgrades.
- macro and industry commentary: leverage totals, open interest, ETFs,
  institutions, funding rates, network TVL, regulation, VC rounds.
- news-anchor phrasing, market recaps, or anything that reads like a research note.
You are hunting individual tokens to buy and flip, not covering the ecosystem.

HOW YOU DECIDE (this belongs in "verdicts", not in your thoughts):
A verdict is the visible work behind a decision — it must read like someone
actually went and looked. Every verdict carries FIVE TO SEVEN checks from
DIFFERENT kinds of research, not restatements of the tape. Use at least four of
these buckets per verdict:
- the tape: whether buyers or sellers are leading the hour (as a fact, never as
  trade counts), whether hourly volume is rising or dying, the 1h/6h shape
  (extended, cooling, basing), and how old the name is.
- the people: who is behind it and who is holding it — the account, the site,
  the community — taken only from the research material below.
- the crowd side: what the board thesis actually claims, how many people wrote
  one, and whether the claim survives what the chart did after it was written.
- the smart side: which notable wallets are in it, whether they are up and need
  an exit or underwater and stuck. Say what that does to supply.
- the outside read: anything from the web material — a post, a launch, a date.
- the counter-case: the strongest reason you could be wrong, stated as a check,
  not as a hedge.
Each check is a short clause with the finding and the word "fails" or "holds"
where a threshold applies. No two checks in one verdict may test the same thing.
Then a call — buying / stalking / pass — with an entry condition and an
invalidation. Pass most of them, and say which part failed. If a name is already
in your open positions the call is "holding" or "pass", never "stalking".

GROUND TRUTH (breaking any of these is a failed tick):
- every number you say — volume, %, age, p&l — must come from the snapshot
  below, copied as given. Never round it, never soften it, never estimate, never
  carry a number forward from a previous tick.
- if a number is not in the snapshot, do not say a number. Say the thing without it.
- only tickers present in the snapshot exist. Never invent a founder, a team, a
  partnership, a follower count, or a fill. If you do not have it in the material
  above, talk about what you can see instead.

HOW YOU TALK ABOUT PRICE (hard rule):
Never say market cap, mc, fdv, valuation, liquidity, liq, depth, pool, or size.
NEVER quote trade counts ("812 buys vs 640 sells") — who is winning the hour is
stated in words: "buyers still in front", "sellers took the hour back". Talk
about the move: "up 60% in an hour and buyers still ahead", "gave back a third
of the run", "volume halved into the second hour". Never write raw per-token
decimal prices; refer to levels as where it broke from or where it last based.

YOUR BOOK, THEN THE REST OF THE MARKET (hard rule):
Whenever the wallet has open positions, at least two of this tick's thoughts are
about YOUR OWN names — by ticker, with your live p&l, and what you are doing
next: holding, trimming, adding, cutting, or nothing yet and why. Talk about
losers as well as winners. The remaining thoughts are about names you do NOT
hold: a contender, a name you passed on, a crowd thesis you disagree with.
"""

# The brain output contract (minified JSON), appended to the system prompt.
LLM_OUTPUT_CONTRACT = """
Return ONLY minified JSON with EXACTLY this shape:
{"thoughts":["..."],
 "actions":[{"kind":"read|did|refused","text":"..."}],
 "verdicts":[{"symbol":"TICKER","call":"buying|stalking|pass|holding",
   "checks":["clause with finding + fails/holds", ...],
   "entry":"condition or null","invalidation":"what kills it or null",
   "reason":"one line why"}],
 "theses":[{"who":"name/archetype","claim":"their thesis in one line",
   "stance":"agree|fade|watch","reason":"the number that makes you take that side"}],
 "watchlist":[{"rank":1,"symbol":"TICKER","conviction":1,
   "thesis":"2-3 sentences","trigger":"the exact condition that makes you buy"}],
 "remember":[{"topic":"short slug","note":"one durable sentence"}],
 "fomo":0,
 "break":{"taking":false,"minutes":15,"reason":"why"}}
Produce 6-9 thoughts, 2-4 actions, one verdict PER screener row you were given
(each with 5-7 checks from different buckets), 1-3 theses, 0-2 memories, and a
ranked watchlist of the names you would actually spend on next. "fomo" is your
0-100 read of crowd heat. Set break.taking true only when you genuinely want to
step away; most ticks it is false. Every symbol in verdicts/watchlist MUST be a
ticker from the screener rows below. /no_think"""


def _fmt_usd(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "$?"


def build_wallet_block(portfolio, recent_fills: Optional[list] = None) -> str:
    """The wallet mimicry: format the book like the reference's positionBlock so the
    brain reasons over live positions. `portfolio` is our PortfolioState (paper
    or live — the brain cannot tell, by design). Numbers come from the engine,
    never from the model: we cite cash, deployed size, entry price and thesis."""
    if portfolio is None:
        return "- no open positions. you are flat."
    lines: list[str] = []
    cash = getattr(portfolio, "cash_usd", None)
    positions = getattr(portfolio, "open_positions", None) or []
    deployed = sum((getattr(t, "position_size_usd", 0.0) or 0.0) for t in positions)
    if cash is not None:
        lines.append(f"- book: cash {_fmt_usd(cash)}, deployed {_fmt_usd(deployed)}")
    if not positions:
        lines.append("- no open positions. you are flat.")
    for t in positions[:8]:
        symbol = getattr(t, "symbol", "?")
        size = getattr(t, "position_size_usd", None)
        entry = getattr(t, "entry_price_usd", None)
        thesis = (getattr(t, "thesis", "") or "")[:90]
        bit = f"- {symbol}: open {_fmt_usd(size)}"
        if entry:
            bit += f", entry ${entry:.8f}"
        if thesis:
            bit += f" — {thesis}"
        lines.append(bit)
    for f in (recent_fills or [])[:5]:
        lines.append(f"- recent fill: {f}")
    return "\n".join(lines)


def _num(v, fmt: str, suffix: str = "") -> str:
    """None-safe number formatting for Optional Candidate fields."""
    return (fmt.format(v) + suffix) if isinstance(v, (int, float)) else "?"


def build_snapshot_block(candidates: list[Candidate]) -> str:
    """The screener rows the model is allowed to cite. One compact line per
    candidate — these are the ONLY numbers it may repeat (ground truth).
    Optional fields render as '?' rather than crashing or inventing."""
    rows = []
    for c in candidates:
        socials = []
        if getattr(c, "has_twitter", False):
            socials.append("tw")
        if getattr(c, "has_telegram", False):
            socials.append("tg")
        if getattr(c, "has_website", False):
            socials.append("site")
        rows.append(
            f"- {c.symbol} ({c.mint_address[:8]}): price ${c.price_usd:.8f}, "
            f"1h vol {_fmt_usd(c.volume_1h_usd)}, "
            f"{_num(c.buys_1h, '{}')} buys vs {_num(c.sells_1h, '{}')} sells, "
            f"1h {_num(c.price_change_1h_pct, '{:+.1f}', '%')}, "
            f"liq {_fmt_usd(c.liquidity_usd)}, age {_num(c.age_hours, '{:.1f}', 'h')}, "
            f"socials {'+'.join(socials) if socials else 'none'}")
    return "\n".join(rows)


def build_tick_prompt(snapshot_block: str, wallet_block: str,
                      crowd_block: str = "", web_block: str = "",
                      social_block: str = "", memory_block: str = "") -> str:
    """Assemble the brain tick user prompt: snapshot + wallet + optional intel."""
    parts = [
        "SCREENER SNAPSHOT (the only numbers you may cite):",
        snapshot_block or "- (no rows)",
        "",
        "YOUR BOOK (reason about it, do not recite the balance):",
        wallet_block or "- no open positions. you are flat.",
    ]
    if crowd_block:
        parts += ["", "CROWD BOARD (theses written by others, weigh by their p&l):",
                  crowd_block]
    if web_block:
        parts += ["", "WEB (last 24h, evidence only):", web_block]
    if social_block:
        parts += ["", "SOCIAL READ (evidence only):", social_block]
    if memory_block:
        parts += ["", "MEMORY (context only):", memory_block]
    parts += ["", "Produce the next tick."]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Parse + validate the model's tick (defense-first rule 1 & 2). Every field is
# schema/type/range checked; anything malformed is discarded (returns None) so
# the caller fails CLOSED to the template pass. A verdict for a symbol that was
# not in the snapshot is dropped (the model may not invent names).
# ---------------------------------------------------------------------------

_VALID_CALLS = ("buying", "stalking", "pass", "holding")
_MAX_THOUGHTS = 12
_MAX_VERDICTS = 8
_MAX_CHECKS = 8
_MAX_WATCH = 5
# Cap on how many candidates the single brain call grades. Bounds the JSON
# output so it fits the token budget (a truncated body fails closed). The
# highest-momentum rows are graded; the rest fall back to the per-candidate
# thinker in main.py.
_MAX_BRAIN_CANDIDATES = 8


@dataclass
class LLMVerdict:
    symbol: str
    call: str                      # buying | stalking | pass | holding
    checks: list = field(default_factory=list)
    entry: Optional[str] = None
    invalidation: Optional[str] = None
    reason: str = ""

    @property
    def wants_entry(self) -> bool:
        """The ONLY signal main.py consumes. True only for a valid 'buying'.
        The deterministic gate still must pass before any entry (rule 3)."""
        return self.call == "buying"


@dataclass
class LLMBrainResult:
    verdicts: dict                 # symbol(upper) -> LLMVerdict
    thoughts: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    theses: list = field(default_factory=list)
    watchlist: list = field(default_factory=list)
    remember: list = field(default_factory=list)
    fomo: Optional[int] = None
    break_taking: bool = False
    break_minutes: int = 0
    break_reason: str = ""
    source: str = "template"       # honest resolution label
    degraded: bool = True
    llm_usage: Optional[LLMResult] = None

    def verdict_for(self, symbol: str) -> Optional[LLMVerdict]:
        return self.verdicts.get(symbol.upper())


def _str_list(v, cap: int) -> list:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if isinstance(x, (str, int, float))
            and str(x).strip()][:cap]


def parse_llm_tick(raw: str, valid_symbols: set) -> Optional[dict]:
    """Extract + validate the brain tick JSON. Returns a normalized dict or None
    (None => caller fails closed). `valid_symbols` is the UPPER-cased ticker set
    actually passed to the model."""
    if not raw:
        return None
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None

    # verdicts — the decision-critical field. Validate each hard.
    verdicts: dict = {}
    raw_verdicts = obj.get("verdicts")
    if isinstance(raw_verdicts, list):
        for v in raw_verdicts[:_MAX_VERDICTS]:
            if not isinstance(v, dict):
                continue
            symbol = str(v.get("symbol") or "").strip().upper().lstrip("$")
            call = str(v.get("call") or "").strip().lower()
            if not symbol or symbol not in valid_symbols:
                continue                      # invented/unknown name -> drop
            if call not in _VALID_CALLS:
                continue                      # invalid call -> drop (fail closed)
            verdicts[symbol] = LLMVerdict(
                symbol=symbol,
                call=call,
                checks=_str_list(v.get("checks"), _MAX_CHECKS),
                entry=(str(v.get("entry")).strip()
                       if isinstance(v.get("entry"), str) else None),
                invalidation=(str(v.get("invalidation")).strip()
                              if isinstance(v.get("invalidation"), str) else None),
                reason=(str(v.get("reason")).strip()
                        if isinstance(v.get("reason"), str) else ""),
            )

    fomo = obj.get("fomo")
    fomo = int(max(0, min(100, fomo))) if isinstance(fomo, (int, float)) else None

    brk = obj.get("break") or {}
    if not isinstance(brk, dict):
        brk = {}

    return {
        "verdicts": verdicts,
        "thoughts": _str_list(obj.get("thoughts"), _MAX_THOUGHTS),
        "actions": [a for a in (obj.get("actions") or [])
                    if isinstance(a, dict)][:4],
        "theses": [t for t in (obj.get("theses") or [])
                   if isinstance(t, dict)][:3],
        "watchlist": [w for w in (obj.get("watchlist") or [])
                      if isinstance(w, dict)][:_MAX_WATCH],
        "remember": [m for m in (obj.get("remember") or [])
                     if isinstance(m, dict)][:2],
        "fomo": fomo,
        "break_taking": bool(brk.get("taking")),
        "break_minutes": min(max(int(brk.get("minutes") or 0), 0), 60),
        "break_reason": str(brk.get("reason") or ""),
    }



class LLMBrain:
    """The reference-style tick brain. One role-routed reasoning call per tick grades
    every candidate and emits the full brain output. Fail-closed: mock mode, a
    keyless/unreachable provider, or any unparsable answer yields an EMPTY
    verdict map tagged degraded — main.py then falls back to the deterministic
    template per candidate, so a bad model answer can never become a buy."""

    def __init__(self) -> None:
        self._system = LLM_SYSTEM + LLM_OUTPUT_CONTRACT

    async def tick(
        self,
        candidates: list[Candidate],
        portfolio=None,
        crowd_block: str = "",
        web_block: str = "",
        social_block: str = "",
        memory_block: str = "",
        recent_fills: Optional[list] = None,
    ) -> LLMBrainResult:
        valid_symbols = {c.symbol.upper() for c in candidates}

        # Hermetic: mock mode never touches a provider (defense-first + tests).
        if config.DATA_BACKEND != "live" or not config.LLM_BRAIN:
            return LLMBrainResult(verdicts={}, source="template", degraded=True)
        if not candidates:
            return LLMBrainResult(verdicts={}, source="template", degraded=True)

        # Bound the board the brain grades so the single JSON output stays within
        # the token budget (a truncated body fails closed). Highest 1h-volume rows
        # first; the rest fall back to the per-candidate thinker in main.py.
        if len(candidates) > _MAX_BRAIN_CANDIDATES:
            candidates = sorted(candidates,
                                key=lambda c: (c.volume_1h_usd or 0.0),
                                reverse=True)[:_MAX_BRAIN_CANDIDATES]
            valid_symbols = {c.symbol.upper() for c in candidates}

        user_prompt = build_tick_prompt(
            snapshot_block=build_snapshot_block(candidates),
            wallet_block=build_wallet_block(portfolio, recent_fills),
            crowd_block=crowd_block,
            web_block=web_block,
            social_block=social_block,
            memory_block=memory_block,
        )

        result, resolved = await run_role(
            role="reasoning",
            task="llm_brain",
            system_prompt=self._system,
            user_prompt=user_prompt,
            budget=config.LLM_BRAIN_MAX_TOKENS,
            json_mode=True,
            timeout_seconds=config.LLM_BRAIN_TIMEOUT_SECONDS,
        )
        if result is None or not result.text:
            log.warning("llm_brain degraded (no result): %s", resolved.label)
            return LLMBrainResult(verdicts={}, source=resolved.label,
                                  degraded=True, llm_usage=result)

        parsed = parse_llm_tick(result.text, valid_symbols)
        if parsed is None:
            log.warning("llm_brain unparsable tick -> fail closed "
                        "(provider=%s finish=%s len=%d) head=%r",
                        resolved.label, result.finish_reason,
                        len(result.text or ""), (result.text or "")[:160])
            return LLMBrainResult(verdicts={}, source=resolved.label,
                                  degraded=True, llm_usage=result)

        return LLMBrainResult(
            verdicts=parsed["verdicts"],
            thoughts=parsed["thoughts"],
            actions=parsed["actions"],
            theses=parsed["theses"],
            watchlist=parsed["watchlist"],
            remember=parsed["remember"],
            fomo=parsed["fomo"],
            break_taking=parsed["break_taking"],
            break_minutes=parsed["break_minutes"],
            break_reason=parsed["break_reason"],
            source=resolved.label,
            degraded=resolved.degraded,
            llm_usage=result,
        )





log = logging.getLogger(__name__)
