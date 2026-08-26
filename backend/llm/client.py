"""Shared OpenAI-compatible JSON client with DeepSeek, Groq, and Template adapters."""
from __future__ import annotations

import hashlib
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
    if provider == "deepseek":
        # $0.22 / 1M cache-miss input, $0.66 / 1M output (off-peak)
        # cache-hit input is $0.007 / 1M
        # Peak rates are double
        mult = 2.0 if is_peak else 1.0
        in_cost = (input_tokens / 1_000_000.0) * 0.22 * mult
        out_cost = (output_tokens / 1_000_000.0) * 0.66 * mult
        cache_cost = (cache_tokens / 1_000_000.0) * 0.007 * mult
        return in_cost + out_cost + cache_cost
    elif provider == "groq":
        # Groq Llama 3 70B: ~$0.59/1M input, $0.79/1M output
        return (input_tokens / 1_000_000.0) * 0.59 + (output_tokens / 1_000_000.0) * 0.79
    return 0.0


class LLMClient:
    def __init__(self, provider: str, base_url: str, api_key: str, model: str, client: Optional[httpx.AsyncClient] = None) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = client
        self._ok: Optional[bool] = None

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
            
        started = time.monotonic()
        is_peak = _is_peak_window() if self.provider == "deepseek" else False
        
        try:
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=config.DEEPSEEK_TIMEOUT_SECONDS if self.provider == "deepseek" else config.SOCIAL_LLM_TIMEOUT_SECONDS,
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
                pricing_snapshot_id=f"{self.provider}_20260826",
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


class DeepSeekClient(LLMClient):
    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        super().__init__("deepseek", config.DEEPSEEK_BASE_URL, config.DEEPSEEK_API_KEY, config.DEEPSEEK_MODEL, client)


class GroqClient(LLMClient):
    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        super().__init__("groq", config.SOCIAL_LLM_BASE_URL, config.SOCIAL_LLM_API_KEY, config.SOCIAL_LLM_MODEL, client)


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None