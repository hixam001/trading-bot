"""Shared OpenAI-compatible JSON client.

Main-path adapters (thinker / narrator / reflections) are selected by
`config.MAIN_LLM_PROVIDER` via `build_main_client()`:
  - `MainGroqClient`   — Groq direct API (warm rollback path)
  - `DeepSeekClient`   — DeepSeek direct API, non-thinking mode (docs/08 §1)
The social evidence read keeps its own adapter (`GroqClient` via
`SOCIAL_LLM_*`) and is never affected by the main-provider flip.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Dict
from datetime import datetime, timezone

import httpx

import config


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    task: str
    request_id: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cache_hit_tokens: Optional[int] = None
    latency_ms: float = 0.0
    response_hash: str = ""
    estimated_cost_usd: float = 0.0
    pricing_snapshot_id: str = ""
    http_status: Optional[int] = None
    retry_count: int = 0
    is_peak_window: bool = False
    finish_reason: str = ""
    validation_error: Optional[str] = None
    degradation_reason: Optional[str] = None


def _is_peak_window() -> bool:
    """DeepSeek peak window: 01:00-04:00 UTC and 06:00-10:00 UTC on weekdays."""
    now = datetime.now(timezone.utc)
    # Weekday 0-4 (Monday to Friday)
    if now.weekday() > 4:
        return False
    hour = now.hour
    if 1 <= hour < 4 or 6 <= hour < 10:
        return True
    return False

def _estimate_cost(provider: str, input_tokens: int, output_tokens: int, cache_tokens: int, is_peak: bool) -> float:
    # Groq qwen3.8-27b pricing: ~$0.80/1M input, $4.00/1M output (approximate)
    if provider == "groq":
        return (input_tokens / 1_000_000.0) * 0.80 + (output_tokens / 1_000_000.0) * 4.00
    # DeepSeek V4 Flash, per api-docs.deepseek.com verified 2026-08-27:
    # off-peak $0.22/1M cache-miss input, $0.007/1M cache-hit input,
    # $0.66/1M output; peak hours are exactly double (01:00-04:00 and
    # 06:00-10:00 UTC Mon-Fri, see _is_peak_window). `input_tokens` arrives
    # with cache tokens already subtracted (see complete_json).
    if provider == "deepseek":
        mult = 2.0 if is_peak else 1.0
        return (
            (input_tokens / 1_000_000.0) * 0.22 * mult
            + (cache_tokens / 1_000_000.0) * 0.007 * mult
            + (output_tokens / 1_000_000.0) * 0.66 * mult
        )
    return 0.0


# Pricing snapshot ids stamped on every LLMResult. Groq keeps its original
# snapshot (rates unchanged); DeepSeek's was verified on 2026-08-27.
_PRICING_SNAPSHOT: dict[str, str] = {
    "groq": "groq_20260826",
    "deepseek": "deepseek_20260827",
}


class LLMClient:
    def __init__(
        self,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        client: Optional[httpx.AsyncClient] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = client
        self._ok: Optional[bool] = None
        # Bounded per-provider timeout (performance-discipline rule: every
        # external call has a defined timeout). Main clients pass their own
        # provider value; the social client falls back to SOCIAL_LLM_*.
        self.timeout_seconds: float = (
            timeout_seconds if timeout_seconds is not None else config.SOCIAL_LLM_TIMEOUT_SECONDS
        )

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def health(self) -> bool:
        if self._ok is not None:
            return self._ok
        if not self.api_key:
            self._ok = False
            return False
        try:
            resp = await self.client.get(f"{self.base_url}/models", headers=self._headers(), timeout=10.0)
            self._ok = resp.status_code == 200
        except httpx.HTTPError:
            self._ok = False
        return self._ok

    async def complete_json(
        self, task: str, system_prompt: str, user_prompt: str, budget: Optional[int] = None, json_mode: bool = True
    ) -> Optional[LLMResult]:
        if not self.api_key:
            return None
        
        max_tokens = budget or 200
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.provider == "deepseek":
            # NON-THINKING MODE ONLY (docs/08 §1, handoff §18): V4 Flash
            # defaults to thinking mode, which burns the small output budget
            # on reasoning_content and returns an empty `content` — forcing
            # a fail-closed template pass on every call.
            payload["thinking"] = {"type": "disabled"}
            
        started = time.monotonic()
        # DeepSeek bills peak hours (01:00-04:00 + 06:00-10:00 UTC Mon-Fri)
        # at 2x off-peak rates; Groq has no peak pricing. The flag is stamped
        # on the result and drives the cost estimate below.
        is_peak = _is_peak_window() if self.provider == "deepseek" else False
        
        try:
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout_seconds,
            )
            
            body: dict[str, Any] = {}
            if response.status_code == 200:
                body = response.json()
                
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices:
                return LLMResult(
                    text="", provider=self.provider, model=self.model, task=task,
                    http_status=response.status_code, latency_ms=(time.monotonic() - started) * 1000.0,
                    degradation_reason="unparsable_response" if response.status_code == 200 else f"http_{response.status_code}"
                )
                
            message = choices[0].get("message")
            text = message.get("content") if isinstance(message, dict) else None
            if not isinstance(text, str) or not text.strip():
                return LLMResult(
                    text="", provider=self.provider, model=self.model, task=task,
                    http_status=response.status_code, latency_ms=(time.monotonic() - started) * 1000.0,
                    degradation_reason="empty_content"
                )
                
            usage = body.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            
            in_tok = _int_or_none(usage.get("prompt_tokens")) or 0
            out_tok = _int_or_none(usage.get("completion_tokens")) or 0
            tot_tok = _int_or_none(usage.get("total_tokens")) or 0
            cache_tok = 0
            
            # DeepSeek specific cache tokens
            prompt_cache_details = usage.get("prompt_cache_hit_tokens") or usage.get("prompt_cache_details", {}).get("cached_tokens")
            if prompt_cache_details:
                cache_tok = _int_or_none(prompt_cache_details) or 0
                in_tok -= cache_tok # non-cache tokens
                
            est_cost = _estimate_cost(self.provider, in_tok, out_tok, cache_tok, is_peak)
                
            return LLMResult(
                text=text.strip(),
                provider=self.provider,
                model=self.model,
                task=task,
                request_id=response.headers.get("x-request-id") or body.get("id"),
                input_tokens=in_tok,
                output_tokens=out_tok,
                total_tokens=tot_tok,
                cache_hit_tokens=cache_tok,
                latency_ms=(time.monotonic() - started) * 1000.0,
                response_hash=hashlib.sha256(text.encode()).hexdigest(),
                estimated_cost_usd=est_cost,
                pricing_snapshot_id=_PRICING_SNAPSHOT.get(self.provider, f"{self.provider}_unknown"),
                http_status=response.status_code,
                is_peak_window=is_peak,
                finish_reason=choices[0].get("finish_reason", "")
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as e:
            return LLMResult(
                text="", provider=self.provider, model=self.model, task=task,
                latency_ms=(time.monotonic() - started) * 1000.0,
                degradation_reason=f"exception:{type(e).__name__}"
            )


class MainGroqClient(LLMClient):
    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        super().__init__(
            "groq", config.GROQ_BASE_URL, config.GROQ_API_KEY, config.GROQ_MODEL,
            client, timeout_seconds=config.GROQ_TIMEOUT_SECONDS,
        )
        self.is_main = True


class DeepSeekClient(LLMClient):
    """DeepSeek direct API main adapter (docs/08 §6): OpenAI-compatible
    /chat/completions, JSON output, NON-thinking mode only. Reasoning mode
    stays out of the hot path until an offline benchmark proves a measurable
    gain (docs/08 §1)."""

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        super().__init__(
            "deepseek", config.DEEPSEEK_BASE_URL, config.DEEPSEEK_API_KEY, config.DEEPSEEK_MODEL,
            client, timeout_seconds=config.DEEPSEEK_TIMEOUT_SECONDS,
        )
        self.is_main = True


def main_max_tokens() -> int:
    """Output-token budget for the main path, matched to the provider that
    build_main_client() actually returns (unrecognized values fail closed to
    Groq, so they get the Groq budget)."""
    return config.DEEPSEEK_MAX_TOKENS if config.MAIN_LLM_PROVIDER == "deepseek" else config.GROQ_MAX_TOKENS


def build_main_client(client: Optional[httpx.AsyncClient] = None) -> LLMClient:
    """Factory for the MAIN LLM path (thinker/narrator/reflections), keyed on
    config.MAIN_LLM_PROVIDER. Unrecognized values fail CLOSED to Groq (the
    warm, proven path) and log loudly — a typo in .env must never silently
    route decisions through an unknown provider."""
    provider = config.MAIN_LLM_PROVIDER
    if provider == "deepseek":
        return DeepSeekClient(client)
    if provider != "groq":
        logging.getLogger(__name__).warning(
            "MAIN_LLM_PROVIDER=%r not recognized; failing closed to groq", provider
        )
    return MainGroqClient(client)


class GroqClient(LLMClient):
    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        super().__init__(
            "groq", config.SOCIAL_LLM_BASE_URL, config.SOCIAL_LLM_API_KEY, config.SOCIAL_LLM_MODEL,
            client, timeout_seconds=config.SOCIAL_LLM_TIMEOUT_SECONDS,
        )


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None