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
# ---------------------------------------------------------------------------
# LIVE-EXECUTION STATE (single source of truth for the paper-side READERS).
#
# live_execution/ is a sibling package inside backend/ (§42), and its mutable
# state — kill switch, daily-loss breaker, idempotency ledger, commit log,
# operator break — lives in ONE directory. Paper-side readers (rule_engine/
# liveness.py for the break, api/routes/disclosure.py for the kill switch)
# must resolve that directory from here rather than composing their own
# paths: a stale anchor does not raise, it silently forks a SECOND state
# directory, so the bot and the operator can end up looking at different
# kill switches. Overridable per process so tests (and isolated drills) never
# touch the operator's live state.
# ---------------------------------------------------------------------------
LIVE_STATE_DIR: Path = Path(
    os.getenv("LIVE_STATE_DIR")
    or str(Path(__file__).parent / "live_execution" / "state")
)
BREAK_STATE_FILE: str = os.getenv(
    "BREAK_STATE_FILE", str(LIVE_STATE_DIR / "break_state.json")
)
KILL_SWITCH_FILE: str = os.getenv(
    "KILL_SWITCH_FILE", str(LIVE_STATE_DIR / "kill_switch.json")
)

BLOCKLIST_STATE_FILE: str = os.getenv(
    "BLOCKLIST_STATE_FILE", str(Path(__file__).parent / "blocklist_state.json")
)
# §49: the DONT-pattern killer is now PnL-based and BOTH books feed it.
# A mint whose newest AUTO_BLOCK_CONSECUTIVE_LOSSES recorded closes are all
# losses (realized PnL < 0, ANY exit rule — "sells for loss") is blocked
# until a human clears it. Hardcoded per the risk-number philosophy.
AUTO_BLOCK_CONSECUTIVE_LOSSES: int = 2
# Legacy alias for the pre-§49 name (rule-ID-based "stop-outs only").
AUTO_BLOCK_CONSECUTIVE_STOPS: int = AUTO_BLOCK_CONSECUTIVE_LOSSES
# §49 re-entry cooldown: a mint whose LAST recorded close is a loss younger
# than this many hours is filtered at read time on BOTH books. The 24h
# window IS the punishment for one loss; the block is for a pattern.
REENTRY_COOLDOWN_HOURS: float = 24.0
# Conviction ticket sizing + daily deploy cap.
SIZING_MODE: str = "fixed"                 # "fixed" | "conviction" | "risk_budget"
TICKET_CASH_FRACTION: float = 0.15         # base = cash * 0.15
TICKET_MAX_USD: float = 150.0              # hard per-trade ceiling
MIN_TICKET_USD: float = 25.0               # below this, don't bother
DAILY_DEPLOY_CAP_USD: float = 300.0        # max new deployments / UTC day

# ---------------------------------------------------------------------------
# REF-R8 risk-budget sizing (reference computeBudget() parity). HARDCODED -
# sizing arithmetic is safety-critical and never env-overridable (same
# philosophy as SLIPPAGE_PCT / FEE_PCT). Active only when SIZING_MODE is
# "risk_budget":
#   per order = equity * PER_ORDER_FRACTION * drawdown_factor,
#               clamped to [MIN_TICKET_USD, HARD_ORDER_CEILING_USD]
#   per day   = per order * DAY_MULTIPLE, clamped to HARD_DAILY_CEILING_USD
# The drawdown factor is the only adaptive term: a book under water on open
# risk trades smaller until that risk is off. Plain arithmetic, never model
# output - the model decides WHETHER to enter, never the size.
# ---------------------------------------------------------------------------
PER_ORDER_FRACTION: float = 0.035          # 3.5% of equity at full conviction
DAY_MULTIPLE: int = 4                      # a day may contain 4 full-size tickets
HARD_ORDER_CEILING_USD: float = 3000.0     # absolute stop per order
HARD_DAILY_CEILING_USD: float = 12000.0    # absolute stop per UTC day

# ---------------------------------------------------------------------------
# A11 thesis restatement (omo audit §30; reference thesis-author.server.ts).
# An open write-up that has not been advanced in THESIS_RESTATE_STALE_HOURS
# (or was never model-authored) is rewritten against the position's current
# numbers, at most THESIS_RESTATE_PER_PASS rows per pass, oldest text first,
# so a tick never turns into a batch job. HARDCODED cadence knobs — the job
# is narrative-only: it can only ever change thesis text, never size, exits,
# or verdicts, so it is not env-overridable (same philosophy as the sizing
# constants above).
# ---------------------------------------------------------------------------
THESIS_RESTATE_STALE_HOURS: float = 6.0    # reference STALE_MS = 6h
THESIS_RESTATE_PER_PASS: int = 2           # reference PER_PASS = 2

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
# Operator API auth (§38 security audit, finding F3).
# ADMIN_TOKEN gates the mutating operator endpoints (POST /api/admin/reset,
# POST /api/knowledge-base/ingest) via the X-Admin-Token header. FAIL CLOSED:
# an empty/unset token DISABLES those endpoints entirely (403) — a destructive
# endpoint must never be open without a credential, even on loopback.
# ---------------------------------------------------------------------------
ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "")

# §38 finding F4: hard cap on one ingested knowledge document (chars).
# Rejects oversized payloads before they touch disk, the DB, or prompt context.
MAX_INGEST_CHARS: int = int(os.getenv("MAX_INGEST_CHARS", "200000"))

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

# ---------------------------------------------------------------------------
# Main-provider selection (handoff §18): the MAIN LLM path (thinker,
# narrator, post-close reflections) is a reversible .env flip between
# "groq" and "deepseek". Groq stays the warm rollback path. The SOCIAL
# read is NOT affected — it stays on SOCIAL_LLM_* (Groq) regardless.
# Unrecognized values fail closed to groq (see build_main_client()).
# ---------------------------------------------------------------------------
MAIN_LLM_PROVIDER: str = os.getenv("MAIN_LLM_PROVIDER", "groq").strip().lower()

# ---------------------------------------------------------------------------
# Reference-style brain (2026-08-27). When True AND DATA_BACKEND=live, the tick runs
# a single reference-faithful reasoning call (role-routed, wallet-aware, rich
# verdicts/checks/watchlist) instead of one minimal per-candidate verdict.
# Fail-closed: any malformed/missing output degrades to the deterministic
# template pass. Mock mode ALWAYS uses the template thinker (hermetic tests).
# This changes ONLY the reasoning layer — the deterministic entry gate
# (verdict AND all rules) and PAPER_TRADING_ONLY are untouched.
# ---------------------------------------------------------------------------
LLM_BRAIN: bool = os.getenv("LLM_BRAIN", "1").strip().lower() in ("1", "true", "yes", "on")
# Output-token budget for the single brain tick call. It emits a full JSON tick
# (6-9 thoughts + one 5-7-check verdict per graded candidate + watchlist), which
# runs ~1.5-2k tokens for a small board; 4000 leaves headroom so the JSON is not
# truncated mid-object (a truncated body fails closed, so bigger is safer).
LLM_BRAIN_MAX_TOKENS: int = int(os.getenv("LLM_BRAIN_MAX_TOKENS", "4000"))
# The brain emits a much larger completion than the per-candidate thinker, so it
# needs a longer read timeout than DEEPSEEK_TIMEOUT_SECONDS (12s). On timeout the
# brain fails closed (template), never a bad buy.
LLM_BRAIN_TIMEOUT_SECONDS: float = float(os.getenv("LLM_BRAIN_TIMEOUT_SECONDS", "60"))

# ---------------------------------------------------------------------------
# DeepSeek direct API (main-provider candidate per docs/08 §1). Non-thinking
# mode only in the hot path. Model id + prices verified against
# api-docs.deepseek.com on 2026-08-27 (pricing snapshot id in llm/client.py).
#   deepseek-v4-flash (DeepSeek-V4-Flash-0731), 1M context, JSON output:
#     input  cache-miss: $0.22/1M off-peak, $0.44/1M peak
#     input  cache-hit : $0.007/1M off-peak, $0.014/1M peak
#     output          : $0.66/1M off-peak, $1.32/1M peak
#   Peak = 01:00-04:00 + 06:00-10:00 UTC Mon-Fri (llm/client._is_peak_window).
# ---------------------------------------------------------------------------
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_TIMEOUT_SECONDS: float = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "12"))
DEEPSEEK_MAX_TOKENS: int = int(os.getenv("DEEPSEEK_MAX_TOKENS", "192"))


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

# Exit conditions — now owned by the the reference bot-model exit engine below
# (the old +50% take-profit and 72h force-close were replaced by
# EXIT_TP_LADDER + EXIT_STALE_* in that block).
STOP_LOSS_PCT: float = 0.20      # close if unrealized loss >= -20%

# ---------------------------------------------------------------------------
# Exit engine (§5.2 rebuilt on the the reference bot model — PROCESS.md §5).
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
# Exit-price sanity guards (2026-08-28 cash-corruption incident, handoff §32).
# A transient bad quote once priced a $0.04 token at $119 (~2960x); the exit
# scanner ratcheted high-water on it and a take-profit trim credited ~$94k of
# phantom cash. These two hardcoded, fail-closed guards make that class of bug
# impossible. Both are deliberately generous so they ONLY ever trip on data
# errors, never on real market moves.
# ---------------------------------------------------------------------------
# A single exit-scan price this many multiples ABOVE the position's
# established peak is a bad quote, not a move: skip the position this scan and
# do NOT ratchet high-water. Upward-only on purpose — a genuine collapse must
# still be able to exit. Real prices do not 50x in one 15s scan; bad quotes do.
EXIT_PRICE_JUMP_MAX: float = 50.0
# Backstop: a single close/trim crediting more than this multiple of the
# position's cost basis is refused before any state write (the cash can never
# be corrupted even if a bad price reaches the exit math). 200x on one
# position is already a once-in-a-blue-moon gain; beyond it is a data bug.
MAX_EXIT_PROCEEDS_MULT: float = 200.0

# ---------------------------------------------------------------------------
# Rule engine thresholds (§2.3). All in USD / percent as labelled.
# ---------------------------------------------------------------------------
MIN_LIQUIDITY_USD: float = 15_000.0     # liquidity_floor (the reference bot parity)
MIN_VOLUME_1H_USD: float = 8_000.0      # volume_alive (the reference bot parity)
NEWBORN_AGE_HOURS: float = 24.0         # not_newborn_fade: joint condition —
NEWBORN_FADE_PCT: float = 15.0          #   young AND down >= this % in 1h fails it
# crowd_heat: 0-100 conviction index. the reference bot computes theirs from written
# theses on the FOMO board (heat = 20 + 8 x theses). Until a FOMO feed is
# wired (see docs/FOMO_INTEGRATION.md), we use the documented proxy:
# heat = CROWD_HEAT_BASE + CROWD_HEAT_PER_SIGNAL x named-presence signals.
# The act band is what gates: below MIN the crowd isn't there yet; above MAX
# it's already a hype peak (the reference refuses both extremes).
CROWD_HEAT_BASE: int = 20
CROWD_HEAT_PER_SIGNAL: int = 8
CROWD_HEAT_MIN: int = 36                # needs >= 2 of {twitter, telegram, site}
CROWD_HEAT_MAX: int = 100

# ---------------------------------------------------------------------------
# Crowd feeds (crowd_heat real sources — see data_providers/crowd.py and
# docs/FOMO_INTEGRATION.md). Both fail SOFT: an unavailable feed degrades
# crowd_heat to the presence proxy; it never blocks anything else.
# ---------------------------------------------------------------------------
# Option 1 — fomo.fun board (the reference bot' exact source). Requires the operator's
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
# Item #3 (omo parity — exit-liquidity-dump discount): a thesis whose author
# ALREADY closed their position at a profit (closedAt set AND realized_usd>0)
# is KNOWN-dumped. The board total still counts it, but crowd heat counts it
# at this fraction (0.0 = a dumped thesis is not live conviction; 1.0 = old
# behavior). Only applies to the rows we actually SAW — the unseen remainder
# of the board total keeps full credit (fail-soft, same as omo's brain-level
# discount). Applied at the count the heat formula consumes, NOT to the raw
# `total` returned to the thinker/tests (which stays the board's own number).
FOMO_DUMPED_THESIS_WEIGHT: float = float(
    os.getenv("FOMO_DUMPED_THESIS_WEIGHT", "0.0"))
# A4 (omo audit §28): the bot's OWN fomo.family handle. When set, the live
# cycle reads the bot's own position accounting back from the thesis feed
# (authorTrade) and cross-checks it against the journal's cost basis —
# observability only, the journal stays the money authority. Empty = disabled.
FOMO_OWN_HANDLE: str = os.getenv("FOMO_OWN_HANDLE", "")
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
# ---------------------------------------------------------------------------
# §47 — LOCAL stealth transport (Scrapling, BSD-3). Phase-0 drill verified
# 2026-08-30: curl-cffi Chrome-TLS impersonation passes fomo's Cloudflare
# 100% with no browser (the block is TLS-fingerprint, not IP); the patchright
# stealth browser is the second free hop for the harder days. SCRAPLING_*
# knobs below are transport-tuning only — SCRAPLING_ENABLED=0 falls back to
# the paid chain with zero code changes (reversible one .env line).
# ---------------------------------------------------------------------------
SCRAPLING_ENABLED: bool = os.getenv("SCRAPLING_ENABLED", "1") == "1"
# Browser-hop budget, ms (Scrapling takes ms; curl hop uses _TIMEOUT).
SCRAPLING_TIMEOUT_MS: int = int(os.getenv("SCRAPLING_TIMEOUT_MS", "30000"))
# Close the warm browser after this many idle seconds (RAM hygiene).
SCRAPLING_IDLE_CLOSE_SECONDS: float = float(
    os.getenv("SCRAPLING_IDLE_CLOSE_SECONDS", "600"))
# Only if a deployment IP is hard-blocked: pass a proxy to the browser hop
# (user:pass@host:port). Empty = never used. The curl hop needs no proxy.
SCRAPLING_PROXY: str = os.getenv("SCRAPLING_PROXY", "")

# Additional stealth-scrape providers — OPTIONAL failover chain. When the
# preferred provider runs out of credits (or errors), the next CONFIGURED
# one takes over automatically; an exhausted provider is benched so we stop
# burning calls on it. All are GET-return-body services (simplest contract).
SCRAPINGBEE_API_KEY: str = os.getenv("SCRAPINGBEE_API_KEY", "")
SCRAPINGDOG_API_KEY: str = os.getenv("SCRAPINGDOG_API_KEY", "")
ZENROWS_API_KEY: str = os.getenv("ZENROWS_API_KEY", "")
SCRAPEOPS_API_KEY: str = os.getenv("SCRAPEOPS_API_KEY", "")
STEALTH_BENCH_SECONDS: float = float(os.getenv("STEALTH_BENCH_SECONDS", "1800"))
# A 429 is a transient RATE LIMIT (clears in ~a minute), not credit exhaustion,
# so it gets a short backoff instead of the long bench — benching a healthy
# provider for 30 min over a throttle is what sidelined Firecrawl (2026-08-27).
STEALTH_THROTTLE_BACKOFF_SECONDS: float = float(
    os.getenv("STEALTH_THROTTLE_BACKOFF_SECONDS", "75"))

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
# Split deployments (dashboard on Vercel/CF Pages, API elsewhere) may need
# more than one allowed origin: FRONTEND_ORIGIN accepts a comma-separated
# list (e.g. "https://bot.vercel.app,http://localhost:5173"). Empty entries
# are dropped; the local dev origin remains the fail-closed default.
FRONTEND_ORIGINS: list = [o.strip() for o in FRONTEND_ORIGIN.split(",") if o.strip()]
WS_POLL_INTERVAL_SECONDS: float = 2.0
# Live book public access (SEC-02): when False (default), live portfolio and
# executions endpoints require the X-Admin-Token operator header or loopback.
LIVE_BOOK_PUBLIC: bool = os.getenv("LIVE_BOOK_PUBLIC", "false").strip().lower() == "true"
# HTTPS enforcement: when True, non-loopback HTTP requests are redirected to HTTPS.
FORCE_HTTPS: bool = os.getenv("FORCE_HTTPS", "false").strip().lower() == "true"




def assert_paper_trading_only() -> None:
    """
    §52: RETIRED with the paper book — no caller remains (the paper engine
    that asserted it at runtime was deleted). Kept only because external
    scripts may import it; live trading NEVER calls this: live arming is
    LIVE_TRADING_ENABLED in live_execution/config.py, human-edit-only.
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
# Second-pass deep-research budget per tick (the reference researches the names it
# actually cares about - one extra Dexscreener call each, capped).
RESEARCH_PER_TICK: int = int(os.getenv("RESEARCH_PER_TICK", "8"))
# Realtime social read (the reference realtime role) - ANY OpenAI-compatible provider.
# Groq today, Grok tomorrow: switching is ONLY these three env values.
#   groq: https://api.groq.com/openai/v1 + llama-3.3-70b-versatile
#   xai:  https://api.x.ai/v1           + grok-3-mini (or newer)
#   openrouter: https://openrouter.ai/api/v1 + vendor/model
# Empty SOCIAL_LLM_API_KEY = stage disabled (fail-soft, like every feed).
SOCIAL_LLM_BASE_URL: str = os.getenv("SOCIAL_LLM_BASE_URL", "https://api.groq.com/openai/v1")
SOCIAL_LLM_API_KEY: str = os.getenv("SOCIAL_LLM_API_KEY", "")
SOCIAL_LLM_MODEL: str = os.getenv("SOCIAL_LLM_MODEL", "qwen/qwen3.8-27b")
SOCIAL_LLM_TIMEOUT_SECONDS: float = float(os.getenv("SOCIAL_LLM_TIMEOUT_SECONDS", "20"))
SOCIAL_READ_PER_TICK: int = int(os.getenv("SOCIAL_READ_PER_TICK", "8"))

# Live web search evidence for the think stage (the reference web-research
# parity). §51: FREE-first chain — Brave (primary), self-hosted SearXNG
# (keyless secondary), Firecrawl (last-resort failover). Empty everywhere =
# stage disabled. tbs=qdr:d / freshness=pd / time_range=day all limit results
# to the last 24h like the reference recent=true.
WEB_SEARCH_PER_TICK: int = int(os.getenv("WEB_SEARCH_PER_TICK", "8"))
WEB_RESEARCH_TIMEOUT_SECONDS: float = float(os.getenv("WEB_RESEARCH_TIMEOUT_SECONDS", "20"))
# §48: cross-tick evidence cache (in-memory, per mint). A HIT (real
# evidence lines) is cached this long — default 2h, well inside the search's
# own 24h relevance window. A MISS (no results) is cached only
# WEB_SEARCH_CACHE_MISS_TTL so "nothing found" stays reasonably current:
# a fresh memecoin's attention often starts BETWEEN searches.
WEB_SEARCH_CACHE_TTL: float = float(os.getenv("WEB_SEARCH_CACHE_TTL", "7200"))
WEB_SEARCH_CACHE_MISS_TTL: float = float(
    os.getenv("WEB_SEARCH_CACHE_MISS_TTL", "1800"))

# ---------------------------------------------------------------------------
# §51 — FREE web-search transport chain (Brave → SearXNG → Firecrawl).
#
# The crowd feed went free in §47 (Scrapling TLS impersonation); the web-search
# evidence stage is the LAST metered social input. This chain replaces it with
# free transports in preference order, behind the same §48 staging + cache:
#   1. Brave Search API   — $5 of free credits EVERY month (auto-applied), at
#      $5/1k queries ≈ 1,000 searches/month free, 1 req/s free tier. Freshness
#      "pd" = past-day (parity with the old tbs=qdr:d 24h window).
#   2. Self-hosted SearXNG — keyless, unlimited: our own docker sidecar,
#      format=json&time_range=day. Public instances DISABLE the json format
#      (403) — only a self-hosted instance works; see deploy/searxng/.
#   3. Firecrawl /v1/search — the paid incumbent, kept ONLY as last-resort
#      failover (402 exhaustion benches it until refill, as today).
# Every hop maps results onto the SAME {title, description} row shape the old
# Firecrawl path produced, so summarize_hits() output stays byte-identical —
# the thinker prompt sees no change. Benching mirrors crowd.py §34: 402/422 →
# long bench, 429 → short backoff, 2 consecutive transport errors → bench.
# ---------------------------------------------------------------------------
# Brave: free key from https://api-dashboard.search.brave.com (free plan —
# the $5 monthly credit is applied automatically, no card on the free tier).
BRAVE_SEARCH_API_KEY: str = os.getenv("BRAVE_SEARCH_API_KEY", "")
BRAVE_SEARCH_URL: str = os.getenv(
    "BRAVE_SEARCH_URL", "https://api.search.brave.com/res/v1/web/search")
# Self-hosted SearXNG (deploy/searxng/ + docker-compose.yml service "searxng").
# MUST be an instance WE run (json format enabled, limiter off) — a public
# instance returns 403 on format=json. Empty = hop off.
SEARXNG_URL: str = os.getenv("SEARXNG_URL", "")
# §34-style benching for the search chain (seconds).
SEARCH_BENCH_SECONDS: float = float(os.getenv("SEARCH_BENCH_SECONDS", "1800"))
SEARCH_THROTTLE_BACKOFF_SECONDS: float = float(
    os.getenv("SEARCH_THROTTLE_BACKOFF_SECONDS", "75"))

# ---------------------------------------------------------------------------
# DB maintenance — row retention limits for append-only tables.
# Prune functions keep the newest KEEP rows and delete everything older.
# Adjust via env if you want to keep more/less history before resetting.
# ---------------------------------------------------------------------------
FEED_PRUNE_KEEP: int = int(os.getenv("FEED_PRUNE_KEEP", "2000"))
REGIME_PRUNE_KEEP: int = int(os.getenv("REGIME_PRUNE_KEEP", "500"))
