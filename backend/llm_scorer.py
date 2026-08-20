"""
llm_scorer.py — Async Ollama scoring module.

Calls the local Qwen3-8B model via Ollama's /api/generate endpoint using a
persistent httpx.AsyncClient (performance-discipline rule 2: reuse connections).

Defense-first rules applied:
  - Every field of the LLM response is validated explicitly before use (rule 1).
  - On any parse/validation failure, returns None (fail closed, rule 2).
  - Full raw response logged on failure so it's debuggable (rule 6).
  - Timeout is explicit (rule 8).

The module manages one shared async client at module scope.
Call close_client() on shutdown to cleanly close the connection pool.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

import httpx

import config
from models import Candidate, Trade, Verdict

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared HTTP client — one per process, reused across all calls
# (performance-discipline rule 2)
# ---------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0,
                read=config.OLLAMA_TIMEOUT_SECONDS,
                write=10.0,
                pool=5.0,
            ),
        )
    return _client


async def close_client() -> None:
    """Close the shared client. Call on application shutdown."""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

async def check_ollama_health() -> dict:
    """
    Check if Ollama is reachable and the configured model is available.
    Returns a dict suitable for the /api/system-status endpoint.
    """
    client = _get_client()
    try:
        resp = await client.get(
            config.OLLAMA_TAGS_ENDPOINT,
            timeout=httpx.Timeout(5.0),
        )
        resp.raise_for_status()
        data = resp.json()
        models = [m.get("name", "") for m in data.get("models", [])]
        model_loaded = any(
            config.MODEL_NAME in m for m in models
        )
        return {
            "ollama_reachable": True,
            "model_name": config.MODEL_NAME,
            "model_loaded": model_loaded,
            "available_models": models,
            "ollama_url": config.OLLAMA_URL,
        }
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        log.warning("Ollama health check failed (connection): %s", exc)
        return {
            "ollama_reachable": False,
            "model_name": config.MODEL_NAME,
            "model_loaded": False,
            "available_models": [],
            "ollama_url": config.OLLAMA_URL,
            "error": str(exc),
        }
    except Exception as exc:
        log.error("Ollama health check unexpected error: %s", exc)
        return {
            "ollama_reachable": False,
            "model_name": config.MODEL_NAME,
            "model_loaded": False,
            "available_models": [],
            "ollama_url": config.OLLAMA_URL,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Scoring prompt builder
# ---------------------------------------------------------------------------

def _build_scoring_prompt(candidate: Candidate, kb_context: str) -> str:
    return f"""You are a Solana memecoin paper-trading analyst. Evaluate the candidate token below for a simulated trade entry.

## Background Knowledge
{kb_context}

## Candidate Token
- Symbol: {candidate.symbol}
- Mint: {candidate.mint_address}
- Price: ${candidate.price_usd:.8f}
- Liquidity: ${candidate.liquidity_usd:,.0f}
- Volume 24h: ${candidate.volume_24h_usd:,.0f}
- Holder count: {candidate.holder_count:,}
- Top single holder: {candidate.top_holder_pct:.1f}%
- Token age: {candidate.age_hours:.1f} hours
- Market cap: ${candidate.market_cap_usd:,.0f}

## Instructions
Respond ONLY with a single valid JSON object — no preamble, no explanation, no markdown fences.
The JSON must contain exactly these fields:

{{
  "verdict": "pass" or "fail",
  "confidence": number between 0.0 and 1.0,
  "risk_flags": ["array", "of", "specific", "risk", "strings"],
  "thesis": "1-2 sentence rationale for the verdict",
  "entry_condition": "specific price/volume trigger for entry (required if verdict=pass, empty string if fail)",
  "invalidation_condition": "what would prove this thesis wrong"
}}
"""


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------

def _validate_verdict_json(raw: dict, candidate: Candidate) -> Optional[Verdict]:
    """
    Explicitly validate every field of the LLM's JSON response.
    Returns None on any validation failure (fail closed, defense-first rule 1).
    Never uses .get() with a default on money-relevant fields.
    """
    # verdict
    if "verdict" not in raw:
        log.warning("LLM response missing 'verdict' field")
        return None
    verdict_val = raw["verdict"]
    if verdict_val not in ("pass", "fail"):
        log.warning("LLM 'verdict' invalid: %r (must be 'pass' or 'fail')", verdict_val)
        return None

    # confidence
    if "confidence" not in raw:
        log.warning("LLM response missing 'confidence' field")
        return None
    try:
        confidence = float(raw["confidence"])
    except (TypeError, ValueError):
        log.warning("LLM 'confidence' not a number: %r", raw["confidence"])
        return None
    if not (0.0 <= confidence <= 1.0):
        log.warning("LLM 'confidence' out of range: %f", confidence)
        return None

    # risk_flags
    if "risk_flags" not in raw:
        log.warning("LLM response missing 'risk_flags' field")
        return None
    risk_flags = raw["risk_flags"]
    if not isinstance(risk_flags, list):
        log.warning("LLM 'risk_flags' not a list: %r", risk_flags)
        return None
    risk_flags = [str(f) for f in risk_flags]  # coerce to strings

    # thesis
    if "thesis" not in raw:
        log.warning("LLM response missing 'thesis' field")
        return None
    thesis = str(raw["thesis"]).strip()
    if not thesis:
        log.warning("LLM 'thesis' is empty")
        return None

    # entry_condition
    entry_condition = str(raw.get("entry_condition", "")).strip()
    if verdict_val == "pass" and not entry_condition:
        log.warning("LLM verdict=pass but 'entry_condition' is empty")
        return None

    # invalidation_condition
    invalidation_condition = str(raw.get("invalidation_condition", "")).strip()

    return Verdict(
        candidate=candidate,
        verdict=verdict_val,
        confidence=confidence,
        risk_flags=risk_flags,
        thesis=thesis,
        entry_condition=entry_condition,
        invalidation_condition=invalidation_condition,
    )


def _extract_json_from_response(text: str) -> Optional[dict]:
    """
    Extract a JSON object from the LLM's response text.
    Handles cases where the model wraps output in markdown code fences
    despite being instructed not to.
    """
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown fences if present
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find bare JSON object
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

async def score_candidate(candidate: Candidate, kb_context: str) -> Optional[Verdict]:
    """
    Score a single candidate using the local Ollama LLM.

    Returns a validated Verdict, or None if the LLM call fails or returns
    a malformed response (fail closed — the candidate is skipped).

    Timing is logged so the tick loop can track LLM latency (performance-
    discipline rule 7: measure before optimizing).
    """
    prompt = _build_scoring_prompt(candidate, kb_context)
    client = _get_client()
    t_start = time.monotonic()

    payload = {
        "model": config.MODEL_NAME,
        "prompt": prompt,
        "stream": False,
        "format": "json",  # ask Ollama to enforce JSON output mode
        "options": {
            "temperature": 0.3,   # lower temperature for more consistent JSON
            "num_predict": 512,
        },
    }

    try:
        resp = await client.post(
            config.OLLAMA_GENERATE_ENDPOINT,
            json=payload,
            timeout=httpx.Timeout(
                connect=5.0,
                read=config.OLLAMA_TIMEOUT_SECONDS,
                write=10.0,
                pool=5.0,
            ),
        )
        elapsed = time.monotonic() - t_start
        resp.raise_for_status()

    except httpx.TimeoutException:
        log.error(
            "Ollama timeout scoring %s after %.1fs (model=%s)",
            candidate.symbol,
            time.monotonic() - t_start,
            config.MODEL_NAME,
        )
        return None
    except httpx.ConnectError:
        log.error(
            "Ollama connection error scoring %s — is Ollama running at %s?",
            candidate.symbol,
            config.OLLAMA_URL,
        )
        return None
    except httpx.HTTPStatusError as exc:
        log.error("Ollama HTTP %d scoring %s: %s", exc.response.status_code, candidate.symbol, exc)
        return None

    try:
        response_body = resp.json()
        raw_text: str = response_body.get("response", "")
    except Exception as exc:
        log.error("Failed to decode Ollama response body for %s: %s", candidate.symbol, exc)
        return None

    log.info(
        "Ollama scored %s in %.2fs (%d chars response)",
        candidate.symbol,
        elapsed,
        len(raw_text),
    )

    parsed = _extract_json_from_response(raw_text)
    if parsed is None:
        log.warning(
            "Could not extract JSON from Ollama response for %s. Raw: %r",
            candidate.symbol,
            raw_text[:500],
        )
        return None

    verdict = _validate_verdict_json(parsed, candidate)
    if verdict is None:
        log.warning(
            "Verdict validation failed for %s. Parsed JSON: %r",
            candidate.symbol,
            parsed,
        )
        return None

    verdict.raw_llm_response = raw_text
    return verdict


# ---------------------------------------------------------------------------
# Post-trade reflection (FR-26)
# ---------------------------------------------------------------------------

def _build_reflection_prompt(trade: Trade) -> str:
    entry_p = trade.entry_price_usd
    exit_p = trade.exit_price_usd or 0.0
    pnl_usd = trade.realized_pnl_usd or 0.0
    pnl_pct = trade.realized_pnl_pct or 0.0
    thesis = trade.verdict_snapshot.get("thesis", "No thesis recorded.")

    # Compute hold duration
    try:
        from datetime import datetime, timezone
        opened = datetime.fromisoformat(trade.opened_at)
        closed = datetime.fromisoformat(trade.closed_at or trade.opened_at)
        hold_hours = (closed - opened).total_seconds() / 3600
    except Exception:
        hold_hours = 0.0

    return f"""You previously issued a verdict on a Solana memecoin paper trade.

## Your Original Thesis
{thesis}

## Trade Outcome
- Symbol: {trade.symbol}
- Entry price: ${entry_p:.8f}
- Exit price: ${exit_p:.8f}
- Realized P&L: ${pnl_usd:+.4f} ({pnl_pct:+.1f}%)
- Exit reason: {trade.exit_reason or "unknown"}
- Held for: {hold_hours:.1f} hours

Write exactly 1-2 sentences reflecting on what this outcome reveals about your original assessment. \
Be specific: what did you get right, what did you miss, and what would you look for differently next time? \
Plain text only — no JSON, no lists, no formatting.
"""


async def generate_reflection(trade: Trade) -> Optional[str]:
    """
    Generate a short post-trade reflection from the LLM.

    Returns a plain-text string (1-2 sentences), or None on any failure.
    Called fire-and-forget from the tick loop — failures are logged but
    do not affect the trade record's core fields (FR-27).
    """
    if trade.closed_at is None or trade.realized_pnl_usd is None:
        log.warning("generate_reflection called on unclosed trade %s — skipping", trade.trade_id)
        return None

    prompt = _build_reflection_prompt(trade)
    client = _get_client()

    payload = {
        "model": config.MODEL_NAME,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.5,
            "num_predict": 200,
        },
    }

    try:
        resp = await client.post(
            config.OLLAMA_GENERATE_ENDPOINT,
            json=payload,
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        if not raw:
            log.warning("Empty reflection from Ollama for trade %s", trade.trade_id)
            return None
        # Trim to ~300 chars if the model over-generates
        if len(raw) > 400:
            raw = raw[:400].rsplit(".", 1)[0] + "."
        return raw
    except Exception as exc:
        log.warning("Reflection failed for trade %s: %s", trade.trade_id, exc)
        return None
