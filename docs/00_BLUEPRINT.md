# 00 — Project Blueprint

## Vision

Build a local, self-hosted system that watches Solana memecoin markets in
real time, evaluates every candidate token against a strict, deterministic
rule set, and simulates trades against real market data — with a local LLM
narrating every decision in grounded, specific, plain language, and every
outcome logged so both the rules and the model's usefulness can be measured
and improved against a real track record. The system runs entirely in
paper-trading mode: no real funds, no wallet signing, no real transactions,
for the full duration of this build and for as long afterward as the
operator chooses.

## Primary goals

1. **Deterministic, auditable decisions.** The actual buy/hold/exit
   decision is made by pure, testable code — not by an LLM's freeform
   judgment. Given the same inputs, the system always produces the same
   decision.
2. **Grounded, non-hallucinated explanations.** The LLM explains decisions
   using only the specific data it was actually given — it should be
   structurally incapable of asserting a risk claim (e.g. mint authority
   status) it has no data for.
3. **A real, measurable calibration process.** Ten days of continuous
   paper trading against live market data, with daily review of which
   rules are responsible for which outcomes, so thresholds are tuned from
   evidence rather than guesswork.
4. **A visible, journal-like record.** Every decision — accepted or
   rejected — is logged with its full reasoning, viewable in a dashboard,
   so the system's behavior is fully auditable after the fact.
5. **Resilience to any single data provider.** Every external data source
   sits behind a formal interface, so a free-tier limit, an API change, or
   a provider going away doesn't require touching the core system.

## Non-goals

- **No real trade execution.** No wallet signing, no real swap
  broadcasting, no code path anywhere that can move real funds. If live
  trading is ever pursued, it is a separate, manual, human-reviewed
  decision entirely outside this system's scope.
- **No multi-user accounts.** Single operator, local-first tool.
- **No public deployment infrastructure.** Runs on localhost / LAN.
- **No cryptographic public-verification system in the initial build.**
  (See `05_VERIFICATION_APPENDIX.md` for this as documented future work.)

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Rule engine | Pure Python, no framework | Needs to be fast, dependency-free, and trivially unit-testable |
| Backend API | FastAPI (async) | Natural fit for async I/O against the LLM and external data APIs |
| Persistence | SQLite (via `aiosqlite`), WAL mode | Zero-ops, supports concurrent reads (API) while the tick loop writes |
| LLM runtime | Ollama, local | No cloud dependency, no per-call cost, full data privacy |
| LLM model | Qwen3-8B (`qwen3:8b`) | Empirically benchmarked on the target 6GB VRAM GPU: 100% valid structured output, ~23.6 tok/s — see Appendix note in 01_ARCHITECTURE.md |
| Market data | Birdeye (candidates, security), Dexscreener (buy/sell tx counts), Jupiter (execution-quality price) | Split by provider strength; all free-tier today, all behind one interface (see 01_ARCHITECTURE.md §6) |
| Frontend | React + TypeScript + Vite + Tailwind | Standard, fast local dev loop; matches the dashboard-style UI this project needs |
| Frontend charts | Recharts or Chart.js | Equity curve, portfolio stats |
| Real-time updates | WebSocket (live feed), polling (holdings/stats) | Live feed needs push; slower-moving panels don't |

## Directory layout

```
trading-bot/
├── config.py                    # all thresholds, safety flags, env loading
├── models.py                    # Candidate, Trade, RuleResult, GateDecision, etc.
├── rule_engine/
│   ├── __init__.py
│   ├── rules.py                 # the 9(+) individual rule functions
│   ├── gate.py                  # evaluate_gate(), GateDecision
│   └── regime.py                # MarketRegime, compute_market_regime()
├── data_providers/
│   ├── __init__.py
│   ├── base.py                  # MarketDataProvider protocol
│   ├── birdeye.py
│   ├── dexscreener.py
│   ├── jupiter.py
│   └── mock.py
├── llm/
│   ├── __init__.py
│   ├── narrator.py               # thesis generation from a GateDecision
│   ├── advisory.py               # optional deep-dive veto step (later phase)
│   └── reflection.py             # per-trade post-close reflection
├── paper_trading_engine.py       # open/close/scale-in, atomic, idempotent
├── knowledge_base/
│   ├── static_knowledge.md
│   ├── ingested/                 # operator-supplied material + digests
│   └── loader.py
├── main.py                       # the async tick loop
├── learning_loop.py               # daily aggregate stats + threshold review
├── promotion_gate.py              # read-only live-trading-readiness checklist
├── scripts/
│   └── ingest_directory.py        # bulk knowledge ingestion CLI
├── api/
│   ├── main.py                    # FastAPI app
│   ├── db.py                      # SQLite schema + repository functions
│   ├── websocket.py
│   └── routes/
│       ├── feed.py
│       ├── holdings.py
│       ├── journal.py
│       ├── stats.py
│       ├── knowledge_base.py
│       ├── promotion_gate.py
│       └── system_status.py
├── frontend/                      # React + Vite + Tailwind dashboard
├── tests/
│   ├── test_rules.py
│   ├── test_regime.py
│   ├── test_money_math.py
│   ├── test_promotion_gate.py
│   └── test_api_routes.py
├── docs/                          # this documentation set
├── .env.example
└── README.md
```

## Safety invariants (apply to every module without exception)

- `config.PAPER_TRADING_ONLY = True`, hardcoded, never environment-configurable.
- No file in this repository constructs, signs, or broadcasts a real
  Solana transaction, under any framing.
- `promotion_gate.py` is read-only: it evaluates and reports, and never
  triggers, writes, or activates anything.
- Every external input (API response, LLM output) is validated explicitly
  before it touches money math or a trading decision; validation failures
  fail closed (skip/reject), never fail open with a guessed default.
- A field whose value is genuinely unknown is represented as `None`, never
  coerced to `False`/`0` — an unknown security check is not the same claim
  as a passed security check.
