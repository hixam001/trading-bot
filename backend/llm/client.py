"""Small provider client for validated JSON thesis completions."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

import config


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    request_id: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    latency_ms: float
    response_hash: str


class DeepSeekJSONClient:
    """Direct DeepSeek chat client; callers own schema validation."""

    provider = "deepseek"

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client

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
            "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        }

    async def complete_json(
        self, system_prompt: str, user_prompt: str, *, json_mode: bool = True
    ) -> Optional[LLMResult]:
        if not config.DEEPSEEK_API_KEY:
            return None
        payload = {
            "model": config.DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": config.DEEPSEEK_MAX_TOKENS,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        try:
            response = await self.client.post(
                config.DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=config.DEEPSEEK_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            body: dict[str, Any] = response.json()
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices:
                return None
            message = choices[0].get("message")
            text = message.get("content") if isinstance(message, dict) else None
            if not isinstance(text, str) or not text.strip():
                return None
            usage = body.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            return LLMResult(
                text=text.strip(),
                provider=self.provider,
                model=config.DEEPSEEK_MODEL,
                request_id=response.headers.get("x-request-id") or body.get("id"),
                input_tokens=_int_or_none(usage.get("prompt_tokens")),
                output_tokens=_int_or_none(usage.get("completion_tokens")),
                total_tokens=_int_or_none(usage.get("total_tokens")),
                latency_ms=(time.monotonic() - started) * 1000.0,
                response_hash=hashlib.sha256(text.encode()).hexdigest(),
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return None


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None