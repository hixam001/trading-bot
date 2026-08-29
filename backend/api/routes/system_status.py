"""
api/routes/system_status.py — GET /api/system-status (D4): active provider
reachability, per-provider daily call counts.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

import config
from api import db

router = APIRouter()


@router.get("/api/system-status")
async def get_system_status(request: Request):
    narrator = request.app.state.narrator
    # Health check via the active main provider (Groq / DeepSeek).
    # The old Ollama-specific check_ollama_health() was removed post-Groq
    # migration; we now probe the main LLM client directly.
    main_llm_ok = await narrator._main_llm.health()
    async with db.get_db() as conn:
        providers = await db.get_provider_call_summary(conn)
        llm_usage = await db.get_llm_call_usage(conn, limit=100)
    return {
        "paper_trading_only": config.PAPER_TRADING_ONLY,
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
