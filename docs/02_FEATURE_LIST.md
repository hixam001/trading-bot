# 02 — Feature List

Every functional requirement, organized by module. `Status` tracks build
progress (updated as items complete).

## A. Data ingestion & provider abstraction

| # | Requirement | Status |
|---|---|---|
| A1 | `MarketDataProvider` protocol defined (`get_candidates`, `get_current_price`, `get_security_info`) | DONE |
| A2 | Birdeye provider implementation: candidate discovery, liquidity, volume, price, market cap | DONE + LIVE-VERIFIED (memepool trending endpoint; field names confirmed against real responses) |
| A3 | Birdeye `token_security` integration for mint/freeze authority + honeypot fields, `Optional[bool]`, `None` when unknown | DONE; NOTE: free-tier key returns 401 on this endpoint → auto-disables per session, fields stay UNKNOWN (never False) |
| A4 | Dexscreener provider implementation: buys/sells 1h + 1h volume/change, liquidity, mcap, pair age, presence channels | DONE + LIVE-VERIFIED (field names confirmed against real responses) |
| A5 | Jupiter provider implementation: live execution-quality price for open positions | DONE + LIVE-VERIFIED (lite-api.jup.ag swap/v1/quote; old v6 URL was sunset) |
| A6 | Mock provider: plausible synthetic values for every field above, including edge cases (below/above every threshold) | DONE (verified end-to-end) |
| A7 | Bounded retry + backoff on every external call; distinct handling and logging for HTTP 429 | DONE |
| A8 | Per-provider daily call counter, logged/queryable | DONE |
| A9 | `DATA_BACKEND`/provider selection is a single config value; swapping providers touches no other module | DONE |

## B. Rule engine (deterministic decision-maker)

| # | Requirement | Status |
|---|---|---|
| B1 | `RuleResult`, `GateDecision`, `RuleFn`, `evaluate_gate()` implemented per architecture §2 | DONE |
| B2–B11 | All 10 individual rule functions + unit tests, both branches each (incl. joint `not_newborn_fade`, zero/nonzero `exposure_cap`, `None`-passes-`security_clear`) | DONE |
| B12 | No short-circuiting: every rule evaluated and logged even after an earlier rule fails | DONE (`test_gate_no_short_circuit_all_rules_present_on_failure`) |
| B13 | `Candidate` model with all new fields (`buys_1h`, `sells_1h`, presence channels, security fields) | DONE |

## C. Market regime gate

| # | Requirement | Status |
|---|---|---|
| C1 | `MarketRegime` dataclass + `compute_market_regime()` per architecture §3 | DONE |
| C2 | Computed once per tick from the full candidate batch, before per-candidate evaluation | DONE (e2e test asserts exactly one row per tick) |
| C3 | `market_regime` table/log — queryable independently of trade records | DONE (+ `candidate_count` column for empty-batch disambiguation) |
| C4 | Starting thresholds documented in `config.py` as placeholders needing calibration | DONE |
| C5 | `GET /api/market-regime` endpoint | DONE |

## D. LLM narration and advisory layer

| # | Requirement | Status |
|---|---|---|
| D1 | Narration prompt implemented exactly per architecture §4.1 (verdict pre-decided, rules-only grounding) | DONE |
| D2 | Validation: non-empty thesis; flag (not silently drop) any mention of a rule/term not present in the candidate's actual rule list | DONE (rule-derived vocabulary + invented-rule check + numeric echo check; flags recorded on feed events) |
| D3 | Persistent `httpx.AsyncClient`, reused across calls (no per-call client construction) | DONE |
| D4 | Ollama health check + `GET /api/system-status` surfacing it | DONE |
| D5 | Per-trade post-close reflection: fire-and-forget async task, never blocks the tick loop | DONE |
| D6 | Reflection text stored on the `Trade` record and surfaced in the journal | DONE |
| D7 | *(Later phase)* Optional advisory deep-dive step: can add risk flags/lower confidence, cannot override a rule-engine verdict in either direction | NOT STARTED (deliberately post-calibration) |


## E. Paper trading engine

| # | Requirement | Status |
|---|---|---|
| E1 | `open_position()` — atomic, idempotent (write-then-check pattern, cash only touched after confirmed state write) | DONE (`try_insert_open_trade` + partial UNIQUE index backstop) |
| E2 | `close_position()` — same atomicity pattern; double-close test proves cash credited exactly once | DONE |
| E3 | `scale_into_position()` — new function, same atomicity discipline as E1/E2 (exposure cap enforced atomically in the UPDATE's WHERE clause) | DONE |
| E4 | `decide_and_act()` — unified entry point routing to E1 or E3 based on existing exposure | DONE |
| E5 | `compute_unrealized_pnl()` / `compute_realized_pnl()` — pure functions, tested with known-correct expected outputs, raise on invalid input rather than returning 0 | DONE |
| E6 | `check_exit_conditions()` — take-profit, stop-loss, timeout, in that order | DONE (order test included) |
| E7 | `PAPER_TRADING_ONLY` asserted at runtime inside every position-opening function (belt-and-suspenders, not just checked once upstream) | DONE (`config.assert_paper_trading_only()` + dedicated test) |
| E8 | *(Post-calibration)* `scale_out_partial()` — trims a fraction of an open position on a structural-trim trigger | NOT STARTED (per §5.3 sequencing) |
| E9 | *(Post-calibration)* Rolling price/volume history per open position | NOT STARTED |

## F. Knowledge base

| # | Requirement | Status |
|---|---|---|
| F1 | `static_knowledge.md` loaded and injected into every scoring/narration prompt | DONE |
| F2 | `ingest_file()` — single-file ingestion, filename sanitized, empty content rejected | DONE |
| F3 | `scripts/ingest_directory.py` — bulk ingestion CLI | DONE |
| F4 | `POST /api/knowledge-base/ingest` — bulk/batch ingestion via API | DONE |
| F5 | Digest generation at ingest time, stored alongside raw source | DONE (Ollama in live mode; labeled extractive fallback otherwise) |
| F6 | `get_context()` injects digests (not raw files) into prompts, bounded by a configured char budget | DONE |
| F7 | Truncation drops whole documents, never cuts mid-document | DONE |
| F8 | Dynamic stats: win rate by liquidity bucket, age bucket, computed from real trade history | DONE |
| F9 | `GET /api/knowledge-base` returns static + ingested content + dynamic stats | DONE |

## G. Learning loop and promotion gate

| # | Requirement | Status |
|---|---|---|
| G1 | Daily aggregate stats: win rate, profit factor, max drawdown, total P&L | DONE |
| G2 | Per-rule rejection-rate breakdown — which rules are responsible for the most rejections | DONE |
| G3 | Human-reviewed threshold recommendations — logged, never auto-applied | DONE |
| G4 | `promotion_gate.py` — pure, read-only, evaluates 5 criteria | DONE |
| G5 | `promotion_gate.py` never writes to the database, never modifies config, never triggers anything | DONE (invariant documented at top of file) |
| G6 | `GET /api/promotion-gate` mirrors `promotion_gate.evaluate()` exactly | DONE |

## H. API layer

| # | Requirement | Status |
|---|---|---|
| H1 | All endpoints listed in architecture §7 implemented | DONE |
| H2 | Every endpoint read-only with respect to trades/safety flags | DONE (only POST is KB ingestion) |
| H3 | `WS /ws/feed` pushes new feed events in real time | DONE |
| H4 | Pagination on `feed`, `journal` endpoints | DONE (+ tests) |
| H5 | SQLite in WAL mode; tick loop writes, API reads, without blocking each other | DONE |

## I. Frontend

| # | Requirement | Status |
|---|---|---|
| I1–I9 | All panels: live feed (WS, expandable rule breakdown), holdings (live price + color P&L), journal (thesis+reflection+outcome, filter/sort), stats dashboard (equity curve), regime panel, promotion gate (display only), knowledge base, system status, persistent "PAPER TRADING — NO REAL FUNDS" banner | BUILT (tsc + vite build pass; not yet exercised in a browser) |
| I10 | Graceful degraded state if the API is unreachable | DONE (explicit offline banner + auto-retry) |

## J. Testing

| # | Requirement | Status |
|---|---|---|
| J1 | Every rule function: unit tests, both branches | DONE (73 tests passing) |
| J2 | Money-math functions: known-correct expected outputs + edge cases | DONE |
| J3 | Atomicity: double-close and double-open tests | DONE (incl. crash-replay + scale-in cap + PAPER_TRADING_ONLY assertion) |
| J4 | API routes: shape/pagination tests against a seeded test DB | DONE |
| J5 | End-to-end mock-mode smoke test covering a full tick cycle | DONE (entries, forced exits, cash conservation, once-per-tick regime) |

## K. Safety and observability

| # | Requirement | Status |
|---|---|---|
| K1 | `config.PAPER_TRADING_ONLY` hardcoded, not env-configurable | DONE |
| K2 | No real-execution code path anywhere in the repository | DONE (no wallet/tx code exists) |
| K3 | Structured logging throughout (not print statements) | DONE |
| K4 | Decision-to-log latency instrumentation | DONE (per-tick elapsed ms logged) |
| K5 | Every rejection logged with its specific reason, same detail level as every acceptance | DONE (full 10-rule breakdown on every feed event) |

