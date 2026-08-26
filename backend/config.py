"""
config.py — Central configuration for trading-bot.

Safety-critical constants are HARDCODED here, never environment-variable-
configurable, so they cannot be overridden via .env or environment injection:
  - PAPER_TRADING_ONLY  (the authoritative paper-trading safety gate)
  - SLIPPAGE_PCT / FEE_PCT  (part of the simulated execution model; changing
    them mid-session would corrupt the track record)

Operator-facing settings (API keys, model URL, backend selection) load from
.env via python-dotenv.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Blocklist — manual + auto (see blocklist.py). Auto-blocks after N
# consecutive stop-outs on the same mint (the DONT pattern killer).
# ---------------------------------------------------------------------------
BLOCKLIST_STATE_FILE: str = os.getenv(
    "BLOCKLIST_STATE_FILE", str(Path(__file__).parent / "blocklist_state.json")
)
AUTO_BLOCK_CONSECUTIVE_STOPS: int = 2
# Conviction ticket sizing + daily deploy cap.
SIZING_MODE: str = "fixed"                 # "fixed" | "conviction"
TICKET_CASH_FRACTION: float = 0.15         # base = cash * 0.15
TICKET_MAX_USD: float = 150.0              # hard per-trade ceiling
MIN_TICKET_USD: float = 25.0               # below this, don't bother
DAILY_DEPLOY_CAP_USD: float = 300.0        # max new deployments / UTC day

# ---------------------------------------------------------------------------
# SAFETY FLAG — HARDCODED, NEVER READ FROM ENV, NEVER CHANGED BY CODE.
# Edited only by a human, manually, in this file. Every position-opening
# function asserts this at runtime as well (belt-and-suspenders, E7).
# ---------------------------------------------------------------------------
PAPER_TRADING_ONLY: bool = True

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).parent
DB_PATH: Path = BASE_DIR / os.getenv("DB_PATH", "trading_bot.db")

# ---------------------------------------------------------------------------
# Supabase (Postgres) — optional remote DB backend. Empty USE_SUPABASE_DB
# keeps the local SQLite book. The service-role key bypasses RLS; it must
# live only in .env on the server, never in the repo or frontend.
# ---------------------------------------------------------------------------
USE_SUPABASE_DB: bool = os.getenv("USE_SUPABASE_DB", "") == "1"
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_ANON_KEY: str = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_DB_URL: str = os.getenv("SUPABASE_DB_URL", "")
KNOWLEDGE_BASE_DIR: Path = BASE_DIR / "knowledge_base"
STATIC_KNOWLEDGE_FILE: Path = KNOWLEDGE_BASE_DIR / "static_knowledge.md"
INGESTED_KNOWLEDGE_DIR: Path = KNOWLEDGE_BASE_DIR / "ingested"

# ---------------------------------------------------------------------------
# LLM providers
# Qwen3-8B: empirically ~23.6 tok/s on the target 6GB VRAM GPU with 100%
# valid structured output. The LLM is the pipeline bottleneck; nothing here
# may add avoidable serial I/O on top of it.
# ---------------------------------------------------------------------------
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GROQ_BASE_URL: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
GROQ_TIMEOUT_SECONDS: float = float(os.getenv("GROQ_TIMEOUT_SECONDS", "12"))
GROQ_MAX_TOKENS: int = int(os.getenv("GROQ_MAX_TOKENS", "192"))

# Legacy local provider settings remain available for explicit offline use.
OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL_NAME: str = os.getenv("MODEL_NAME", "qwen3:8b")
OLLAMA_TIMEOUT_SECONDS: float = 120.0
OLLAMA_GENERATE_ENDPOINT: str = f"{OLLAMA_URL}/api/generate"
OLLAMA_TAGS_ENDPOINT: str = f"{OLLAMA_URL}/api/tags"
# Ollama context window for /api/generate calls (num_ctx). KV-cache RAM scales
# with this value, NOT with prompt length; Ollama's default (4096) is ~4x
# larger than needed here: a narration prompt is fixed instruction text
# (~200 tokens) plus ten rule lines (~20 tokens each) — analytically <700
# tokens worst case. 1024 leaves ~1.5x headroom over that bound while
# cutting narrator KV memory roughly 4x vs the default. VERIFY against
# response.prompt_eval_count on first live narration; raise to 2048 only if
# the measured count exceeds ~700.
OLLAMA_NUM_CTX: int = int(os.getenv("OLLAMA_NUM_CTX", "1024"))
# Output token ceiling (num_predict). Ollama truncates long structured output
# at its own default; raise via env without touching code. -1 = unlimited.
OLLAMA_NUM_PREDICT: int = int(os.getenv("OLLAMA_NUM_PREDICT", "512"))
# In mock data mode the narrator uses a deterministic template backend so the
# full pipeline runs without Ollama; live mode uses Ollama when reachable.
NARRATOR_FALLBACK_TO_TEMPLATE: bool = True

# ---------------------------------------------------------------------------
# Data providers (A9): single selection point for the whole app
#   "mock" -> data_providers.mock.MockProvider
#   "live" -> combined Birdeye + Dexscreener + Jupiter stack
# Swapping providers touches no other module.
# ---------------------------------------------------------------------------
DATA_BACKEND: str = os.getenv("DATA_BACKEND", "mock").strip().lower()
# --- Provider API keys ------------------------------------------------------
# Birdeye requires a key even on the free tier (sent as X-API-KEY).
BIRDEYE_API_KEY: str = os.getenv("BIRDEYE_API_KEY", "")
# Dexscreener's basic pair/search endpoints are currently keyless; this field
# exists so a future paid tier needs zero code changes (sent as Authorization
# bearer when set).
DEXSCREENER_API_KEY: str = os.getenv("DEXSCREENER_API_KEY", "")
# Jupiter quote API v6 is currently keyless; a Jupiter Pro key is sent as
# x-api-key when set.
JUPITER_API_KEY: str = os.getenv("JUPITER_API_KEY", "")

BIRDEYE_BASE_URL: str = "https://public-api.birdeye.so"
DEXSCREENER_BASE_URL: str = "https://api.dexscreener.com"

# Free on-chain RPC for authority/security reads (no Birdeye needed).
ONCHAIN_RPC_URLS: list = [
    u for u in (
        os.getenv("SOLANA_RPC_URL", ""),
        "https://api.mainnet-beta.solana.com",
        "https://solana-rpc.publicnode.com",
    ) if u
]
JUPITER_QUOTE_URL: str = "https://lite-api.jup.ag/swap/v1/quote"
USDC_MINT: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT: str = "So11111111111111111111111111111111111111112"

# HTTP client behavior for external APIs (defense-first rule 8): every call
# has a defined timeout and bounded retry with backoff; 429 gets its own,
# longer backoff and a distinct log event + counter.
EXTERNAL_API_TIMEOUT_SECONDS: float = 15.0

# ---------------------------------------------------------------------------
# Paper trading parameters (simulated execution model)
# ---------------------------------------------------------------------------
INITIAL_CASH_USD: float = float(os.getenv("INITIAL_CASH_USD", "1000.0"))
INTENDED_POSITION_SIZE_USD: float = 100.0   # fixed per-entry size the cash_available rule checks against

# These are NOT env-configurable — part of the simulated execution model.
SLIPPAGE_PCT: float = 0.02    # 2% simulated entry/exit slippage
FEE_PCT: float = 0.01         # 1% simulated DEX fee each way

# Exit conditions — now owned by the omotrades-model exit engine below
# (the old +50% take-profit and 72h force-close were replaced by
# EXIT_TP_LADDER + EXIT_STALE_* in that block).
STOP_LOSS_PCT: float = 0.20      # close if unrealized loss >= -20%

# ---------------------------------------------------------------------------
# Exit engine (§5.2 rebuilt on the omotrades model — PROCESS.md §5).
# Risk-off rules close FULLY and outrank profit taking; only take-profit
# tranches are partial. All values hardcoded (non-env-configurable).
# ---------------------------------------------------------------------------
# Trailing give-back: once the position has been up TRAIL_ACTIVATION_PCT or
# better, close fully when it has given back TRAIL_GIVE_BACK_PP percentage
# points from its high-water mark ("up 50%+ then give back 40 points").
EXIT_TRAIL_ACTIVATION_PCT: float = 0.50
EXIT_TRAIL_GIVE_BACK_PP: float = 40.0
# Liquidity break: pool can no longer return the size cleanly -> full exit
# regardless of P&L. Evaluated only when liquidity data is available.
EXIT_LIQUIDITY_FLOOR_USD: float = 8_000.0
# Thesis invalidated: deep multi-hour dump WITH sellers leading decisively.
EXIT_INVALIDATION_CHG6H_PCT: float = -25.0
EXIT_INVALIDATION_SELL_MULT: float = 1.4
# Stale thesis: held this long, going nowhere, tape drying up -> close.
EXIT_STALE_DAYS: float = 14.0
EXIT_STALE_BAND_PCT: float = 0.10
EXIT_STALE_VOL6H_USD: float = 5_000.0
# Take-profit ladder: (net gain fraction, fraction of remaining position).
# Risk-off always beats these; what survives the ladder rides the trail.
EXIT_TP_LADDER: tuple[tuple[float, float], ...] = (
    (1.00, 0.33),   # +100% -> trim 33% of remaining
    (3.00, 0.33),   # +300% -> trim 33% of remaining
    (9.00, 0.50),   # +900% -> trim half of what's left
)
# Sell risk gate (narrow on purpose — a refused sell leaves risk on).
SELL_MIN_CLIP_USD: float = 25.0        # trims smaller than this are skipped
SELL_COOLDOWN_MINUTES: float = 30.0    # per mint between gated sells
MAX_EXITS_PER_24H: int = 8             # rolling window, gated sells only
# Dedicated fast exit scanner: memecoins gap through stops between 60s ticks,
# so risk checks run on their own cheap loop (price-only HTTP, zero LLM).
EXIT_SCAN_INTERVAL_SECONDS: float = 15.0

# ---------------------------------------------------------------------------
# Rule engine thresholds (§2.3). All in USD / percent as labelled.
# ---------------------------------------------------------------------------
MIN_LIQUIDITY_USD: float = 15_000.0     # liquidity_floor (omotrades parity)
MIN_VOLUME_1H_USD: float = 8_000.0      # volume_alive (omotrades parity)
NEWBORN_AGE_HOURS: float = 24.0         # not_newborn_fade: joint condition —
NEWBORN_FADE_PCT: float = 15.0          #   young AND down >= this % in 1h fails it
# crowd_heat: 0-100 conviction index. omotrades computes theirs from written
# theses on the FOMO board (heat = 20 + 8 x theses). Until a FOMO feed is
# wired (see docs/FOMO_INTEGRATION.md), we use the documented proxy:
# heat = CROWD_HEAT_BASE + CROWD_HEAT_PER_SIGNAL x named-presence signals.
# The act band is what gates: below MIN the crowd isn't there yet; above MAX
# it's already a hype peak (omo refuses both extremes).
CROWD_HEAT_BASE: int = 20
CROWD_HEAT_PER_SIGNAL: int = 8
CROWD_HEAT_MIN: int = 36                # needs >= 2 of {twitter, telegram, site}
CROWD_HEAT_MAX: int = 100

# ---------------------------------------------------------------------------
# Crowd feeds (crowd_heat real sources — see data_providers/crowd.py and
# docs/FOMO_INTEGRATION.md). Both fail SOFT: an unavailable feed degrades
# crowd_heat to the presence proxy; it never blocks anything else.
# ---------------------------------------------------------------------------
# Option 1 — fomo.fun board (omotrades' exact source). Requires the operator's
# Privy refresh token, extracted ONCE from a logged-in fomo.family browser
# session (DevTools -> Application -> Local Storage -> privy-token / refresh
# token). Exchanged for a ~1h access token automatically.
FOMO_PRIVY_REFRESH_TOKEN: str = os.getenv("FOMO_PRIVY_REFRESH_TOKEN", "")
# Gitignored sidecar where the bot persists Privy's ROTATED refresh token
# after each session mint — makes the auth chain self-sustaining across
# restarts. The .env value acts only as the one-time bootstrap.
FOMO_PRIVY_STATE_FILE: str = os.getenv(
    "FOMO_PRIVY_STATE_FILE",
    str(Path(__file__).parent.parent / ".fomo_privy.json"),
)
FOMO_API_BASE: str = "https://prod-api.fomo.family"
PRIVY_SESSIONS_URL: str = "https://auth.privy.io/api/v1/sessions"
PRIVY_APP_ID: str = "cm6h485o300n3zj9yl6vpedq7"     # fomo.family's public app id
FOMO_NETWORK_ID: int = 1399811149                   # solana mainnet
FOMO_THESIS_LIMIT: int = 40
FOMO_CACHE_TTL_SECONDS: float = 60.0
# (pump.fun comments were evaluated as a secondary source and DEFERRED —
# their API host is dead/503s even via stealth proxy; docs/FOMO_INTEGRATION.md)

# Firecrawl stealth proxy — required fallback for the fomo board because
# prod-api.fomo.family 403-challenges direct reads (verified live).
# NOTE: the API lives at firecrawl.DEV now; the old .app host is TLS-dead
# (verified 2026-08-23).
FIRECRAWL_API_KEY: str = os.getenv("FIRECRAWL_API_KEY", "")
FIRECRAWL_SCRAPE_URL: str = os.getenv(
    "FIRECRAWL_SCRAPE_URL", "https://api.firecrawl.dev/v1/scrape"
)
# Additional stealth-scrape providers — OPTIONAL failover chain. When the
# preferred provider runs out of credits (or errors), the next CONFIGURED
# one takes over automatically; an exhausted provider is benched so we stop
# burning calls on it. All are GET-return-body services (simplest contract).
SCRAPINGBEE_API_KEY: str = os.getenv("SCRAPINGBEE_API_KEY", "")
SCRAPINGDOG_API_KEY: str = os.getenv("SCRAPINGDOG_API_KEY", "")
ZENROWS_API_KEY: str = os.getenv("ZENROWS_API_KEY", "")
SCRAPEOPS_API_KEY: str = os.getenv("SCRAPEOPS_API_KEY", "")
STEALTH_BENCH_SECONDS: float = float(os.getenv("STEALTH_BENCH_SECONDS", "1800"))

# ---------------------------------------------------------------------------
# Market regime thresholds (§3.3) — EXPLICIT PLACEHOLDERS needing calibration.
# To be tuned from real paper-trading data during the 10-day window
# (03_GANTT_CHART.md calibration section). Do not treat these as validated.
# Intuition per spec: reject regime if an unusually high fraction of the whole
# candidate universe is simultaneously green (broad-pump smell) or if median
# volume across the universe is suspiciously thin (dead tape).
# ---------------------------------------------------------------------------
REGIME_MIN_PCT_GREEN: float = 0.15
REGIME_MAX_PCT_GREEN: float = 0.85
REGIME_MIN_MEDIAN_VOLUME_USD: float = 20_000.0

# ---------------------------------------------------------------------------
# Knowledge base prompt budget (performance-discipline rule 8)
# ---------------------------------------------------------------------------
KB_MAX_CONTEXT_CHARS: int = 5_000

# ---------------------------------------------------------------------------
# Promotion gate thresholds — read-only assessment, never auto-activates anything
# ---------------------------------------------------------------------------
PROMOTION_MIN_TRADES: int = 40
LEARNING_WINDOW_DAYS: int = 10
PROMOTION_MIN_WIN_RATE: float = 0.55
PROMOTION_MIN_PROFIT_FACTOR: float = 1.5
PROMOTION_MAX_DRAWDOWN_PCT: float = 20.0

# ---------------------------------------------------------------------------
# API server
# ---------------------------------------------------------------------------
API_HOST: str = os.getenv("API_HOST", "127.0.0.1")
API_PORT: int = int(os.getenv("API_PORT", "8000"))
FRONTEND_ORIGIN: str = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")
WS_POLL_INTERVAL_SECONDS: float = 2.0


def assert_paper_trading_only() -> None:
    """
    Runtime assertion called INSIDE every position-opening/state-changing
    trading function (E7). Belt-and-suspenders: even if a caller upstream
    forgot to check, the engine itself refuses to act unless the hardcoded
    safety flag is True.
    """
    if PAPER_TRADING_ONLY is not True:
        raise RuntimeError(
            "PAPER_TRADING_ONLY is not True — refusing to touch trade state. "
            "This flag is hardcoded in config.py and must only be changed by "
            "a human, manually."
        )

EXTERNAL_API_MAX_RETRIES: int = 3
EXTERNAL_API_RETRY_BACKOFF_SECONDS: float = 2.0
RATE_LIMIT_EXTRA_BACKOFF_SECONDS: float = 15.0

# ---------------------------------------------------------------------------
# Tick loop
# ---------------------------------------------------------------------------
TICK_INTERVAL_SECONDS: int = int(os.getenv("TICK_INTERVAL_SECONDS", "60"))
MAX_CANDIDATES_PER_TICK: int = int(os.getenv("MAX_CANDIDATES_PER_TICK", "20"))
# Second-pass deep-research budget per tick (omo researches the names it
# actually cares about - one extra Dexscreener call each, capped).
RESEARCH_PER_TICK: int = int(os.getenv("RESEARCH_PER_TICK", "8"))
# Realtime social read (omo realtime role) - ANY OpenAI-compatible provider.
# Groq today, Grok tomorrow: switching is ONLY these three env values.
#   groq: https://api.groq.com/openai/v1 + llama-3.3-70b-versatile
#   xai:  https://api.x.ai/v1           + grok-3-mini (or newer)
#   openrouter: https://openrouter.ai/api/v1 + vendor/model
# Empty SOCIAL_LLM_API_KEY = stage disabled (fail-soft, like every feed).
SOCIAL_LLM_BASE_URL: str = os.getenv("SOCIAL_LLM_BASE_URL", "https://api.groq.com/openai/v1")
SOCIAL_LLM_API_KEY: str = os.getenv("SOCIAL_LLM_API_KEY", "")
SOCIAL_LLM_MODEL: str = os.getenv("SOCIAL_LLM_MODEL", "llama-3.3-70b-versatile")
SOCIAL_LLM_TIMEOUT_SECONDS: float = float(os.getenv("SOCIAL_LLM_TIMEOUT_SECONDS", "20"))
SOCIAL_READ_PER_TICK: int = int(os.getenv("SOCIAL_READ_PER_TICK", "8"))

# Live web search evidence for the think stage (omo web-research parity).
# Uses the existing FIRECRAWL_API_KEY; empty key = stage disabled.
# tbs=qdr:d limits results to the last 24h like omo recent=true.
WEB_SEARCH_PER_TICK: int = int(os.getenv("WEB_SEARCH_PER_TICK", "8"))
WEB_RESEARCH_TIMEOUT_SECONDS: float = float(os.getenv("WEB_RESEARCH_TIMEOUT_SECONDS", "20"))
