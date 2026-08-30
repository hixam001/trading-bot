"""
llm/narrator.py — thesis generation from an ALREADY-COMPUTED GateDecision
(D1–D4 / §4.1).

Hard rules for this module:
  - The verdict is decided by the rule engine. The narrator NEVER re-decides,
    re-scores, or overrides; it narrates a decision that has already been made.
  - The LLM receives ONLY the rule results (ids, pass/fail, details with the
    real numbers) — nothing else to hallucinate from.
  - Output is validated: non-empty; groundedness-checked against the actual
    rule list; flags are recorded on the feed event, never silently dropped.

Two backends:
    - main provider — direct API JSON completion via a persistent client
                (Groq or DeepSeek per MAIN_LLM_PROVIDER). Live mode only.
                The model narrates an already-decided result.
    - template — deterministic synthesizer used in mock mode or when the
                provider is unreachable. Grounded BY CONSTRUCTION (it only
                formats the rule details it was handed).
"""
from __future__ import annotations

import logging
from typing import Optional

import config
from llm.client import build_main_client, main_max_tokens, LLMResult, _is_peak_window
from llm.grounding import validate_numbers, validate_thesis
from models import GateDecision

log = logging.getLogger(__name__)

NARRATION_PROMPT = """\
You are narrating a trading decision that has ALREADY been made by a \
deterministic rule system. Do not second-guess the verdict. Do not invent \
information not present below. Write 1-2 sentences explaining the decision \
using ONLY the rule results and numbers given.

Verdict: {verdict}
Rules checked:
{rules_block}

If REJECT: name the specific failing rule(s), using their actual numbers.
If ENTER: state which factors support entry, using actual numbers.
Do not mention any check not listed above. /no_think"""


# ---------------------------------------------------------------------------
# Item 1 — narration anti-repetition (omo cabin-ritual parity, lightweight).
#
# Consecutive narrations were sounding identical: the template always opened
# "Entered X:" / "Rejected X:" and the LLM was given no instruction to vary
# its angle. We rotate a small set of *style angles* (which factor to lead
# with / how to frame it) through both paths so back-to-back decisions read
# distinctly. Purely cosmetic: the angle never changes WHICH rules are cited
# or the verdict — grounding is unaffected, and every angle still instructs
# the model to use only the given numbers.
# ---------------------------------------------------------------------------

# Style angles for the LLM path. Each is a short framing instruction; they
# rotate so the same verdict is not always led the same way.
_ANGLES = (
    "Lead with the single strongest factor and keep it to one crisp sentence.",
    "Frame it as risk-first: what had to be true for this verdict to stand.",
    "Lead with the flow/pressure picture, then the supporting number.",
    "Be terse and factual; state the verdict, then the deciding number.",
    "Lead with the liquidity/depth picture, then the supporting factor.",
)

# Opener variants for the deterministic template path (degraded/mock mode).
_ENTER_OPENERS = (
    "Entered {sym}: {bits}.",
    "{sym} entry — {bits}.",
    "Taking a position in {sym}: {bits}.",
)
_REJECT_OPENERS = (
    "Rejected {sym}: failed {n} check(s) — {named}.",
    "{sym} turned away — {n} check(s) failed: {named}.",
    "Passing on {sym}: {named}.",
)

_angle_index = 0


def _next_angle() -> int:
    """Advance the rotation and return the index to use for this narration."""
    global _angle_index
    idx = _angle_index % len(_ANGLES)
    _angle_index += 1
    return idx



class NarrationResult:
    def __init__(self, thesis: str, source: str, grounding_flags: list[str], llm_usage: Optional[LLMResult] = None):
        self.thesis = thesis
        self.source = source                    # "<provider>:<model>" | "template" | "degraded:*"
        self.grounding_flags = grounding_flags
        self.llm_usage = llm_usage


def build_prompt(gate: GateDecision) -> str:
    verdict = "ENTER" if gate.all_passed else "REJECT"
    lines = [
        f"- {r.rule_id}: "
        + ("NOT EVALUATED" if not r.evaluated
           else ("PASS" if r.passed else "FAIL"))
        + f" ({r.detail})"
        for r in gate.rules
    ]
    base = NARRATION_PROMPT.format(verdict=verdict, rules_block="\n".join(lines))
    # Item 1: rotate the framing angle so consecutive narrations differ. The
    # angle is style-only — it never adds facts or changes the verdict.
    angle = _ANGLES[_next_angle()]
    return f"{base}\nStyle for this one: {angle}"


def _template_thesis(gate: GateDecision) -> str:
    """Deterministic thesis grounded by construction.

    Item 1: the opener rotates through a small set of variants so consecutive
    degraded narrations do not all start identically. The cited rule details
    are unchanged — only the framing sentence varies.
    """
    c = gate.candidate
    if gate.all_passed:
        supporting = [r for r in gate.rules if r.passed][:3]
        bits = "; ".join(r.detail for r in supporting)
        opener = _ENTER_OPENERS[_next_angle() % len(_ENTER_OPENERS)]
        return opener.format(sym=c.symbol, bits=bits)
    # §43: a rule the pipeline deliberately did not evaluate (crowd feed
    # reserved for shortlisted candidates) is NOT a rejection reason — never
    # cite it as one. It still appears in the journal's rule breakdown.
    failing = [r for r in gate.rules if not r.passed and r.evaluated]
    if not failing:
        # Only an un-evaluated rule stands between this candidate and entry
        # (§43 fail-closed path — e.g. the crowd feed was never queried).
        skipped = [r for r in gate.rules if not r.evaluated]
        named = "; ".join(f"{r.rule_id} ({r.detail})" for r in skipped[:2])
        return f"Passing on {c.symbol}: {named}." if named else (
            f"Passing on {c.symbol}: no rule cleared it for entry.")
    named = "; ".join(f"{r.rule_id} ({r.detail})" for r in failing[:2])
    opener = _REJECT_OPENERS[_next_angle() % len(_REJECT_OPENERS)]
    return opener.format(sym=c.symbol, n=len(failing), named=named)


class Narrator:
    """One client per process, reused across every call (D3)."""

    def __init__(self) -> None:
        self._main_llm = build_main_client()

    async def aclose(self) -> None:
        await self._main_llm.aclose()


    async def narrate(self, gate: GateDecision) -> NarrationResult:
        """
        Mock mode: template only (fully offline, deterministic).
        Live mode: the configured main provider first, template fallback when
        unreachable/empty.
        """
        detail_strings = [r.detail for r in gate.rules]
        rule_ids = [r.rule_id for r in gate.rules]

        thesis: Optional[str] = None
        source = ""
        if config.DATA_BACKEND == "live":
            result = await self._main_llm.complete_json(
                task="narrator",
                system_prompt="You narrate an already-decided paper-trading result. Reply with plain text only.",
                user_prompt=build_prompt(gate),
                json_mode=False,
            )
            thesis = result.text if result else None
            source = f"{self._main_llm.provider}:{self._main_llm.model}" if thesis else ""
            llm_usage = result

        if not thesis:
            thesis = _template_thesis(gate)
            source = f"degraded:{result.degradation_reason}" if 'result' in locals() and result else "template"

        flags: list[str] = []
        if not thesis.strip():
            flags.append("empty thesis")
            thesis = _template_thesis(gate)
            source = "template"
        else:
            flags.extend(validate_thesis(thesis, rule_ids))
            flags.extend(validate_numbers(thesis, detail_strings))
        if flags:
            log.warning("narration grounding flags for %s: %s",
                        gate.candidate.symbol, flags)

        return NarrationResult(thesis.strip(), source, flags, llm_usage=locals().get('llm_usage'))


# ---------------------------------------------------------------------------
# Post-close reflection (D5/D6) — fire-and-forget, never blocks the tick loop
# ---------------------------------------------------------------------------

REFLECTION_PROMPT = """\
A paper trade just closed. In 2-3 sentences, reflect using ONLY the data \
below: did the outcome match what the entry rules suggested? What would you \
note for future threshold review? Do not invent information.

Entry rules: {rule_summary}
Outcome: exited via {exit_reason} at ${exit_price:.8f} after entry ${entry_price:.8f}; \
realized P&L ${pnl_usd:+.4f} ({pnl_pct:+.1f}%).
"""


async def generate_reflection(trade, rule_summary: str) -> str:
    prompt = REFLECTION_PROMPT.format(
        rule_summary=rule_summary,
        exit_reason=trade.exit_reason,
        exit_price=trade.exit_price_usd or 0.0,
        entry_price=trade.entry_price_usd,
        pnl_usd=trade.realized_pnl_usd or 0.0,
        pnl_pct=trade.realized_pnl_pct or 0.0,
    )
    if config.DATA_BACKEND == "live":
        # Reflections are never time-critical (docs/08 §5): when the main
        # provider is DeepSeek, skip non-urgent reflections during peak
        # windows instead of paying 2x rates. Logged, never silent.
        if config.MAIN_LLM_PROVIDER == "deepseek" and _is_peak_window():
            log.info(
                "reflection for %s skipped to template: deepseek peak window",
                trade.symbol,
            )
        else:
            n = Narrator()
            try:
                result = await n._main_llm.complete_json(
                    task="reflection",
                    system_prompt="Reflect on the closed paper trade using only the supplied data.",
                    user_prompt=prompt,
                    budget=main_max_tokens(),
                    json_mode=False,
                )
                if result and result.text:
                    return result.text
            finally:
                await n.aclose()
    pnl_pct = trade.realized_pnl_pct or 0.0
    return (
        f"[template reflection] Exited {trade.symbol} via {trade.exit_reason} "
        f"with {pnl_pct:+.1f}% realized P&L. Entry rules said: {rule_summary}"
    )

