"""
config.py — Central configuration for trading-bot.

Risk-critical constants (PAPER_TRADING_ONLY, SLIPPAGE_PCT, FEE_PCT) are
hardcoded here, not environment-variable-configurable, so they cannot be
accidentally overridden via a .env file or environment injection.

All operator-facing settings (API keys, model URL, backend selection) are
loaded from .env via python-dotenv.

Per defense-first rule: config values that feed into money math or trade
state are explicit constants, not .get()-with-defaults on untrusted input.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# SAFETY FLAG — HARDCODED, NEVER READ FROM ENV OR CHANGED BY CODE
# This is the authoritative paper-trading safety gate. Do not move it to an
# environment variable. It must be edited in this file, by a human, manually.
# ---------------------------------------------------------------------------
PAPER_TRADING_ONLY: bool = True

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).parent
DB_PATH: Path = BASE_DIR / os.getenv("DB_PATH", "trading_bot.db")
KNOWLEDGE_BASE_DIR: Path = BASE_DIR / "knowledge_base"
STATIC_KNOWLEDGE_FILE: Path = KNOWLEDGE_BASE_DIR / "static_knowledge.md"
INGESTED_KNOWLEDGE_DIR: Path = KNOWLEDGE_BASE_DIR / "ingested"

# ---------------------------------------------------------------------------
# Ollama / LLM — local only, no cloud fallback
# The model and URL encode empirical hardware benchmarking results:
# Qwen3-8B at ~23.6 tok/s on the target 6GB VRAM GPU with 100% valid JSON.
# Do not swap these defaults to a hosted model.
# ---------------------------------------------------------------------------
OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL_NAME: str = os.getenv("MODEL_NAME", "qwen3:8b")
OLLAMA_TIMEOUT_SECONDS: float = 120.0
OLLAMA_GENERATE_ENDPOINT: str = f"{OLLAMA_URL}/api/generate"
OLLAMA_TAGS_ENDPOINT: str = f"{OLLAMA_URL}/api/tags"

# ---------------------------------------------------------------------------
# Data ingestion backend
# ---------------------------------------------------------------------------
DATA_BACKEND: str = os.getenv("DATA_BACKEND", "mock")  # mock | birdeye | coinstats
BIRDEYE_API_KEY: str = os.getenv("BIRDEYE_API_KEY", "")
COINSTATS_API_KEY: str = os.getenv("COINSTATS_API_KEY", "")
JUPITER_API_KEY: str = os.getenv("JUPITER_API_KEY", "")
BIRDEYE_BASE_URL: str = "https://public-api.birdeye.so"
JUPITER_QUOTE_URL: str = "https://quote-api.jup.ag/v6/quote"

# HTTP client timeouts for external APIs (defense-first rule 8)
EXTERNAL_API_TIMEOUT_SECONDS: float = 15.0
EXTERNAL_API_MAX_RETRIES: int = 3
EXTERNAL_API_RETRY_BACKOFF_SECONDS: float = 2.0

# ---------------------------------------------------------------------------
# Tick loop
# ---------------------------------------------------------------------------
TICK_INTERVAL_SECONDS: int = int(os.getenv("TICK_INTERVAL_SECONDS", "60"))
MAX_CANDIDATES_PER_TICK: int = int(os.getenv("MAX_CANDIDATES_PER_TICK", "20"))

# ---------------------------------------------------------------------------
# Paper trading parameters
# ---------------------------------------------------------------------------
INITIAL_CASH_USD: float = float(os.getenv("INITIAL_CASH_USD", "1000.0"))
POSITION_SIZE_PCT: float = float(os.getenv("POSITION_SIZE_PCT", "0.10"))
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "5"))

# These are NOT env-configurable — they are part of the simulated execution
# model and changing them mid-session would corrupt the track record.
SLIPPAGE_PCT: float = 0.02   # 2% simulated entry/exit slippage
FEE_PCT: float = 0.01         # 1% simulated DEX fee each way

# Exit thresholds
TAKE_PROFIT_PCT: float = 0.50   # close if unrealized gain >= +50%
STOP_LOSS_PCT: float = 0.20     # close if unrealized loss >= -20%
MAX_HOLD_HOURS: int = 72         # force-close after 72 hours

# ---------------------------------------------------------------------------
# Deterministic pre-filter thresholds
# All in USD or percentages as labelled.
# ---------------------------------------------------------------------------
MIN_LIQUIDITY_USD: float = 10_000.0
MAX_TOP_HOLDER_PCT: float = 20.0    # top single holder must own < this %
MIN_HOLDER_COUNT: int = 200
MIN_AGE_HOURS: int = 1              # must be at least 1h old
MAX_AGE_HOURS: int = 168            # must be less than 7 days old
MIN_VOLUME_24H_USD: float = 5_000.0
MIN_MARKET_CAP_USD: float = 50_000.0

# ---------------------------------------------------------------------------
# Knowledge base prompt budget (performance-discipline rule 8)
# ---------------------------------------------------------------------------
KB_MAX_CONTEXT_CHARS: int = 2_000   # hard cap on context injected per prompt

# ---------------------------------------------------------------------------
# Promotion gate thresholds (read-only assessment — never auto-activates anything)
# ---------------------------------------------------------------------------
PROMOTION_MIN_TRADES: int = 40
PROMOTION_MIN_WIN_RATE: float = 0.55
PROMOTION_MIN_PROFIT_FACTOR: float = 1.5
PROMOTION_MAX_DRAWDOWN_PCT: float = 20.0
LEARNING_WINDOW_DAYS: int = 10

# ---------------------------------------------------------------------------
# API server
# ---------------------------------------------------------------------------
API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
API_PORT: int = int(os.getenv("API_PORT", "8000"))
FRONTEND_ORIGIN: str = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")
# How often the API's WS broadcaster polls SQLite for new feed events (seconds)
WS_POLL_INTERVAL_SECONDS: float = 2.0
