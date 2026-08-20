"""
api/routes/system_status.py — GET /api/system-status

Ollama health check, model status, and active data backend.
FR-30: Surfaces LLM connection issues so the frontend shows "LLM disconnected"
rather than silently failing.
"""
from __future__ import annotations

import config
import llm_scorer
from fastapi import APIRouter

router = APIRouter()


@router.get("/system-status")
async def get_system_status():
    """
    FR-30: System health — Ollama reachability, model status, active backend.

    Also surfaces FR-5a: which data backend is active, so it's always obvious
    whether the feed is running on synthetic or real data.
    """
    ollama_status = await llm_scorer.check_ollama_health()

    return {
        "paper_trading_only": config.PAPER_TRADING_ONLY,
        "data_backend": config.DATA_BACKEND,
        "ollama": ollama_status,
        "config": {
            "model_name": config.MODEL_NAME,
            "ollama_url": config.OLLAMA_URL,
            "tick_interval_seconds": config.TICK_INTERVAL_SECONDS,
            "max_open_positions": config.MAX_OPEN_POSITIONS,
            "initial_cash_usd": config.INITIAL_CASH_USD,
            "position_size_pct": config.POSITION_SIZE_PCT,
            "take_profit_pct": config.TAKE_PROFIT_PCT,
            "stop_loss_pct": config.STOP_LOSS_PCT,
            "max_hold_hours": config.MAX_HOLD_HOURS,
            "learning_window_days": config.LEARNING_WINDOW_DAYS,
            "promotion_min_trades": config.PROMOTION_MIN_TRADES,
        },
    }
