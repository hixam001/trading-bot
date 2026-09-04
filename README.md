# trading-bot — AI-assisted Solana memecoin trading research system

A local trading research system for Solana memecoins. **§52: single-book** —
the paper simulation is retired; one live cycle runs the whole engine:

1. **The decision cycle (`backend/run_live_cycle.py`)** — every
   `TICK_INTERVAL_SECONDS` it pulls real market candidates (Birdeye trending
   memepool), enriches them (Dexscreener pairs, Jupiter pricing), computes a
   market-wide regime snapshot, and runs each candidate through a
   deterministic staged entry-rule set (cheap rules → fomo scrape → crowd
   rules → web search → social read, spend-disciplined per candidate).
   Entry requires **every rule to pass AND the LLM's buy verdict**; exits
   come from a reference-parity numeric exit engine (stop / trailing /
   liquidity-break / invalidation / stale / take-profit ladder) behind a
   separate sell-risk gate. The ExecutionLedger is the money authority; a
   one-way mirror keeps the shared `trades` table fed so calibration,
   learning, reflections and the dashboard keep a queryable home.
2. **Live execution (operator-ARMED in this repo)** — `backend/live_execution/`
   routes the cycle into real Jupiter swaps from a funded wallet. This repo
   is committed ARMED by its operator (2026-08-28, after the §27 devnet
   drill passed 5/5); the flags are editable **only by a human in
   `backend/live_execution/config.py`** (see [Live trading](#live-trading-operator-armed)).

**The LLM never trades.** Decisions are made by pure, testable code; the
model (DeepSeek by default) performs a pre-trade think/veto stage, narrates
decisions, and advances position write-ups — it never sizes, opens, closes,
or overrides numeric exits. No local model is required: all LLM work goes
through cloud APIs (DeepSeek / Groq). **Ollama is no longer part of the
stack** (a legacy offline fallback remains in code but nothing starts or
requires it).

Everything is journaled to SQLite (or optional Supabase Postgres) and shown
on a React dashboard served by the backend itself.

---

## Safety model (read this first)

- `backend/` (the shared brain: pipeline, rules, sizing, providers, LLM
  stages) contains **no wallet and no transaction construction anywhere**.
  It cannot touch real funds by construction.
- `backend/live_execution/` is the only real-execution code (a subpackage
  INSIDE backend/ so the engine deploys as one module — the shared brain
  still never imports it, test-pinned). It ships with
  `LIVE_TRADING_ENABLED = False` and `REQUIRE_MANUAL_CONFIRMATION = True`
  hardcoded — deliberately **not** settable via environment variables, so one
  stray `.env` line can never arm it. A kill switch, daily-loss breaker,
  per-trade/per-day caps, wallet identity pin, and an idempotency ledger
  guard it.
- Secrets live only in the gitignored root `.env` and (for live) a keypair
  file outside the repo **or** the `WALLET_KEYPAIR_JSON` env channel
  (in-memory only) for file-less hosts. API keys and keypair material are
  redacted from logs.

---

## Requirements

- Linux or macOS
- **Python 3.11+** (developed on 3.14) with `python3-venv`
- **Node.js 20+** + npm (for the dashboard build)
- API keys per the [.env section](#configuration-env) below — or run in
  `mock` mode with **zero keys** just to watch the machine move

---

## Quick start

```bash
git clone https://github.com/hixam001/trading-bot.git
cd trading-bot

# 1. Create your environment file and fill in your keys
cp .env.example .env
#    minimum for real-data cycles:
#      DATA_BACKEND=live
#      BIRDEYE_API_KEY=<your key>
#      MAIN_LLM_PROVIDER=deepseek
#      DEEPSEEK_API_KEY=<your key>

# 2. One click: creates .venv + installs deps, builds the dashboard,
#    starts the backend + tick loop on :8000, opens your browser.
./start.sh

# 3. Stop everything
./stop.sh
```

`./start.sh` is idempotent — re-running reuses whatever is already up. The
dashboard is served by the backend at **http://localhost:8000**. First run
takes a few minutes (venv + `npm install`); later starts take seconds.

## Configuration (.env)

All operator-facing settings live in `/.env` at the repo root (gitignored).
`/.env.example` documents **every** field. Risk-critical constants (all rule
thresholds, exit numbers, slippage/fee model, `PAPER_TRADING_ONLY`, the live
arm flags) are deliberately hardcoded in `backend/config.py` /
`live_execution/config.py` and are NOT in `.env`.

| Field | Required? | What it powers |
|---|---|---|
| `DATA_BACKEND` (`mock` \| `live`) | yes | `mock` = hermetic fake data (zero keys needed); `live` = real Birdeye/Dexscreener/Jupiter data |
| `BIRDEYE_API_KEY` | for `live` | candidate discovery (trending memepool). Free key from birdeye.so |
| `DEXSCREENER_API_KEY`, `JUPITER_API_KEY` | optional | currently keyless endpoints; keys reserved for paid tiers |
| `MAIN_LLM_PROVIDER` (`deepseek` \| `groq`) | yes | selects the main brain (think / narrate / reflections / thesis re-authoring) |
| `DEEPSEEK_API_KEY` | for `deepseek` | **the main model** — DeepSeek V4 Flash direct API (non-thinking mode) |
| `GROQ_API_KEY`, `GROQ_MODEL` | for `groq` | warm rollback provider (qwen3.8-27b on Groq) |
| `SOCIAL_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | optional | realtime social read stage (OpenAI-compatible; Groq free tier today). Empty key = stage off. Since §51 the read is **staged**: it runs only for candidates that passed every rule, so the free tier's 1,000 calls/day covers it |
| `FOMO_PRIVY_REFRESH_TOKEN` | optional | REAL crowd-conviction feed (fomo.fun board). Since §47 the board is read through the FREE local Scrapling transport (curl-cffi Chrome TLS first, stealth browser fallback) — no paid key needed; `FIRECRAWL_API_KEY`/`SCRAPING*_API_KEY` are shadow-week failover only. The scrape runs only AFTER a candidate passes every other rule (quota saver); rejects journal crowd_heat as "not evaluated". The rotated-token sidecar is stored **encrypted at rest** (`SECRET_STORE_KEY`/`SECRET_STORE_KEY_FILE`, §54) |
| `BRAVE_SEARCH_API_KEY`, `SEARXNG_URL` | optional | §51 FREE web-search evidence chain: Brave first (~1,000 free searches/month, auto credit), then your self-hosted SearXNG sidecar (keyless, unlimited), then Firecrawl last-resort. At least one configured = stage on; empty everywhere = off |
| `SCRAPINGBEE/SCRAPINGDOG/ZENROWS/SCRAPEOPS_API_KEY` | optional | stealth-scrape failover chain for the crowd feed (auto-benching on 402/429) — shadow-week backup for the free §47 transport |
| `USE_SUPABASE_DB`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_DB_URL` | optional | remote Postgres book instead of local SQLite (run `migrations/supabase/001_init.sql` once first) |
| `TICK_INTERVAL_SECONDS`, `MAX_CANDIDATES_PER_TICK`, `INITIAL_CASH_USD` | optional | cycle cadence + the historical equity-curve anchor (defaults 60 / 20 / 1000) |
| `WALLET_KEYPAIR_PATH`, `EXPECTED_WALLET_ADDRESS`, `SOLANA_RPC_URL` | live only | see [Live trading](#live-trading-operator-armed) |

If a main-LLM key is missing/invalid the system **fails closed**: the model
stage degrades to deterministic template passes that can never produce a buy.

## LLM providers — no local model needed

- **Main path (default): DeepSeek V4 Flash** (`MAIN_LLM_PROVIDER=deepseek`)
  — thesis formation, think/veto, narration, post-close reflections, and the
  thesis re-authoring job. Non-thinking mode is forced so the small output
  budget is never burned on hidden reasoning. DeepSeek bills peak hours
  (01:00–04:00 + 06:00–10:00 UTC weekdays) at 2×; non-urgent LLM jobs skip
  peak windows automatically.
- **Rollback: Groq** (`MAIN_LLM_PROVIDER=groq`) — one-line reversible flip.
- **Social read:** a separate OpenAI-compatible endpoint (`SOCIAL_LLM_*`,
  Groq's free tier today) — staged since §51: it only runs for candidates
  that passed every rule, so the free 1,000 requests/day budget covers it.
- Every call is accounted for (tokens, latency, cost estimate, degradation
  reason) in the `llm_call_usage` table; degradation is always honest and
  logged.
- **Ollama is retired** from the stack: nothing starts it and nothing
  requires it. (Legacy `OLLAMA_*` settings remain in `.env.example` purely
  for offline compatibility of an old fallback path.)

## Dashboard & API

The backend serves the built React dashboard and a set of read-only JSON
feeds (no endpoint can change trading state):

| Endpoint | Content |
|---|---|
| `http://localhost:8000/` | dashboard: live decision feed (WS), holdings, journal, stats, regime, status |
| `/api/stats` | five-number portfolio truth: cash, equity, total spend, realized + unrealized P&L |
| `/api/feed`, `/api/events.json` | per-candidate decision events (ENTER/PASS + full rule breakdown) |
| `/api/holdings`, `/api/journal` | open positions (live marks) / closed trades |
| `/api/theses.json` | the thesis book: every write-up with author + timestamps (restated write-ups advance here) |
| `/api/exits.json` | exit-engine activity |
| `/api/market-regime` | the tick's market-wide regime snapshot |
| `/api/refusals.json` | every gate/model refusal with full rule breakdown |
| `/api/disclosure.json` | public machine truth: armed state, kill switch, config caps/floors, sizing formulas |
| `/api/proof.json`, `/api/verify.json`, `/api/binding.json`, `/api/reasoning.json` | the decision-proof surface: commit seals, on-chain memo verification, fill binding, per-decision provenance |
| `/api/system-status` | health: provider call counters, LLM state |
| `/api/promotion-gate` | READ-ONLY live-readiness report (never writes, ever) |

## Tests

486 tests cover the rule engine, exit engine, money math (hand-computed
expectations), the atomic open/close journal, LLM provider swap, crowd feed,
risk budget × calibration, thesis restatement, the live-execution safety
model, and more.

```bash
.venv/bin/python -m pytest -q                            # full suite (~2s)
cd backend && ../.venv/bin/python -m pytest tests/ -q    # backend only
cd backend && ../.venv/bin/python -m pytest live_execution/tests/ -q   # live package only
```

Tests are hermetic: they force `DATA_BACKEND=mock` and their own tmp
databases — your `.env` and real book can never leak into them.

## Live trading (operator-ARMED)

> ⚠️ **Real money. Read `handoff.md` §26/§27/§31 before touching this.**
> §52: this IS the engine now — there is no paper bot anymore.

`live_execution/` routes the same read/think/gate brain into real Jupiter
swaps. **State committed to this repo (2026-08-28, explicit operator
direction after the devnet drill passed 5/5): `LIVE_TRADING_ENABLED = True`,
`REQUIRE_MANUAL_CONFIRMATION = False`.** Both flags are editable **only by a
human in `live_execution/config.py`** — no env bypass exists by design.
Remaining safety layers (all still active): kill switch file, automatic
daily-loss breaker, per-trade $50 / per-day $300 / 3-position caps, wallet
identity pin, idempotency ledger, SOL-reserve + USDC funding checks, and an
on-chain commit memo published BEFORE every fill (commit–reveal proof).
**If you clone this repo and do NOT want it trading real money, flip
`LIVE_TRADING_ENABLED = False` before running anything** — and never point
`WALLET_KEYPAIR_PATH` at a funded wallet.

### Where the keys go

| What | Where | Notes |
|---|---|---|
| Wallet keypair | a JSON file OUTSIDE the repo (e.g. `~/.config/solana/trading-keypair.json`), path in `.env` `WALLET_KEYPAIR_PATH` — or, on file-less hosts, `WALLET_KEYPAIR_JSON` in the env (in-memory only; see `docs/11_DEPLOYMENT.md`) | 64-byte JSON array (solana-cli format) or base58. **Never commit it.** Fund it with a small SOL fee reserve (~0.03 SOL) + separate USDC capital ($3–5 to start) |
| Wallet identity pin | `.env` `EXPECTED_WALLET_ADDRESS` | the loaded keypair MUST derive this exact pubkey or loading refuses loudly |
| RPC | `.env` `SOLANA_RPC_URL` | public endpoint rate-limits hard; use a paid RPC for real use |

### Arming procedure (what the operator did — human-only, in order)

1. Ran the **devnet drill** until it passed 100%:
   `python run_live_cycle.py --drill` (exercises wallet load, identity pin,
   chain reads, sign/send/confirm, and the commit-memo path — devnet only,
   no Jupiter, no tokens). Passed 5/5 on 2026-08-28 (handoff §31).
2. Funded the **mainnet** wallet (0.03 SOL + $3–5 USDC) and pointed `.env` at
   it (`WALLET_KEYPAIR_PATH`, `EXPECTED_WALLET_ADDRESS`, `SOLANA_RPC_URL`).
3. Hand-edited `live_execution/config.py`: `LIVE_TRADING_ENABLED = True`,
   `REQUIRE_MANUAL_CONFIRMATION = False` — and committed that state.
4. Supervised one cycle: `python run_live_cycle.py --once`.

Rollback is one line: `LIVE_TRADING_ENABLED = False`. A test
(`test_safety_flags_match_the_committed_state`) pins the committed flag
state so any silent flip in either direction fails loudly.

Live cash is accurate by construction: it is the wallet's real on-chain USDC
balance re-read every cycle (never an internal accumulator), and exits are
real swaps — the chain is the source of truth.

## Repository layout

```
backend/                 the single deployable module (shared brain + LIVE execution)
  rule_engine/           deterministic entry rules, exit engine + sell gate, regime, liveness
  sizing.py              the pure money math (risk budget / tickets / P&L) — §52
  data_providers/        birdeye / dexscreener / jupiter / crowd / mock / live stack
  run_live_cycle.py      THE decision cycle (manage → read → think → gate → execute)
  decision_pipeline.py   the shared read→think→gate core
  llm/                   thinker, narrator, social/web research, provider clients
  thesis_restate.py      A11 write-up re-authoring job (narrative-only)
  calibration.py         closed-loop conviction factor (bounds 0.6–1.2)
  api/                   FastAPI app + repository layer (db.py SQLite / db_pg.py Postgres)
  live_execution/        REAL-money package (operator-ARMED; the shared brain never imports it)
  run_live_cycle.py      live decision cycle runner + --drill
  tests/                 backend test suite
  live_execution/tests/  live-package test suite
  docker-entrypoint.sh   container entrypoint (API always; live cycle only if armed + wallet)
frontend/                React + Vite + Tailwind dashboard (deployable to Vercel/CF Pages)
Dockerfile               single-module engine image (frontend baked in for same-origin)
docker-compose.yml       local parity / VM run (persistent volumes for state + book)
migrations/supabase/     one-time SQL for the optional Postgres book
docs/                    00 blueprint → 11 DEPLOYMENT runbook
memory-bank/             structured context for future sessions
handoff.md               complete state/decisions/invariants — READ FIRST
logs/                    backend.log, frontend-build.log
.run/                    pid files for start.sh/stop.sh
```

## Development (manual run)

```bash
# Backend + tick loop (also serves the last built frontend):
cd backend && TICK_LOOP_IN_PROCESS=1 ../.venv/bin/python -m uvicorn api.main:app --port 8000

# Frontend hot-reload dev server (proxies /api and /ws to :8000):
cd frontend && npm install && npm run dev      # http://localhost:5173

# One autonomous live-style decision cycle (ARMED in this repo — real money if a funded wallet is configured):
python run_live_cycle.py --once
```

## Logs & state

- `logs/backend.log` — tick loop, LLM calls, exits, refusals (keys redacted)
- `.run/backend.pid` — used by `./stop.sh`
- `backend/trading_bot.db` (or `DB_PATH`) — the decision journal + the
  live book's mirrored trades (the ExecutionLedger in
  `live_execution/state/` is the money authority)
- `live_execution/state/` — live confirmations, idempotency ledger, kill
  switch (gitignored)

## Documentation

- **`handoff.md`** — the complete handoff: state, decision log, bugs fixed,
  invariants, next steps. Read this first.
- **`memory-bank/`** — structured session context (activeContext, progress,
  decisionLog, session-log).
- **`docs/`** — `00_BLUEPRINT`, `01_ARCHITECTURE`, `02_FEATURE_LIST`,
  `05_VERIFICATION_APPENDIX`, `06_REFERENCE_COMPARISON`,
  `07_PROJECT_REPORT`, `08_LLM_API_MIGRATION`,
  `11_DEPLOYMENT` (runbook), `12_ORACLE_DEPLOY_GUIDE` (step-by-step free-tier deploy).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot runs but all data is fake | `DATA_BACKEND=mock` — set it to `live` and add `BIRDEYE_API_KEY`, then restart |
| "main LLM provider has no API key" warning on start | fill `DEEPSEEK_API_KEY` (or switch `MAIN_LLM_PROVIDER=groq` + `GROQ_API_KEY`); until then the model stage fails closed to templates |
| Backend won't start | `tail -50 logs/backend.log`; most often port 8000 is taken (`./stop.sh` first) |
| Dashboard blank after code changes | rebuild: `cd frontend && npm run build`, or just re-run `./start.sh` |
| Crowd feed shows weak conviction | the fomo.fun board needs `FOMO_PRIVY_REFRESH_TOKEN` (free Scrapling transport does the rest — `.env.example` §6). Weak heat with the token set usually means the board itself has few theses |
| No web-search evidence lines | §51 chain off or benched: set `BRAVE_SEARCH_API_KEY` (free) and/or run the SearXNG sidecar + `SEARXNG_URL`; logs show `search hop … benched` when a hop exhausted its quota |
| DeepSeek calls skipped at night | peak-window pricing guard (01:00–04:00 / 06:00–10:00 UTC weekdays); non-urgent jobs resume off-peak |
| Test suite shows a red flag-state test | someone flipped `live_execution/config.py` without updating the pinning test — check `git diff` on that file before doing anything |

## Provenance note

Portions of this design (the rule-engine-as-decision-maker pattern, the
market-regime gate, the exit engine contract, the commit–reveal proof) were
informed by studying comparable public systems' decision audit logs, notably
the published `omotrades/omo` repository. The implementation here is this
project's own, built for its own goals and constraints (local hardware,
defense-first safety model).

## Open-source credits

- [Scrapling](https://github.com/D4Vinci/Scrapling) by Karim Shoair
  (BSD-3-Clause) — the local stealth transport for our crowd-conviction
  feed reads: `AsyncFetcher` (curl-cffi Chrome-TLS impersonation, the
  direct hop) and `AsyncStealthySession` (the stealth-browser hop with
  Cloudflare solver), via the `[fetchers]` extra.
- [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) by
  Vinyzu (Apache-2.0) — the undetected Playwright fork powering Scrapling's
  stealth engine.
- [Playwright](https://playwright.dev) (Apache-2.0) and
  [curl-cffi](https://github.com/lexiforest/curl_cffi) (MIT) — the
  foundations underneath.



