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
    - deepseek — direct API JSON completion via a persistent client. Live mode
                             only. The model narrates an already-decided result.
  - template — deterministic synthesizer used in mock mode or when Ollama is
               unreachable. Grounded BY CONSTRUCTION (it only formats the
               rule details it was handed).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

import config
from llm.client import DeepSeekClient, LLMResult
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


class NarrationResult:
    def __init__(self, thesis: str, source: str, grounding_flags: list[str], llm_usage: Optional[LLMResult] = None):
        self.thesis = thesis
        self.source = source                    # "ollama:<model>" | "template"
        self.grounding_flags = grounding_flags
        self.llm_usage = llm_usage


def build_prompt(gate: GateDecision) -> str:
    verdict = "ENTER" if gate.all_passed else "REJECT"
    lines = [
        f"- {r.rule_id}: {'PASS' if r.passed else 'FAIL'} ({r.detail})"
        for r in gate.rules
    ]
    return NARRATION_PROMPT.format(verdict=verdict, rules_block="\n".join(lines))


def _template_thesis(gate: GateDecision) -> str:
    """Deterministic thesis grounded by construction."""
    c = gate.candidate
    if gate.all_passed:
        supporting = [r for r in gate.rules if r.passed][:3]
        bits = "; ".join(r.detail for r in supporting)
        return f"Entered {c.symbol}: {bits}."
    failing = [r for r in gate.rules if not r.passed]
    named = "; ".join(f"{r.rule_id} ({r.detail})" for r in failing[:2])
    return f"Rejected {c.symbol}: failed {len(failing)} check(s) — {named}."


class Narrator:
    """One client per process, reused across every call (D3)."""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._deepseek = DeepSeekClient()
        self._ollama_ok: Optional[bool] = None   # None = unchecked

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
        await self._deepseek.aclose()

    async def check_ollama_health(self) -> bool:
        """GET /api/tags with a short timeout; cached between ticks (D4)."""
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
                    "think": False,   # qwen3: skip the long <think> block (~23 tok/s!)
                    "options": {
                        "temperature": 0.2,
                        # Explicit small context window: KV-cache RAM scales
                        # with num_ctx, not prompt length (see config comment).
                        "num_ctx": config.OLLAMA_NUM_CTX,
                        "num_predict": config.OLLAMA_NUM_PREDICT,
                    },
                },
            )
            resp.raise_for_status()
            text = (resp.json().get("response") or "").strip()
            # Belt-and-suspenders: strip any residual <think>…</think> block
            # (older Ollama versions ignore the think flag but honor /no_think,
            # which is appended to the prompt in build_prompt).
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
            return text.strip() or None
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("ollama generate failed: %s", exc)
            return None

    async def narrate(self, gate: GateDecision) -> NarrationResult:
        """
        Mock mode: template only (fully offline, deterministic).
        Live mode: DeepSeek first, template fallback when unreachable/empty.
        """
        detail_strings = [r.detail for r in gate.rules]
        rule_ids = [r.rule_id for r in gate.rules]

        thesis: Optional[str] = None
        source = ""
        if config.DATA_BACKEND == "live":
            result = await self._deepseek.complete_json(
                task="narrator",
                system_prompt="You narrate an already-decided paper-trading result. Reply with plain text only.",
                user_prompt=build_prompt(gate),
                json_mode=False,
            )
            thesis = result.text if result else None
            source = f"deepseek:{config.DEEPSEEK_MODEL}" if thesis else ""
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
        n = Narrator()
        try:
            result = await n._deepseek.complete_json(
                task="reflection",
                system_prompt="Reflect on the closed paper trade using only the supplied data.",
                user_prompt=prompt,
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

