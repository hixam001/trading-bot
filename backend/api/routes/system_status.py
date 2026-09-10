"""
api/routes/system_status.py — GET /api/system-status (D4): active provider
reachability, per-provider daily call counts.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request

import config
from api import db

log = logging.getLogger(__name__)
router = APIRouter()

# §60: process-level TTL cache for the LLM health verdict. The client's own
# health() caches its verdict forever after the first probe (§56 deferred
# item), so system-status must re-probe here on its own cadence: a provider
# that goes down or recovers surfaces within LLM_HEALTH_TTL_SECONDS.
_health_cache: dict = {"ok": None, "at": 0.0}


async def _llm_health(narrator) -> bool:
    """TTL-cached main-LLM reachability (see _health_cache)."""
    now = time.monotonic()
    ttl = float(getattr(config, "LLM_HEALTH_TTL_SECONDS", 300.0))
    if _health_cache["ok"] is not None and now - _health_cache["at"] < ttl:
        return bool(_health_cache["ok"])
    try:
        ok = await narrator._main_llm.health()
    except Exception:
        log.warning("system-status: LLM health probe failed", exc_info=True)
        ok = False
    _health_cache.update(ok=bool(ok), at=now)
    return bool(ok)


@router.get("/api/system-status")
async def get_system_status(request: Request):
    narrator = request.app.state.narrator
    # Health check via the active main provider (Groq / DeepSeek).
    # The old Ollama-specific check_ollama_health() was removed post-Groq
    # migration; we now probe the main LLM client directly — through the
    # §60 TTL cache so the truth can move again (see _health_cache).
    main_llm_ok = await _llm_health(narrator)
    async with db.get_db() as conn:
        providers = await db.get_provider_call_summary(conn)
        llm_usage = await db.get_llm_call_usage(conn, limit=100)
    return {
        # §52: the paper book is retired — this deployment runs the LIVE book
        # (stats/holdings report the same). The old config.PAPER_TRADING_ONLY
        # stays hardcoded True in config.py as the retired paper-safety gate;
        # surfacing it here as "paper" contradicted the live truth.
        "paper_trading_only": False,
        "data_backend": config.DATA_BACKEND,
        "main_llm_reachable": main_llm_ok,
        "main_llm_provider": config.MAIN_LLM_PROVIDER,
        # Live mode narrates via whichever main provider is configured
        # (MAIN_LLM_PROVIDER: deepseek | groq); mock mode is template-only.
        "narration_mode": config.MAIN_LLM_PROVIDER if config.DATA_BACKEND == "live" else "template",
        "provider_calls_today": providers,
        "llm_usage_recent": llm_usage,
        "tick_interval_seconds": config.TICK_INTERVAL_SECONDS,
    }
