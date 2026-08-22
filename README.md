# trading-bot — local AI-assisted paper-trading research system

A deterministic, paper-trading research bot for Solana memecoins. **Decisions
are made by pure, testable code; the LLM only explains decisions that have
already been made.** No real funds, no wallet signing, no real transactions —
`config.PAPER_TRADING_ONLY = True` is hardcoded and asserted at runtime inside
every position-opening function.

Full specification lives in `docs/`: start at `00_BLUEPRINT.md`, then
`01_ARCHITECTURE.md`. Build status per requirement is tracked in
`docs/02_FEATURE_LIST.md`.

## Run (one click)

Double-click **Trading Bot (Paper)** in your application launcher, or run:

```bash
./start.sh
```

That single entry point: builds the dashboard, starts `ollama serve` if it
isn't already up (reusing it if it is), starts the backend + tick loop on
port 8000 serving both API and dashboard, waits for health, and opens
http://localhost:8000 in your browser. Re-running it is safe — running parts
are reused.

Stop everything with `./stop.sh` (a pre-existing ollama is left alone).
Logs land in `logs/`. To get the launcher icon:

```bash
cp trading-bot.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications/ 2>/dev/null || true
```

## Run manually (development)

```bash
# Backend + tick loop (serves the last built frontend at / too):
cd backend && TICK_LOOP_IN_PROCESS=1 ../.venv/bin/python -m uvicorn api.main:app --port 8000

# Frontend dev server with hot reload (proxies /api and /ws to :8000):
cd frontend && npm install && npm run dev    # http://localhost:5173

# Tests:
cd backend && ../.venv/bin/python -m pytest tests/ -q
```

Environment lives in `/.env` at the repo root (see `/.env.example`); it holds
operator-facing settings only. Safety-critical constants stay hardcoded in
`backend/config.py`.

## Layout

- `backend/` — all Python: rule engine, trading engine, providers, LLM
  layer, knowledge base, API, tests. Run everything from inside `backend/`.
  - `rule_engine/` — the 10 deterministic rules, `evaluate_gate()` (no
    short-circuiting), `compute_market_regime()` (once per tick).
  - `paper_trading_engine.py` — atomic, idempotent open/close/scale-in
    (§5.1: conditional state write first; rowcount decides whether cash moves).
  - `data_providers/` — mock (default), Birdeye + Dexscreener + Jupiter live
    stack behind one protocol; bounded retry, 429 backoff, daily call counters.
  - `llm/` — narrator (verdict pre-decided), groundedness validation,
    post-close reflections.
  - `knowledge_base/` — static knowledge, digest-at-ingest, budgeted context.
  - `api/` — FastAPI read endpoints + WS feed broadcaster.
- `frontend/` — React/Vite/Tailwind dashboard.
- `promotion_gate.py` (in `backend/`) — READ-ONLY readiness report; never
  triggers anything.

Bulk-ingest knowledge: .

## Calibration

## Knowledge base

Bulk-ingest operator notes (digested for prompts):

```bash
cd backend && ../.venv/bin/python scripts/ingest_directory.py <directory>
```

Calibration reference: `docs/06_REFERENCE_COMPARISON.md` compares our rules
and proof mechanism against omotrades.com.

## Provenance note

Portions of this design (the rule-engine-as-decision-maker pattern, the
market-regime gate concept) were informed by studying a comparable live
public system's decision audit log. The implementation here is this
project's own, built for its own goals and constraints (local hardware,
paper trading, a 10-day calibration window).
