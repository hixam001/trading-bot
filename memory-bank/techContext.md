# Tech Context — trading-bot

## Stack
- Python 3.14 (system) + `.venv/` at repo root; deps in backend/requirements.txt
  (fastapi, uvicorn[standard], httpx, aiosqlite, python-dotenv, pytest, pytest-asyncio)
- SQLite via aiosqlite, WAL mode, db file backend/trading_bot.db
- Node 26 + Vite/React 18/TypeScript/Tailwind 3 (frontend/)
- Ollama + qwen3:8b (local; narration; think mode disabled)

## Layout
backend/ (all Python; run commands from inside it) · frontend/ · docs/ ·
memory-bank/ · handoff.md · start.sh / stop.sh / trading-bot.desktop ·
.env (root, GITIGNORED — real API keys live here only)

## Commands
- One click: ./start.sh | ./stop.sh
- Tests: cd backend && ../.venv/bin/python -m pytest tests/ -q  (80, <1s)
- Frontend dev: cd frontend && npm run dev (:5173 proxies /api,/ws)
- KB ingest: cd backend && ../.venv/bin/python scripts/ingest_directory.py <dir>

## External services & keys (in .env)
- Birdeye (BIRDEYE_API_KEY, X-API-KEY header) — REQUIRED for DATA_BACKEND=live;
  free tier 401s token_security (handled: session auto-disable)
- Dexscreener (DEXSCREENER_API_KEY optional; endpoints keyless today)
- Jupiter (JUPITER_API_KEY optional; lite-api.jup.ag/swap/v1/quote)
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
