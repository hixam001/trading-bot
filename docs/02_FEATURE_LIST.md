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
| B2–B11 | All 10 active rule functions + unit tests, both branches each (incl. joint `not_newborn_fade`, `already_held` one-position-per-name, `crowd_heat` band and deferred lookup, `not_on_break` fail-closed, `None`-passes-`security_clear`) | DONE |
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

## D. LLM verdict and thesis layer

| # | Requirement | Status |
|---|---|---|
| D1 | Post-gate verdict + grounded thesis per architecture §4.1 (rules decide eligibility; LLM may veto a survivor, never admit a rejection) | DONE (`llm/thinker.py`, `entry_allowed = gate.all_passed and think.wants_entry`) |
| D2 | Validation: non-empty thesis; flag (not silently drop) any mention of a rule/term not present in the candidate's actual rule list | DONE (rule-derived vocabulary + invented-rule check + numeric echo check; flags recorded on feed events) |
| D3 | Persistent `httpx.AsyncClient`, reused across calls (no per-call client construction) | DONE |
| D4 | Main-LLM health surfaced by `GET /api/system-status` | DONE (DeepSeek/Groq; Ollama retired §52 — no local provider is probed) |
| D5 | Per-trade post-close reflection: fire-and-forget async task, never blocks the tick loop | DONE |
| D6 | Reflection text stored on the `Trade` record and surfaced in the journal | DONE |
| D7 | Reference-style single-call brain (`LLM_BRAIN=1`) mapped onto the same post-gate veto | DONE (`llm/llm_brain.py`; malformed output degrades to the per-candidate thinker) |
| D8 | *(Later phase)* Additional advisory deep-dive that can add risk flags/lower confidence | NOT STARTED (deliberately post-calibration; the D1 veto already gates entries) |


## E. Live execution book (paper engine retired §52)

| # | Requirement | Status |
|---|---|---|
| E1 | `ExecutionLedger.record_buy()` — idempotency key + atomic temp-file/`os.replace` JSON persistence; corrupt ledger refuses to trade | DONE (`live_execution/models.py`, `test_live_execution_ledger.py`) |
| E2 | `reduce_position()` — FIFO partial/full close, `full_close` clamp, PnL realized against cost, one save | DONE |
| E3 | Pre-trade guards: kill switch, daily-loss breaker, position/exposure caps, wallet pin, commit memo | DONE (`jupiter_executor.preflight`, `executor.place_buy/place_sell`) |
| E4 | Single confirmed-trade path (`place_buy`/`place_sell` → `execute_confirmed_trade`) with ambiguous-send resolution by signature status | DONE (`test_executor_memo_flow.py`, `test_jupiter_executor.py`) |
| E5 | `compute_unrealized_pnl()` / `compute_realized_pnl()` — pure functions with known-correct expectations, invalid input rejected | DONE (`backend/sizing.py`) |
| E6 | Exit engine: stop/trail/liquidity/invalidation/stale full closes + take-profit ladder trims | DONE (`rule_engine/exits.py`, `test_exit_engine.py`) |
| E7 | Live arming hardcoded (`LIVE_TRADING_ENABLED`, `REQUIRE_MANUAL_CONFIRMATION`), never env-settable | DONE (`live_execution/config.py`, pinned by `test_safety_flags_match_the_committed_state`) |
| E8 | Partial trims of an open position | DONE (TP ladder + `reduce_position(fraction)`) |
| E9 | High-water marks survive restart and `--once` | DONE (mirror persistence + `run_live_cycle._hwm_for_cycle`) |

## F. Knowledge base

| # | Requirement | Status |
|---|---|---|
| F1 | `static_knowledge.md` loaded and injected into every scoring/narration prompt | DONE |
| F2 | `ingest_file()` — single-file ingestion, filename sanitized, empty content rejected | DONE |
| F3 | `scripts/ingest_directory.py` — bulk ingestion CLI | DONE |
| F4 | `POST /api/knowledge-base/ingest` — bulk/batch ingestion via API | DONE |
| F5 | Digest generation at ingest time, stored alongside raw source | DONE (main cloud LLM when available; labeled extractive fallback otherwise — no local/Ollama path) |
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
| H2 | No endpoint can open, close, or modify a trade or safety flag; operator mutations are limited to auth, KB ingestion, and `POST /api/admin/reset` (requires `X-Admin-Token`) | DONE (`test_admin_reset.py`, `test_authz_surface.py`) |
| H3 | `WS /ws/feed` pushes new feed events in real time | DONE |
| H4 | Pagination on `feed`, `journal` endpoints | DONE (+ tests) |
| H5 | SQLite in WAL mode; tick loop writes, API reads, without blocking each other | DONE |

## I. Frontend

| # | Requirement | Status |
|---|---|---|
| I1–I9 | All panels: live feed (WS, expandable rule breakdown), holdings (live price + color P&L), journal (thesis+reflection+outcome, filter/sort), stats dashboard (equity curve), regime panel, promotion gate (display only), knowledge base, system status, persistent status banner ("DISARMED — NO LIVE TRADING" / "● LIVE · real money") | DONE (vite build clean, Playwright E2E suites passing) |
| I10 | Graceful degraded state if the API is unreachable | DONE (explicit offline banner + auto-retry) |

## J. Testing

| # | Requirement | Status |
|---|---|---|
| J1 | Every rule function: unit tests, both branches | DONE (73 tests passing) |
| J2 | Money-math functions: known-correct expected outputs + edge cases | DONE |
| J3 | Atomicity: crash-replay and double-close protections on the ledger | DONE (live `ExecutionLedger` temp-file + `os.replace`; exit/TP tests assert no double-credit) |
| J4 | API routes: shape/pagination tests against a seeded test DB | DONE |
| J5 | End-to-end mock-mode smoke test covering a full tick cycle | DONE (entries, forced exits, cash conservation, once-per-tick regime) |

## K. Safety and observability

| # | Requirement | Status |
|---|---|---|
| K1 | Safety flags hardcoded (`LIVE_TRADING_ENABLED`, `REQUIRE_MANUAL_CONFIRMATION`), not env-configurable | DONE |
| K2 | Kill switch, daily loss breaker, wallet pin, position caps, and on-chain commit memos guard execution | DONE |
| K3 | Structured logging throughout (not print statements) | DONE |
| K4 | Decision-to-log latency instrumentation | DONE (per-tick elapsed ms logged) |
| K5 | Every rejection logged with its specific reason, same detail level as every acceptance | DONE (full rule breakdown on every feed event) |

## L. Platform security & hardening (10-rule defense-in-depth)

| # | Requirement | Status |
|---|---|---|
| L1 | Cookie-based session authentication: HttpOnly, SameSite=Lax, `Path=/`, 1-hour absolute lifetime (no rolling refresh) | DONE (`backend/api/auth.py`, `backend/api/routes/auth.py`, `test_cookie_auth.py`) |
| L2 | Access control & record ownership: authorization checks on all ID endpoints + Supabase RLS | DONE (`migrations/supabase/006_rls_policies.sql`, `test_authz_surface.py`) |
| L3 | Mass assignment defense: Pydantic extra-field stripping (`extra="ignore"`) on all write schemas | DONE (`test_mass_assignment.py`) |
| L4 | Server-side rate limiting: sliding window token bucket middleware across all route tiers | DONE (`backend/api/rate_limiter.py`, `test_rate_limiting.py`) |
| L5 | Bot protection: cryptographic challenge-response and 2.0s time-lock on authentication | DONE (`backend/api/bot_protection.py`, `test_bot_protection.py`) |
| L6 | SQL injection elimination: 100% parameterized queries in SQLite/Postgres + column whitelisting | DONE (`backend/api/db.py`, `backend/api/db_pg.py`, `test_db_surface_parity.py`) |
| L7 | Server-side input validation: null-byte checks, regex bounds, traversal guards, length clamps | DONE (`test_input_validation.py`) |
| L8 | Cross-Site Scripting (XSS) prevention: CSP headers, safe React JSX rendering, URL protocol sanitation | DONE (`backend/api/main.py`, `frontend/src/components/Journal.tsx`) |
| L9 | File upload & ingestion security: extension whitelist, binary magic & shebang checks, 0o600 storage | DONE (`backend/knowledge_base/loader.py`, `test_file_upload_security.py`) |
| L10 | Billing caps & alerts: documented cost limits and alert thresholds for LLMs, RPCs, and databases | DONE (`handoff.md`, operational runbook) |
| L11 | HTTPS everywhere & TLS enforcement: 308 redirect middleware, HSTS preload, CSP upgrade-insecure-requests, Uvicorn proxy headers | DONE (`backend/api/main.py`, `backend/tests/test_https_enforcement.py`) |
| L12 | Production deployment hardening: debug mode & Swagger disabled, sourcemaps turned off, .git & dotfiles blocked | DONE (`backend/api/main.py`, `backend/tests/test_production_hardening.py`) |
| L13 | Production error handling & secret redaction: generic 500 error messages with error_id, logging secret filter, 422 input stripping | DONE (`backend/api/error_handler.py`, `backend/tests/test_error_handling_security.py`) |
| L14 | Centralized logging & security monitoring: rotating file logs (app.log, error.log, security.log), secret scrubbing, security event ring buffer | DONE (`backend/api/logging_config.py`, `backend/api/security_monitor.py`, `test_logging_and_monitoring.py`) |

## M. Code quality, performance & container infrastructure (§75–§76)

| # | Requirement | Status |
|---|---|---|
| M1 | Dead code & unused module elimination: removed retired paper-era modules (`backend/llm/reuse.py`, `backend/tests/test_narration_reuse.py`), experimental spike (`scripts/scrapling_spike.py`), and cleaned 55 unused imports | DONE |
| M2 | WebSocket feed optimization: incremental delta query (`get_feed_events_since`) eliminating full 100-row deserialization loops | DONE (`backend/api/websocket.py`, `backend/api/db.py`, `backend/api/db_pg.py`) |
| M3 | Refusal funnel projection: lightweight verdict query (`get_feed_verdicts`) skipping heavy JSON text columns | DONE (`backend/api/routes/funnel.py`) |
| M4 | Jupiter pricing cache: short-TTL (2.0s) price caching in `JupiterProvider.get_current_price` reducing redundant RPC calls | DONE (`backend/data_providers/jupiter.py`, `backend/tests/test_jupiter_decimals.py`) |
| M5 | Single-book UI primitive unification: standardized `App.tsx` and `LiveBook.tsx` on shared `Panel` and `Empty` primitives | DONE (`frontend/src/App.tsx`, `frontend/src/components/LiveBook.tsx`) |
| M6 | Sizing logic consolidation: unified inline portfolio sizing math in `stats.py` to `sizing.compute_entry_cost` | DONE (`backend/api/routes/stats.py`) |
| M7 | Operational launcher parity: `start.sh` & `stop.sh` dynamic `API_PORT` resolution (default 8000), `0o750` logs directory enforcement, paper-era flag removal | DONE (`start.sh`, `stop.sh`) |
| M8 | Container entrypoint & Docker parity: audit A8 safety regex for `LIVE_TRADING_ENABLED`, unprivileged `0o750` `/app/logs` volume, diagnostic CLI tools in image, updated `.dockerignore` | DONE (`backend/docker-entrypoint.sh`, `Dockerfile`, `docker-compose.yml`, `test_docker_entrypoint.py`) |



