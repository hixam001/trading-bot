"""
api/routes/system_status.py — GET /api/system-status (D4): Ollama
reachability, active model, per-provider daily call counts.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

import config
from api import db

router = APIRouter()


@router.get("/api/system-status")
async def get_system_status(request: Request):
    narrator = request.app.state.narrator
    ollama_ok = await narrator.check_ollama_health()
    async with db.get_db() as conn:
        providers = await db.get_provider_call_summary(conn)
    return {
        "paper_trading_only": config.PAPER_TRADING_ONLY,
        "data_backend": config.DATA_BACKEND,
        "ollama_reachable": ollama_ok,
        "model": config.MODEL_NAME,
        "narration_mode": "ollama" if (config.DATA_BACKEND == "live" and ollama_ok) else "template",
        "provider_calls_today": providers,
        "tick_interval_seconds": config.TICK_INTERVAL_SECONDS,
    }
