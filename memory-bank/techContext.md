# Tech Context — trading-bot

## Stack
- Python 3.14 (system) + `.venv/` at repo root; deps in backend/requirements.txt
  (fastapi, uvicorn[standard], httpx, aiosqlite, asyncpg, python-dotenv,
  solders (Solana signing — live_execution), pytest, pytest-asyncio)
- DB: SQLite via aiosqlite (WAL; backend/trading_bot.db) — DEFAULT — OR
  Supabase Postgres via asyncpg (USE_SUPABASE_DB=1 + SUPABASE_DB_URL;
  schema migrations/supabase/001_init.sql; TLS fingerprint-pinned)
- Node 26 + Vite/React 18/TypeScript/Tailwind 3 (frontend/)
- LLM: DeepSeek/Groq cloud APIs only — Ollama RETIRED 2026-08-28, nothing
  local runs

## Layout
backend/ (all Python; THE deployable module — paper pipeline + live_execution/
subpackage + run_live_cycle.py; run commands from inside it) · frontend/
(deployable to Vercel/CF Pages) · Dockerfile + docker-compose.yml +
.dockerignore (root) · backend/docker-entrypoint.sh · docs/ (…,
11_DEPLOYMENT.md) · memory-bank/ · handoff.md · start.sh / stop.sh ·
.env (root, GITIGNORED — real API keys live here only)

## Commands
- One click: ./start.sh | ./stop.sh
- Tests: cd backend && ../.venv/bin/python -m pytest tests/ -q  (434, ~5s)
- All suites from repo root: .venv/bin/python -m pytest -q  (583 = 434 backend
  + 149 live_execution; root pytest.ini sets asyncio_mode=auto)
- Frontend dev: cd frontend && npm run dev (:5173 proxies /api,/ws)
- KB ingest: cd backend && ../.venv/bin/python scripts/ingest_directory.py <dir>
- Deploy (Docker/VM): docker build -t trading-bot . && see docs/11_DEPLOYMENT.md

## External services & keys (in .env)
- Birdeye (BIRDEYE_API_KEY, X-API-KEY header) — REQUIRED for DATA_BACKEND=live;
  free tier 401s token_security (handled: session auto-disable)
- Dexscreener (DEXSCREENER_API_KEY optional; endpoints keyless today)
- Jupiter (JUPITER_API_KEY optional; lite-api.jup.ag/swap/v1/quote)
- fomo.fun board: FOMO_PRIVY_REFRESH_TOKEN (Privy session; rotated tokens
  auto-persisted to gitignored state file) + FIRECRAWL_API_KEY + stealth
  fallbacks SCRAPINGBEE/SCRAPINGDOG/ZENROWS/SCRAPEOPS keys. Chain:
  direct → firecrawl → scrapingbee(keyless-only) → zenrows(custom_headers+
  premium_proxy) → scrapeops(keep_headers). ZenRows/scrapeops forward the
  Privy bearer through Cloudflare (verified live).
- Supabase (USE_SUPABASE_DB, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY [server-
  only, bypasses RLS], SUPABASE_ANON_KEY, SUPABASE_DB_URL pooler :6543)
- DATA_BACKEND=mock|live is the single provider selection (A9)

## Environment rules
- .env holds operator settings only. Hardcoded in config.py, never env-set:
  PAPER_TRADING_ONLY, SLIPPAGE_PCT(0.02), FEE_PCT(0.01), and all rule/exit/
  regime/promotion thresholds (placeholders until calibration).
- Tests monkeypatch DATA_BACKEND=mock and tmp DB paths (hermeticity).

## Hardware reality
LLM is the bottleneck (~23 tok/s; narrations ~2s each after disabling qwen3
thinking). 20 candidates → tick ≈40–90s; TICK_INTERVAL_SECONDS=60 sleeps
between ticks regardless.
