# trading-bot — AI-assisted Solana memecoin trading research system

A local trading research system for Solana memecoins with two layers:

1. **Paper-trading engine (default, always safe)** — every ~60s tick it pulls
   real market candidates (Birdeye trending memepool), enriches them
   (Dexscreener pairs, Jupiter pricing), computes a market-wide regime
   snapshot, and runs each candidate through a deterministic entry-rule set.
   Entry requires **every rule to pass AND the LLM's buy verdict**; exits
   come from a reference-parity numeric exit engine (stop / trailing /
   liquidity-break / invalidation / stale / take-profit ladder) scanned every
   15s. All money is simulated — `PAPER_TRADING_ONLY = True` is hardcoded and
   runtime-asserted inside every position-opening function.
2. **Live-execution package (optional, ships DISARMED)** — `live_execution/`
   can route the same brain into real Jupiter swaps from a wallet you fund.
   It ships hardcoded OFF and can only be armed by a human editing its
   config file (see [Live trading](#live-trading-optional--ships-disarmed)).

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

- `backend/` (the paper pipeline) contains **no wallet and no transaction
  construction anywhere**. It cannot touch real funds by construction.
- `live_execution/` is the only real-execution code. It ships with
  `LIVE_TRADING_ENABLED = False` and `REQUIRE_MANUAL_CONFIRMATION = True`
  hardcoded — deliberately **not** settable via environment variables, so one
  stray `.env` line can never arm it. A kill switch, daily-loss breaker,
  per-trade/per-day caps, wallet identity pin, and an idempotency ledger
  guard it.
- Secrets live only in the gitignored root `.env` and (for live) a keypair
  file outside the repo. API keys are redacted from logs.

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
#    minimum for real-data paper trading:
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
| `SOCIAL_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | optional | realtime social read stage (OpenAI-compatible; Groq today). Empty key = stage off |
| `FOMO_PRIVY_REFRESH_TOKEN` + `FIRECRAWL_API_KEY` | optional | REAL crowd-conviction feed (fomo.fun board via stealth proxy). Empty = crowd_heat degrades to a presence proxy |
| `SCRAPINGBEE/SCRAPINGDOG/ZENROWS/SCRAPEOPS_API_KEY` | optional | stealth-scrape failover chain for the crowd feed (auto-benching on 402/429) |
| `USE_SUPABASE_DB`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_DB_URL` | optional | remote Postgres book instead of local SQLite (run `migrations/supabase/001_init.sql` once first) |
| `TICK_INTERVAL_SECONDS`, `MAX_CANDIDATES_PER_TICK`, `INITIAL_CASH_USD` | optional | tick cadence + paper book starting cash (defaults 60 / 20 / 1000) |
| `WALLET_KEYPAIR_PATH`, `EXPECTED_WALLET_ADDRESS`, `SOLANA_RPC_URL` | live only | see [Live trading](#live-trading-optional--ships-disarmed) |

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
- **Social read:** a separate OpenAI-compatible endpoint (`SOCIAL_LLM_*`).
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
cd live_execution && ../.venv/bin/python -m pytest tests/ -q   # live package only
```

Tests are hermetic: they force `DATA_BACKEND=mock` and their own tmp
databases — your `.env` and real book can never leak into them.

## Live trading (optional — ships DISARMED)

> ⚠️ **Real money. Read `handoff.md` §26/§27 before even thinking about
> this.** Nothing in this section is needed for the paper bot.

`live_execution/` routes the same read/think/gate brain into real Jupiter
swaps. Safety model: hardcoded `LIVE_TRADING_ENABLED = False` master switch +
`REQUIRE_MANUAL_CONFIRMATION = True` (both editable **only by a human in
`live_execution/config.py`** — no env bypass exists by design), kill switch
file, automatic daily-loss breaker, per-trade $50 / per-day $300 / 3-position
caps, wallet identity pin, idempotency ledger, SOL-reserve + USDC funding
checks, and an on-chain commit memo published BEFORE every fill
(commit–reveal proof).

### Where the keys go

| What | Where | Notes |
|---|---|---|
| Wallet keypair | a JSON file OUTSIDE the repo (e.g. `~/.config/solana/trading-keypair.json`), path in `.env` `WALLET_KEYPAIR_PATH` | 64-byte JSON array (solana-cli format) or base58. **Never commit it.** Fund it with a small SOL fee reserve (~0.03 SOL) + separate USDC capital ($3–5 to start) |
| Wallet identity pin | `.env` `EXPECTED_WALLET_ADDRESS` | the loaded keypair MUST derive this exact pubkey or loading refuses loudly |
| RPC | `.env` `SOLANA_RPC_URL` | public endpoint rate-limits hard; use a paid RPC for real use |

### Arming procedure (human-only, in order)

1. Run the **devnet drill** until it passes 100%:
   `python run_live_cycle.py --drill` (exercises wallet load, identity pin,
   chain reads, sign/send/confirm, and the commit-memo path — devnet only,
   no Jupiter, no tokens).
2. Fund the **mainnet** wallet (0.03 SOL + $3–5 USDC) and point `.env` at it
   (`WALLET_KEYPAIR_PATH`, `EXPECTED_WALLET_ADDRESS`, `SOLANA_RPC_URL`).
3. Hand-edit `live_execution/config.py`: `LIVE_TRADING_ENABLED = True`
   (later optionally `REQUIRE_MANUAL_CONFIRMATION = False`).
4. Supervise one cycle: `python run_live_cycle.py --once`.

Rollback is one line: `LIVE_TRADING_ENABLED = False`. The repo itself always
ships disarmed — a test (`test_safety_flags_are_hardcoded_safe_defaults`)
enforces that and stays red on a machine that is armed, by design.

Live cash is accurate by construction: it is the wallet's real on-chain USDC
balance re-read every cycle (never an internal accumulator), and exits are
real swaps — the chain is the source of truth.

## Repository layout

```
backend/                 paper pipeline (ALL Python; run from inside backend/)
  rule_engine/           deterministic entry rules, exit engine, gate, regime, liveness
  paper_trading_engine.py  atomic money math (rowcount decides whether cash moves)
  data_providers/        birdeye / dexscreener / jupiter / crowd / mock / live stack
  llm/                   thinker, narrator, social/web research, provider clients
  thesis_restate.py      A11 write-up re-authoring job (narrative-only)
  calibration.py         closed-loop conviction factor (bounds 0.6–1.2)
  api/                   FastAPI app + repository layer (db.py SQLite / db_pg.py Postgres)
  tests/                 backend test suite
frontend/                React + Vite + Tailwind dashboard
live_execution/          REAL-money package (ships disarmed; never imported by backend/)
run_live_cycle.py        root bridge: live decision cycle + --drill
migrations/supabase/     one-time SQL for the optional Postgres book
docs/                    00 blueprint → 09 omo audit comparison
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

# One autonomous live-style decision cycle (disarmed by default):
python run_live_cycle.py --once
```

## Logs & state

- `logs/backend.log` — tick loop, LLM calls, exits, refusals (keys redacted)
- `.run/backend.pid` — used by `./stop.sh`
- `backend/trading_bot.db` (or `DB_PATH`) — the paper book
- `live_execution/state/` — live confirmations, idempotency ledger, kill
  switch (gitignored)

## Documentation

- **`handoff.md`** — the complete handoff: state, decision log, bugs fixed,
  invariants, next steps. Read this first.
- **`memory-bank/`** — structured session context (activeContext, progress,
  decisionLog, session-log).
- **`docs/`** — `00_BLUEPRINT`, `01_ARCHITECTURE`, `02_FEATURE_LIST`,
  `05_VERIFICATION_APPENDIX`, `06_REFERENCE_COMPARISON`,
  `07_PROJECT_REPORT`, `08_LLM_API_MIGRATION`, `09_OMO_AUDIT_COMPARISON`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot runs but all data is fake | `DATA_BACKEND=mock` — set it to `live` and add `BIRDEYE_API_KEY`, then restart |
| "main LLM provider has no API key" warning on start | fill `DEEPSEEK_API_KEY` (or switch `MAIN_LLM_PROVIDER=groq` + `GROQ_API_KEY`); until then the model stage fails closed to templates |
| Backend won't start | `tail -50 logs/backend.log`; most often port 8000 is taken (`./stop.sh` first) |
| Dashboard blank after code changes | rebuild: `cd frontend && npm run build`, or just re-run `./start.sh` |
| Crowd feed shows weak conviction | the fomo.fun board needs `FOMO_PRIVY_REFRESH_TOKEN` + `FIRECRAWL_API_KEY` (see `.env.example` §6) |
| DeepSeek calls skipped at night | peak-window pricing guard (01:00–04:00 / 06:00–10:00 UTC weekdays); non-urgent jobs resume off-peak |
| Test suite shows 1 red test | your machine is armed (`LIVE_TRADING_ENABLED=True`) — that's the ships-disarmed canary working as designed |

## Provenance note

Portions of this design (the rule-engine-as-decision-maker pattern, the
market-regime gate, the exit engine contract, the commit–reveal proof) were
informed by studying comparable public systems' decision audit logs, notably
the published `omotrades/omo` repository (full comparison in
`docs/09_OMO_AUDIT_COMPARISON.md`). The implementation here is this project's
own, built for its own goals and constraints (local hardware, paper-first,
defense-first safety model).



