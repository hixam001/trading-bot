**Last updated:** 2026-08-27 · **Branch:** main · **Status:** LIVE
(real market data, simulated funds; Supabase Postgres persistence active) ·
**App:** http://localhost:8000
**Tests:** 289 passing (full suite incl. live_execution)

Read this top-to-bottom before touching anything. It contains everything a
new session needs: state, decisions, bugs fixed, invariants, and next steps.

---

## 1. What this project is

A local **paper-trading research system** for Solana memecoins. Every tick
(~60s) it fetches real candidates (Birdeye memepool trending), enriches them
(Dexscreener pairs), computes a market-wide regime snapshot, and evaluates
each candidate against **ten deterministic rules**. The AND of all rules is
the entire entry decision. Exits come only from three fixed numeric checks.
A local LLM (currently qwen3:8b via Ollama) performs a pre-trade **think/veto
  stage** and narrates decisions; entry still requires the model's buy verdict
  AND every deterministic rule to pass. The model never sizes, opens, closes,
  or overrides numeric exits. Everything is logged to SQLite
and visible in a React dashboard served by the backend itself.

**Non-negotiable:** the paper-trading pipeline (`backend/`) is paper only —
no wallet, no transaction construction anywhere there.
`PAPER_TRADING_ONLY = True` is hardcoded in `backend/config.py` and
runtime-asserted inside every position-opening function. The separate
`live_execution/` package at the repo ROOT (never imported by backend/) is
the only real-execution code; it ships DISARMED — hardcoded
`LIVE_TRADING_ENABLED = False`, mandatory manual confirmation, kill switch,
daily-loss breaker — and must never be armed without a human editing its
config.py and testing the full flow on Solana devnet first. If a task ever
seems to require real execution inside backend/ — stop and flag it.

## 2. How to run / stop / test

```bash
./start.sh        # one click: builds frontend, starts ollama serve IF not
                  # already up, starts backend+tick loop on :8000 (serves
                  # dashboard), opens browser. Idempotent.
./stop.sh         # stops backend; leaves pre-existing ollama alone
cd backend && ../.venv/bin/python -m pytest tests/ -q   # backend-only: 192 tests
.venv/bin/python -m pytest -q                           # full suite: 222 tests, ~1.5s
```

- Dashboard/API: http://localhost:8000 (single origin; backend serves the
  built frontend from `frontend/dist`)
- Logs: `logs/{backend,ollama,frontend-build}.log`; pids in `.run/`
- Dev frontend hot-reload: `cd frontend && npm run dev` (:5173, proxies /api,/ws)
- Knowledge ingest: `cd backend && ../.venv/bin/python scripts/ingest_directory.py <dir>`

## 3. File map (what lives where)

| Path | Purpose |
|---|---|
| `backend/config.py` | ALL thresholds + safety flag + provider keys via env |
| `backend/models.py` | Candidate (incl. `decimals`), Trade, FeedEvent, RuleResult, GateDecision, PortfolioState |
| `backend/rule_engine/` | `rules.py` (11 omo-parity entry rules), `exits.py` (omo exit engine: stop/trail/liquidity-break/invalidation/stale/TP-ladder + sell risk gate), `gate.py` (no-short-circuit AND), `regime.py`, `liveness.py` (not_on_break) |
| `backend/data_providers/crowd.py` | fomo.fun board reader (Privy session, auto-renewing rotated refresh tokens) → feeds crowd_heat. Stealth fallback chain: firecrawl (forwards auth headers) → scrapingbee (keyless-only: platform consumes Authorization) → zenrows `custom_headers=true&premium_proxy=true` → scrapeops `keep_headers=true` — the last two carry the Privy bearer through Cloudflare (verified live). `_json_from_body` rejects only statusCode≥400 envelopes (prod-api sends statusCode:200 on success) |
| `backend/paper_trading_engine.py` | money math + atomic open/close/scale_in + exits + decide_and_act |
| `backend/api/db.py` | schema + repository (SQLite default); when `USE_SUPABASE_DB=1` + `SUPABASE_DB_URL` set, transparently delegates every public function to `db_pg.py`; pytest forces SQLite |
| `backend/api/db_pg.py` | asyncpg/Supabase Postgres twin of db.py — identical surface incl. §5.1 atomicity (rowcount from execute status); TLS via SHA-256 cert-fingerprint pinning (TOFU, `.supabase_fp.txt` gitignored); `init_db()` self-heals schema drift via `_SCHEMA_SYNC_SQL` (§17) |
| `migrations/supabase/001_init.sql` | run ONCE in Supabase SQL editor: 12 tables, JSONB/TIMESTAMPTZ, one-open-position exclusion constraint, RLS locked. `002_llm_usage.sql` adds `llm_call_usage` + versioning columns; `db_pg.init_db()` auto-heals older books so neither has to be re-run manually (§17) |
| `backend/api/main.py` | FastAPI app; serves built frontend; TICK_LOOP_IN_PROCESS env runs tick loop in-process |
| `backend/data_providers/` | base(protocol,retry,counters), birdeye, dexscreener, jupiter, live(stack), mock |
| `backend/llm/` | narrator.py (prompt, Ollama client, template fallback, reflection), grounding.py |
| `backend/knowledge_base/loader.py` | static KB, digest-at-ingest, budgeted get_context |
| `backend/main.py` | run_tick(): regime once/tick → per-candidate gate+narrate → exit checks |
| `backend/promotion_gate.py` | READ-ONLY 5-criteria readiness report. Never writes. Ever. |
| `live_execution/` | REAL-MONEY execution package at repo ROOT (never imported by backend/). Fully wired: `run_live_cycle.py` manages live positions, runs the shared read/think/gate stages, and routes buys/sells through `place_order`; Jupiter quote/swap, local signing, rotating RPC broadcast, confirmation, commit binding, and ledger journaling are connected. Ships DISARMED: hardcoded `LIVE_TRADING_ENABLED=False`, `REQUIRE_MANUAL_CONFIRMATION=True`, kill switch + daily-loss breaker, fail-closed confirmation expiry, idempotency ledger, caps, wallet identity checks, and decimals guards. Operator CLI: `python -m live_execution.scripts.confirm_trade list|approve|deny|kill|resume`. Offline/mock wiring tests pass; a funded throwaway-keypair devnet drill is still REQUIRED before any mainnet use. |
| `frontend/src/` | dashboard panels (feed WS, holdings, journal, stats [equity/spend/realized/unrealized/cash], regime, gate, status); no knowledge tab, no paper-trading banner (removed 2026-08-25) |
| `backend/api/routes/proof.py` | OMO-R1 binding report (`/api/binding.json`), `/api/verify.json`, `/api/refusals.json`, `/api/theses.json`, `/api/proof.json`, `/api/exits.json` |
| `backend/api/routes/disclosure.py` | OMO-R6 public machine-truth feeds: `/api/disclosure.json` (armed/break/config state) + `/api/reasoning.json` (per-decision provenance) |
| `backend/retro_matcher.py` | OMO-R7 retro audit-log signature matching: attributes out-of-pipeline fills to decision commit rows using omo's exact algorithm (symbol+side match, 12h window, earliest fill wins, taken set) |
| `docs/00..07` | blueprint, architecture, feature list (+status), gantt, verification appendix, omotrades comparison, project report |
| `.env` (root, gitignored) | operator settings ONLY: keys + DATA_BACKEND=live |


## 4. The ten entry rules (thresholds are placeholders until calibrated)

Evaluated unconditionally on every candidate — no short-circuiting; every
rejection logs its complete profile. Full details: `docs/07_PROJECT_REPORT.md` §3.

| rule_id | logic | threshold | None behavior |
|---|---|---|---|
| liquidity_floor | liquidity ≥ floor | $10,000 | missing → FAIL closed |
| volume_alive | 1h volume ≥ min | $5,000 | missing → FAIL closed |
| buy_pressure | buys > sells (1h tx counts, strict) | tie fails | either missing → FAIL |
| not_newborn_fade | NOT(age<2h AND chg1h≤−30%) joint | 2h / −30% | either missing → FAIL |
| public_presence | any of twitter/telegram/site confirmed | any 1 passes | unknown ≠ absent; all-unknown fails |
| market_regime_ok | tick regime OK (shared, computed once/tick) | green 15–85% AND median vol ≥ $20k | empty batch → BAD |
| cash_available | cash ≥ intended size | $100 | n/a |
| exposure_cap | held in mint < cap (entries AND scale-ins) | $150 | n/a |
| security_clear | fails only on KNOWN-bad (authority live, honeypot) | — | **None always PASSES** |
| volume_mcap_ratio_ok | v24h/mcap ≥ ratio | 0.80 | inputs missing → PASS neutral |

Exit conditions (checked per open position each tick, in order):
take-profit ≥ +50% → stop-loss ≤ −20% → timeout ≥ 72h (net of 2% slippage +
1% fee).


## 5. Decision log (why things are the way they are)

1. **Ground-up rebuild** — old `backend/+frontend/` implementation was
   deleted (user action); rebuilt to revised docs spec: rule engine,
   GateDecision, regime gate, narration-only LLM. Old code remains in git
   history only.
2. **Layout** — everything Python lives in `backend/` (flat modules +
   packages); `frontend/` separate. Run commands from inside `backend/`.
3. **Schemas added**: `market_regime` (append-only, once/tick,
   candidate_count column) and `provider_call_counters` (per provider/day
   with separate 429 counter). Partial UNIQUE index on
   `trades(mint_address) WHERE is_open=1`.
4. **Groundedness validation**: rule-derived synonym map
   (llm/grounding.py RULE_GROUNDING_TERMS), invented-rule-id check, numeric
   echo check (trailing punctuation stripped). Flags RECORDED on feed
   events, never dropped/rewritten.
5. **Atomicity (§5.1)**: conditional writes return rowcount; rowcount is
   the sole authority for touching cash; scale-in exposure cap enforced
   inside the UPDATE WHERE clause; unique index backstop.
6. **Birdeye discovery**: `/defi/tokenlist` returned majors → switched to
   `/defi/token_trending?listing=memepool`. Field names confirmed against
   real payloads (`volume24hUSD`, `marketcap`, `decimals`).
7. **Jupiter endpoint migrated**: quote-api.jup.ag/v6 is dead (DNS gone);
   now `lite-api.jup.ag/swap/v1/quote`. Pro key rides as x-api-key when set.
8. **DECIMALS BUG (critical, fixed)**: Jupiter was quoted with amount=10⁹
   assuming 9 decimals for every mint. BARRON has 6 decimals → quoted 1000
   tokens as "one" → price inflated exactly 1000× ($0.00054 → $0.539) →
   take-profit booked five fake ~+96,407% closes, cash hit $481,783. Fix:
   Candidate.decimals from Birdeye threaded through protocol → exit loop &
   holdings pass decimals from trade snapshot; Jupiter refuses to quote
   without decimals (fail closed). Regression tests pin it
   (tests/test_jupiter_decimals.py). Corrupted DB wiped; fresh book at $1,000.
9. **token_security on free tier**: 401 (tier lacks it). 401/403 are
   non-retryable ProviderAuthError; BirdeyeProvider disables security
   enrichment for the session after first 401 (semaphore(2) bounds burst).
   Security fields remain UNKNOWN → security_clear passes them (never
   coerced to False).
10. **omotrades reference check** (docs/06): their revealed payloads show
    verdict:"pass" WITH failed rules + side:null + refusal note → an agent
    layer decides between rules and action. Our architecture closes that
    gap deliberately. crowd_heat (fomo index) has no equivalent here;
    already_held is binary vs our scale-in cap (intentional). Commit-reveal
    mechanism verified byte-for-byte locally
    (scripts/verify_reference_commit.py, both hashes MATCH), documented in
    docs/05 — deliberately NOT built.
11. **One-click app**: start.sh/stop.sh/trading-bot.desktop; backend serves
    built frontend (SPA catch-all registered AFTER api routes); ollama
    started only if not already up; stop.sh never kills pre-existing ollama.
12. **Test hermeticity**: tests pin DATA_BACKEND=mock and tmp DB paths so
    operator .env (live) can't leak into them (bit us twice).
13. **Supabase Postgres backend (optional)**: db_pg.py mirrors db.py's whole
    surface over asyncpg; selected when USE_SUPABASE_DB=1 + SUPABASE_DB_URL.
    pytest guard forces SQLite so the suite can never touch the live remote
    book. Live smoke passed every atomicity check against real Supabase;
    uvicorn boot verified serving PG data on all API endpoints.
14. **Supabase TLS**: pooler serves a self-signed chain → system-CA verify
    impossible. Implemented SHA-256 cert-fingerprint pinning (TOFU; pin file
    gitignored; mismatch = hard abort with re-pin instructions).
15. **Stealth-chain header forwarding**: scrapeops keep_headers=true and
    zenrows custom_headers=true(+premium_proxy) carry the Privy bearer
    through Cloudflare — verified live returning REAL board data.
    ScrapingBee cannot (its platform consumes Authorization as its own key).
16. **_json_from_body statusCode bug**: prod-api includes statusCode:200 in
    SUCCESS envelopes; the old any-statusCode rejection silently discarded
    valid board data on the whole scrape path. Now rejects only ≥400.
17. **NO raw SQL outside api/db*.py (enforced rule)**: the PG cutover
    exposed raw SQLite SQL in journal.py / proof.py / main.py /
    paper_trading_engine.py → Supabase 42883 `boolean = integer` on every
    dashboard poll. All moved into repository functions implemented in BOTH
    backends (count_closed_trades, get_recent_decision_commits,
    get_recent_fills, get_open_position_marks, get_verify_commits,
    set_trade_thesis, delete_trade_row). Stale default_ledger import
    (500 on /api/exits.json) removed.
18. **Process management**: backend + ollama launch via `setsid` (own
    session — Konsole Ctrl+C / tab-close cannot kill them; nohup alone only
    blocks SIGHUP); `timeout 10 ollama list` guards the launch path. API
    keys in URLs are redacted from logs by a crowd.py log filter (installed
    at import — setup_logging() never runs under uvicorn).
19. **Dashboard overhaul (2026-08-25, user-requested)**: feed rows read
    ENTER/PASS (was ENTER/REJECT); `[model veto]` prefix REMOVED from
    main.py — thesis stored/shown verbatim (also fixed double-appended
    "invalidates if" sentence); full un-truncated model answer + token
    contract address (click-to-copy) in expanded feed view; explicit amber
    "model chose not to enter:" block when all rules pass but the model
    declines. /api/stats gained realized_pnl_usd / unrealized_pnl_usd
    (live marks net of exit costs, None when no prices) / total_spend_usd
    (open cost basis incl. fee+slippage); UI shows ONLY those five numbers
    (equity curve chart removed from UI, field kept in API for compat).
    Knowledge tab + PaperTradingBanner component deleted (KB no longer
    feeds any prompt — verified thinker.py never imports it; backend KB
    endpoint untouched). PAPER_TRADING_ONLY safety machinery untouched.


## 6. Bugs found & fixed (all regression-tested)

- tokenlist → majors not memecoins (fixed: memepool trending)
- Jupiter v6 URL dead (fixed: lite-api swap/v1/quote)
- 9-decimals assumption → 1000× price fabrication on 6-decimal mints
  (fixed: decimals-aware quoting + fail-closed guard)
- token_security 401 retry storms stalling ticks (fixed: non-retryable auth
  errors + session disable + semaphore(2))
- qwen3 thinking mode → multi-minute narrations (fixed: think=false +
  /no_think + strip `<think>` blocks)
- numeric echo false positive on "0.80." trailing period (fixed: strip
  sentence punctuation from number tokens)
- tests leaked ingested files into real KB dir / depended on operator .env
  (fixed: monkeypatched paths + DATA_BACKEND)
- raw SQLite SQL in journal.py / proof.py / main.py / paper_trading_engine.py
  → Supabase 42883 boolean=integer on every dashboard poll (fixed: all moved
  into repository functions in both db backends)
- stale default_ledger import → 500 on /api/exits.json (fixed: removed)
- ZenRows/ScrapeOps/ScrapingBee API keys logged in plaintext via httpx URL
  logging (fixed: _ApiKeyRedactor filter in crowd.py; old logs scrubbed)

## 7. Known limitations (accepted, documented)

- Birdeye free tier: no token_security → security fields always UNKNOWN
  right now (upgrade tier → auto re-enables on restart).
- ScrapingBee stealth fallback is keyless-only (platform consumes the
  Authorization header); zenrows premium tier costs ~10–25 credits/request.
- Supabase backend: pooler cert is self-signed → fingerprint-pinned; if
  Supabase rotates certs legitimately, delete `backend/.supabase_fp.txt`
  to re-pin (backend hard-aborts until then). Old SQLite book not migrated
  to Supabase — fresh book started there.
- Regime thresholds are placeholders pending calibration data.
- LLM thinker/narrations make ticks take ~40–90s for 20 candidates; fine at the
  60s interval, reduce MAX_CANDIDATES_PER_TICK if needed.
- Post-calibration scope, deliberately unbuilt: Full multi-wallet management, auto-adjustment of thresholds based on LLM feedback (docs/08), durable OMO memory/events roadmap (OMO-R5), and the remaining approved OMO roadmap items in section 13.

### OMO-R5 implementation (2026-08-26)

- Added mirrored `events` and `memories` persistence to SQLite and Supabase;
  events accept only `thought|did|refused|read|trade` and memory weights must
  be positive.
- Added repository functions for append-only event writes, recent event reads,
  weighted memory upsert, and recall. Recall increments `hits` for every
  returned lesson and is bounded by topic and limit.
- `main.run_tick()` now records read, thought, did/refused, and successful
  paper-trade events. Recalled lessons are injected into the thinker as
  context only; deterministic rules and paper execution remain authoritative.
- Added read-only `/api/events.json` and regression tests covering validation,
  persistence, hit accounting, prompt context, and mock-tick stage events.

## 8. Next steps

1. **Calibration window (10 days)** is underway: let it run; review daily
   via dashboard + learning-loop logs; tune ONE threshold at a time from
   rejection-breakdown evidence (manual edits to config.py).
2. Watch first entries/exits; verify realized P&L sanity on close.
3. Optional upgrades: Birdeye tier with token_security; fomo-index source
   for a crowd_heat-style rule (docs/06 §3.2).
4. After calibration: complete approved OMO roadmap work in section 13,
  starting with OMO-R5 memory/events.
5. **LLM API migration:** implemented in section 14.
  docs/08_LLM_API_MIGRATION_AND_FEEDBACK_PLAN.md. Target Groq for the thinker (qwen3.8-27b), Groq for evidence-only social reads, and
  measured usage/outcome instrumentation before switching models.
   Next stage: main/narration model → DeepSeek V4 Flash (social stays Groq)
   — full plan in section 18, gated on a funded DeepSeek key + shadow replay.
6. No automatic learning, threshold changes, prompt changes, model changes,
  or live-trading promotion is permitted.

## 9. Invariants checklist (before any change)

- [ ] No real-execution path added; PAPER_TRADING_ONLY untouched/hardcoded
- [ ] promotion_gate.py still read-only, no promote/activate functions
- [ ] Rules stay pure; no short-circuiting; None semantics preserved
- [ ] Money-touching changes keep atomic write→rowcount→cash ordering
- [ ] External fields validated via require_type; None never coerced
- [ ] Tests hermetic (tmp DBs, mock narration) and passing (145)
- [ ] LLM provider failure returns thinker `pass` for entry; templates explain only
- [ ] db_pg.py kept surface-identical to db.py if repository changes land
- [ ] NO raw SQL outside api/db*.py — every query is a repository function
- [ ] New rule ⇒ add vocab in llm/grounding.py + both-branch tests




## 10. omo-parity batch (2026-08-25)

- **Candidate breadth**: chg5m/6h/24h, fdv, buys/sells/vol 6h, pool_count,
  total_liquidity_usd, top_pool_share, boosted (models.py + dexscreener.py).
- **research.py** (new): omo researchToken port - cross-pool aggregates,
  wired into main.py read stage (live-only, RESEARCH_PER_TICK cap).
- **discovery.py rebuilt**: slot-composed board (flow core + newborn slots +
  mover slots + 5 guaranteed rotation slots, cap 16) + boost feeds -> boosted flag.
- **Refusals public**: get_refusal_events() in db.py AND db_pg.py;
  GET /api/refusals.json; refusals now inside /api/proof.json.
- **live_execution omo engine**: solana.py (multi-RPC failover, send,
  confirm-before-journal, getTokenSupply decimals), executor.py
  (place_order buy+sell with unarmed/blocked/failed/filled statuses, price-
  impact floor 2.5%%, SOL reserve, daily deploy cap $300, idempotent buys,
  pro-rata ledger.reduce_position for TP trims), wallet address verification.
- **run_live_cycle.py** (ROOT): autonomous manage->read->think->gate->execute
  bridge; backend never imports live_execution (isolation intact).
- **LLM**: OLLAMA_NUM_PREDICT knob (512 default) wired into thinker+narrator.
- Tests: 202 passing (7 new: executor guards, ledger sell math, research
  aggregation, discovery composition). Live UNARMED cycle smoke-tested.
- **Social read stage (rigid provider system)**: llm/social.py - generic
  OpenAI-compatible client (Groq/Grok/OpenRouter all speak the same protocol).
  Switching provider = 3 env values, zero code. Evidence-only output
  (interest: organic/peaked/unclear + one grounded note), never a verdict;
  disabled when SOCIAL_LLM_API_KEY empty; fail-soft like every feed.
  Wired into main.py + run_live_cycle.py read stage; injected into the
  thinker prompt as the {social_line} evidence line. Tests: 206 passing.

## 11. P0 batch (2026-08-25, session 2)

- **Birdeye now OPTIONAL**: live stack starts without BIRDEYE_API_KEY;
  trending lens skips, discovery = keyword rotation + new listings.
- **onchain_security.py** (new): free Solana RPC authority checks
  (getAccountInfo jsonParsed) fill mint/freeze revocation when Birdeye
  absent or silent; security_clear works keyless. ONCHAIN_RPC_URLS config.
- **Sell sealing (paper)**: scan_and_execute_exits writes a
  decision_commits row (verdict=sell, payload=trade_id/rule/fraction/price)
  BEFORE close_position / trim_position executes.
- **Live commit log**: live_execution/commit_log.py CommitLog — seal intent
  sha256(nonce|payload) before broadcast, bind signature on confirm;
  wired into place_buy + place_sell via _broadcast_and_confirm.
- **Devnet drill**: run_live_cycle.py --drill runs wallet→balance→decimals→
  blockhash→sign→send→confirm self-transfer of dust on devnet only.
- Tests: 208 passing (commit-log roundtrip, authority parser, social stage).

## 12. Session 3 additions (2026-08-25, later)

- **Web-read stage**: llm/web_research.py - Firecrawl search API evidence
  (last 24h) condensed into thinker prompt via {web_line}. Uses existing
  FIRECRAWL_API_KEY; disabled when key empty. Wired into main.py and
  run_live_cycle.py read stages.
- **Rigid social provider system**: llm/social.py - provider-agnostic
  OpenAI-compatible client (Groq/Grok/OpenRouter/Cerebras all speak the same
  /chat/completions protocol). Switching provider = SOCIAL_LLM_BASE_URL + _KEY
  + _MODEL env change, zero code change. Evidence-only output (interest:
  organic/peaked/unclear + one grounded note), never a verdict.
- **Gate is now exactly omotrades 9 rules** (market_regime_ok and
  security_clear retired from ACTIVE_RULES; functions kept for re-enable).
- **Onchain security**: live stack fills mint/freeze authority flags from
  free Solana RPC (onchain_security.py) when Birdeye absent/silent.
- **Discovery slot guarantees**: newborn/movers/rotation slots + boost feeds.
- **Refusals public**: /api/refusals.json + refusals in /api/proof.json.
- **Live execution complete (DISARMED)**: solana.py multi-RPC + confirm,
  commit_log.py, executor.py place_order buy+sell (unarmed/blocked/failed/
  filled statuses), wallet verify_expected_address, ledger reduce_position,
  run_live_cycle.py --drill devnet self-transfer drill.
- **Tests: 212 passing** (backend 158 + live_execution 54).

### Live execution wiring verification (2026-08-26)

- Confirmed the root-only live bridge is connected end to end:
  `run_live_cycle.py` → shared provider/read/Groq think stages →
  deterministic rules → `live_execution.executor.place_order` → Jupiter
  quote/swap → local wallet signing → rotating Solana RPC broadcast → on-chain
  confirmation → commit-log binding → execution ledger.
- Manage exits use live ledger positions, chain decimals, Jupiter pricing, the
  shared exit rules, and `place_order(side="sell")`; paper backend never
  imports `live_execution`.
- Fixed Solana endpoint selection used by the devnet drill and ensured the
  `place_buy` wrapper performs complete offline preflight before any network
  call. Live-execution focused tests: 45 passing; full suite: 215 passing.
- Real execution remains OFF: `LIVE_TRADING_ENABLED=False` is hardcoded and
  cannot be set through `.env`; manual confirmation remains enabled. No
  mainnet execution has been authorized or claimed as network-verified.

## 13. Approved omo-parity roadmap (2026-08-26) — TO BE IMPLEMENTED

Decision record from the source-level comparison vs omotrades/omo (full detail
in docs/P0_REPORT.md §6). Seven features APPROVED, one REJECTED, one DEFERRED.
Every approved item carries a stable ID (**OMO-R#**) — use it in branch names,
commit messages and test files so later implementation is traceable. Order of
implementation: R5 first (marked important), then R4, R3, R2, R1, R6, R7.

### OMO-R5 — Memory/events system ✅ IMPLEMENTED (2026-08-26)
- omo reference: `OmoEvent`/`OmoMemory` types + hydrate logic in
  `src/lib/omo-brain.server.ts`.
- Persistent event log with kinds `thought|did|refused|read|trade`, plus
  weighted memories (topic, note, weight, hits) recalled into the thinker
  prompt so accumulated lessons influence future decisions; `hits` increments
  on recall.
- Touch points: `events` + `memories` tables in BOTH api/db.py and api/db_pg.py
  (surfaces stay identical); writer hooks in every tick stage; recall injected
  as `{memory_line}` in the thinker prompt (same pattern as social/web lines);
  expose recent events via existing feed routes.
- Operator flagged this IMPORTANT: do this one before all others.

### OMO-R4 — Self-regulating break system ✅ IMPLEMENTED (2026-08-26)
- omo reference: `not_on_break` gate rule + `breakUntil`/`breakReason` state in
  omo-brain.server.ts.
- The loop may pause itself for a stated reason, persisted until a timestamp;
  while broken, the existing `not_on_break` gate rule fails CLOSED and the
  refusal records "on break" loudly. Resume only by expiry or explicit
  operator clear.
- Touch points: state file beside kill_switch.json (REUSE kill_switch.py's
  fail-safe semantics: corrupt state = refuse, never assume fine); wire the
  real state into rule_engine/gate.py `not_on_break`; honor it in main.py and
  run_live_cycle.py tick loops.
- Note: `not_on_break` is already one of the 9 ACTIVE_RULES with a hardcoded
  "not on break" input — this adds the actual break state behind it.

### OMO-R3 — Durable thesis book ✅ IMPLEMENTED (2026-08-26)
- omo reference: `src/lib/theses.server.ts` + public `/api/public/theses.json`.
- Per-position write-up as database state: created at entry (required author =
  `operator` | `model` plus model id when model), revised while held, retired
  at close with realized PnL filed against the row. Live size/pnl always
  refreshed from the book/chain, NEVER trusted from the row. Stored text wins
  over any late/cached feed read (omo invariant).
- Touch points: `theses` table in db.py + db_pg.py; write hook after a live
  buy confirms AND after paper opens (paper rows carry their own thesis);
  retire hook in both exit paths; read-only `GET /api/theses.json`.

### OMO-R2 — FOMO crowd intel upgrade: theses WITH author P&L ✅ IMPLEMENTED (2026-08-26)
- omo reference: `src/lib/fomo.server.ts` (`readFomoIntel`,
  `describeFomoIntel`, `readOwnBasis`).
- Current state: data_providers/crowd.py already reads thesis COUNTS off
  prod-api.fomo.family for crowd_heat (Privy session + Firecrawl stealth
  fallback, queue-gap + response-TTL plumbing done).
- Upgrade scope: return FULL thesis rows — text, handle, author position
  sizeUsd, unrealizedUsd, realizedUsd, closed — and render omo-style evidence
  lines ("@who on $SYM — holding $X, up $Y (Z%): \"text\"") into the thinker
  prompt as a new `{crowd_line}`; prompt instructs the model to weigh each
  claim by whether its author is actually up on the position.
- Extend crowd.py — do NOT duplicate its session/proxy plumbing. Fail-soft as
  today: no feed = empty line, loop continues. Env unchanged:
  FOMO_PRIVY_REFRESH_TOKEN + FIRECRAWL_API_KEY.

### OMO-R1 — Independent verifier upgrade + binding report ✅ APPROVED
- omo reference: `src/lib/verify.server.ts` + `readBinding()` in
  precommit.server.ts.
- Scope NOTE: on-chain memo sealing was REJECTED (see below), so verification
  cannot mirror omo's memo-hash checks. What IS verifiable without memos:
  extend /api/verify.json so each decision-commit row bound to a fill is
  checked against PUBLIC RPC instead of our own journal:
    1. fetch the fill tx via getTransaction on the bound signature
       (needs a get_transaction helper in live_execution/solana.py);
    2. checks per row: tx exists and confirmed; time ordering (commit
       sealed_at < fill blockTime); account key 0 == our wallet address;
       pre/postTokenBalances include the committed mint;
    3. new read-only `GET /api/binding.json` in api/routes/proof.py:
       pairs committed mint vs mint actually touched, with matched /
       mismatched counts (omo BindingReport shape).
- Touch points: live_execution/solana.py (get_transaction), api/routes/proof.py
  (extend verify + add binding), commit rows already carry payload/nonce/
  signature. Tests: mock RPC fixtures; every mismatch case must report the
  FAILED check explicitly — a check that cannot run reports unknown, never
  pass.

### OMO-R6 — Public machine-truth feeds (disclosure + reasoning) ✅ APPROVED
- omo reference: `src/lib/disclosure.server.ts`; `/api/public/disclosure.json`
  and `/api/public/reasoning.json`.
- Minimum scope: two more read-only JSON endpoints alongside proof/exits/
  verify/refusals:
    - `/api/disclosure.json` — live machine state: armed/disarmed flag,
      kill-switch state, break state (OMO-R4), last cycle timestamp + step
      results, config truths (caps, floors), no secrets;
    - `/api/reasoning.json` — per-decision provenance: which model produced
      the thesis, stage timings, inputs snapshot hash (sha256 of the gated
      inputs), linked commit hash.
- Full web UI terminal is explicitly OUT OF SCOPE for now; JSON first, a UI
  can be layered later on the same endpoints.

### OMO-R7 — Audit-log signature matching (retro attribution) ✅ APPROVED
- omo reference: `linkAuditToFills()` in `src/lib/audit.server.ts`.
- Purpose: attribute fills to decision rows when a fill BYPASSES the pipeline
  (e.g. a hand-placed trade against the live wallet once armed). Exact
  bind-at-execute (CommitLog.bind) stays the PRIMARY binding and is never
  overwritten by this layer — retro matching only ever touches rows whose
  signature is still null.
- Algorithm (omo's, kept intact):
    1. pending = decision rows with verdict=act AND signature IS NULL
       (newest 60);
    2. candidates = recent fills (newest 120) whose signature is not already
      claimed by another row;
    3. match on: same symbol (case-insensitive, $ stripped) + same side +
       fill_at >= decision_at + within a 12h window;
    4. earliest unmatched fill wins; a `taken` signatures set grows during the
       run so nothing is claimed twice;
    5. write back signature, matched_at, phase=filled.
- Safeguards (beyond omo): an "unattributed fills" listing must be visible in
  /api/proof.json rather than silently heuristic-matching every orphan; each
  matched row keeps a `matched_by: retro` marker so exact vs retro bindings are
  distinguishable in the public surface.
- Touch points: query helpers in api/db.py + api/db_pg.py (identical surfaces),
  matcher module callable from run_live_cycle.py post-cycle step and main.py,
  surfaced via api/routes/proof.py.
- Tests: double-claim prevention (3 decisions / 2 fills), window edge cases,
  side/symbol mismatch rejection, exact-bind precedence over retro.

### REJECTED / DEFERRED — do not re-litigate without operator approval
- ❌ **On-chain memo commitments + reveal protocol** (omo precommit memo layer)
  — REJECTED 2026-08-26. Local CommitLog seal-before-broadcast stands as the
  sealing mechanism; OMO-R1 is scoped to work WITHOUT on-chain memos.
- ⏸ **Off-book multi-chain tracking** (BNB/other-chain positions marked and
  repriced into equity) — DEFERRED temporarily. Revisit only after the Solana
  side is armed and has a live track record.
- ℹ️ Armed trading history needs no build work: it accrues automatically once
  live cycles run armed.
- 📝 Audit-log retro signature linking was initially REJECTED (2026-08-26,
  rationale: exact bind-at-execute leaves no bypass channel while disarmed),
  then APPROVED as OMO-R7 in the same session once the operator reviewed the
  mechanism — it matters only when armed, and only for out-of-pipeline fills.

## 14. LLM API migration and feedback plan (2026-08-26) — IMPLEMENTED

Full plan: docs/08_LLM_API_MIGRATION_AND_FEEDBACK_PLAN.md.

**Status update (2026-08-27):** the MAIN LLM path (Thinker, Narrator,
post-close reflections) now runs on **DeepSeek V4 Flash** (direct API,
non-thinking mode) — see §18/§19. Groq (`qwen/qwen3.8-27b`) remains the
warm rollback main provider via `MAIN_LLM_PROVIDER` and still powers the
evidence-only social reads. All provider instrumentation (token counts,
latency, estimated cost, peak window flag, degradation reason) is wired and
persisted to `llm_call_usage`. Shadow replay and delayed outcome label
scripts are in `backend/scripts/`.

### Current API/model decisions

- **Thinker:** `build_main_client()` factory → `DeepSeekClient`
  (`deepseek-v4-flash`, non-thinking mode, JSON output) when
  `MAIN_LLM_PROVIDER=deepseek`, else `MainGroqClient`
  (`qwen/qwen3.8-27b`). Unrecognized values fail closed to Groq.
  Fail-closed: any failure degrades to a deterministic template verdict of
  `pass` — the template may explain but never approve an entry while the
  provider is unavailable.
- **Narrator/Reflections:** same main client. Fire-and-forget for
  reflections; never blocks the tick loop or exits. Reflections skip to the
  template during DeepSeek peak windows (logged, never silent).
- **Social reads:** `GroqClient` (`SOCIAL_LLM_BASE_URL` / `SOCIAL_LLM_MODEL`).
  Evidence-only (`organic|peaked|unclear`); no verdict produced.
- **Thinker fallback:** deterministic template on timeout, outage, quota
  exhaustion, invalid JSON, or provider error.

### Planned next step

- ~~DeepSeek migration (pending keys with balance)~~ **DONE (2026-08-27) —
  see §18/§19.** Funded key arrived; swap implemented, shadow-replay-gated,
  flipped live to `MAIN_LLM_PROVIDER=deepseek`.

### Cost and feedback controls

- Target thinker budget: 300–600 input tokens and 60–140 output tokens,
  maximum 192 output tokens, one request per candidate at most.
- Groq `qwen/qwen3.8-27b` approximate pricing: ~$0.80/M input,
  ~$4.00/M output. Cost is logged per call in `llm_call_usage`.
- Current learning is measurement-only: daily P&L, win rate, profit factor,
  drawdown, rejection breakdowns, and post-close reflections. No automatic
  threshold, prompt, model, or live-trading promotion is permitted.

## 15. Final implementation queue (2026-08-26)

This is the last section in the handoff and is the authoritative short list of
what comes next. Full requirements and OMO references remain in sections 13
and 14 above.

### Approved — implement in this order

1. **OMO-R5 Memory/events system** — ✅ IMPLEMENTED.
2. **OMO-R4 Self-regulating break system** — ✅ IMPLEMENTED. *(Bug fixed 2026-08-26: `liveness.set_break(think.break_minutes, think.break_reason)` was passing int/str in wrong positional slots; fixed to `set_break(True, think.break_minutes, think.break_reason)`.)*
3. **OMO-R3 Durable thesis book.** — ✅ IMPLEMENTED.
4. **OMO-R2 FOMO intel with author P&L.** — ✅ IMPLEMENTED.
5. **OMO-R1 Independent verifier and binding report.** — ✅ IMPLEMENTED. Four-check binding verification (`tx_confirmed`, `time_ordering`, `fee_payer`, `mint_present`) in `/api/binding.json`. Fail-closed: missing RPC data → `unknown`, never `pass`. New `signature/phase/matched_by` columns on `decision_commits`.
6. **OMO-R6 Public disclosure and reasoning feeds.** — ✅ IMPLEMENTED. `/api/disclosure.json` (machine state, no secrets) + `/api/reasoning.json` (per-decision provenance: model source, inputs hash, commit hash).
7. **OMO-R7 Retro audit-log signature matching.** — ✅ IMPLEMENTED. `retro_matcher.py` runs post-cycle in both `main.py` and `run_live_cycle.py`. Exact-bind rows (`signature IS NOT NULL`) never overwritten. Double-claim prevented by `taken` set.
8. **LLM migration completion** — Groq for thesis/thinker and reflections;
   Groq for social evidence; usage accounting, shadow replay, paper canary,
   and delayed outcome labels before any model promotion. DeepSeek migration is
   planned next once API keys with sufficient balance are available.

### Rejected or deferred — do not implement without approval

- **Rejected:** on-chain memo commitments and reveal protocol. Local
  CommitLog sealing remains the chosen mechanism.
- **Deferred:** off-book multi-chain tracking until Solana has a live track
  record.
- **Not yet validated:** mainnet live execution. The live execution package is
  wired but remains disarmed; a funded throwaway-keypair devnet drill is
  required before any future arming discussion.

### Standing safety conditions

- `PAPER_TRADING_ONLY=True` and `LIVE_TRADING_ENABLED=False` remain hardcoded.
- Deterministic rules, exits, cash guards, kill switches, and manual approval
  retain authority over every execution path.
- No automatic threshold, prompt, model, or live-trading promotion changes.

## 16. DB maintenance (2026-08-27)

### Feed and regime pruning

- **`prune_feed_events(conn, keep_rows)`** — deletes all but the newest
  `keep_rows` rows from `feed_events`. Returns count deleted. Config:
  `FEED_PRUNE_KEEP` (default 2000, overridable via env).
- **`prune_market_regime(conn, keep_rows)`** — same for `market_regime`.
  Config: `REGIME_PRUNE_KEEP` (default 500).
- Both functions exist in **both** `api/db.py` (SQLite) and `api/db_pg.py`
  (Postgres) with identical surfaces. The Postgres version uses a
  `DELETE ... NOT IN (SELECT id ... LIMIT $1)` subquery.

### Full book reset

- **`reset_book(conn, initial_cash_usd)`** — wipes all nine operational
  tables (`feed_events`, `market_regime`, `decision_commits`, `events`,
  `memories`, `theses`, `daily_stats`, `llm_call_usage`, `trades`) in
  order and resets `portfolio_state.cash_usd` to the given amount.
  Returns a summary dict with per-table row counts.
  **Paper-only** — never touches wallet, live_execution, or on-chain state.
  Postgres version uses `TRUNCATE TABLE ... RESTART IDENTITY CASCADE`.

### Admin endpoint

- **`POST /api/admin/reset`** (not in schema; operator console only)
  - `?confirm=yes` — required; returns 400 without it
  - `?mode=reset_book` (default) — full wipe + $1,000 restore
  - `?mode=prune_only` — trim feed/regime to configured limits, trades/cash
    untouched
  - Logged at WARNING level; returns JSON summary
  - Source: `api/routes/admin.py`, registered via `api/main.py`

### OMO-R1–R7 audit (2026-08-27)

All seven OMO parity routes were reviewed for code quality and correctness:
- **R1** (`/api/binding.json`, `/api/verify.json`): four-check binding
  (`tx_confirmed`, `time_ordering`, `fee_payer`, `mint_present`); all
  unknown-data paths return `unknown`, never `pass`. ✅ correct.
- **R2** (`crowd.py` → thinker `{crowd_line}`): fomo theses with author P&L
  formatted as omo-style evidence lines, injected into thinker prompt. ✅ wired.
- **R3** (`/api/theses.json`, `api/db.py:upsert_thesis/retire_thesis`):
  per-position write-up stored, retired at close with realized PnL. ✅ correct.
- **R4** (`rule_engine/liveness.py`): file-backed break state with atomic
  tmp-rename write, fail-closed on corrupt file, expiry-aware. ✅ correct.
- **R5** (`events`/`memories` tables, `/api/events.json`): append-only event
  stream + weighted memory recall with hit accounting. ✅ correct.
- **R6** (`/api/disclosure.json`, `/api/reasoning.json`): armed/break/config
  truths surfaced; zero secrets; per-decision inputs hash. ✅ correct.
- **R7** (`retro_matcher.py`): omo-exact algorithm; double-claim protection
  via `taken` set; exact-bind rows never overwritten. ✅ correct.

### Tests

- **9 new tests** in `backend/tests/test_admin_reset.py` covering prune
  (keeps newest N, no-op when under limit), reset (clears all tables,
  restores cash), and endpoint (confirm=yes required, unknown mode=400,
  both modes return correct summary).
- **Total: 231 passing** (was 222).

## 17. Supabase schema-drift incident + self-healing sync (2026-08-27)

### Incident

After the LLM observability work (§14) shipped, the LIVE Supabase book
started failing:

- **Every tick died** at `db.insert_event` — `UndefinedTableError: relation
  "events" does not exist` (0 completed ticks, 9 consecutive failures).
- **`GET /api/system-status` 500'd** on every dashboard poll —
  `UndefinedTableError: relation "llm_call_usage" does not exist` (265×).
- **Daily learning failed** on the same missing table.

**Root cause:** schema drift. The operator ran `001_init.sql` back when it
created 9 tables. Afterwards `events`/`memories` (OMO-R5) and `theses`
(OMO-R3) were added to that same file IN PLACE, and `002_llm_usage.sql`
(`llm_call_usage` + `model_version`/`prompt_version` columns) was added as a
new file — none of those updates ever reached the remote DB.
`db_pg.init_db()` only checked that the `'001_init'` row existed in
`schema_migrations`, so the backend booted "verified" straight into the
missing-table errors.

### Fix (self-healing schema sync)

- `db_pg.init_db()` now runs `_SCHEMA_SYNC_SQL` after the base-migration
  check: idempotent `CREATE TABLE IF NOT EXISTS` for `events`, `memories`,
  `theses`, `llm_call_usage` (+ their indexes), `ADD COLUMN IF NOT EXISTS`
  for the four versioning columns, `ENABLE ROW LEVEL SECURITY` on
  `llm_call_usage`, and records `'002_llm_usage'` in `schema_migrations`.
  Every statement is a no-op on an up-to-date book; a sync failure refuses
  boot (loud, fail-closed — never limp on a broken schema).
- `migrations/supabase/002_llm_usage.sql` gained the same RLS + bookkeeping
  statements so manual runs stay consistent. Migration files remain the
  source of truth for fresh installs.

### Surface-identity bugs fixed alongside (db_pg vs db.py)

- `insert_llm_call_usage` parameter renamed `status_str` → `status`
  (callers pass `status=`; would have been a guaranteed TypeError on PG).
- `get_llm_call_usage` now casts `ts`/`tick_ts` with `::text` — consumers
  (system-status route, `learning_loop`'s `.startswith(today)`) get ISO
  strings, not datetime objects, per the db_pg translation rules.
- `insert_feed_event` / `_FEED_COLS` / `_row_to_feed_dict` now carry
  `model_version` + `prompt_version` exactly like the SQLite surface.

### Verification (live)

- Full suite: **231 passing** (pytest forces SQLite — regression-only).
- Restart: boot log shows `db_pg: Supabase schema verified + synced`;
  **zero** new `UndefinedTableError`.
- `/api/system-status` → 200 with `llm_usage_recent` (28 rows after one
  tick: ISO-string `ts`, `status` success/error, degradation reasons).
- First full tick completed: `tick done … 20 candidates, 0 entries` —
  feed_events, OMO-R5 events (`thought`/`refused`), and decision commits
  all persisting to Supabase; every dashboard endpoint 200.

### Lesson

Never edit an applied migration file in place again. New schema = new
numbered migration file, AND mirror it into `_SCHEMA_SYNC_SQL` so older
books heal on boot.

## 18. NEXT TASK: main/narration LLM Groq → DeepSeek (2026-08-27) — ✅ DONE (see §19)

**Status:** IMPLEMENTED, SHADOW-GATED, AND FLIPPED LIVE (2026-08-27).
Funded key arrived; execution record in §19. Plan kept below as built.
**Scope (operator decision 2026-08-27):** the MAIN model path — Thinker,
Narrator, and post-close reflections — moves to DeepSeek V4 Flash. The
SOCIAL model stays exactly as it is (Groq via `SOCIAL_LLM_*`). This executes
the "recommended production arrangement" of docs/08 §1 and the "Planned next
step" of §14 above.

### What changes and what does not

| Role | Today | After swap |
|---|---|---|
| Thinker (pre-trade verdict) | Groq `qwen/qwen3.8-27b` via `MainGroqClient` | **DeepSeek V4 Flash** (direct API, non-thinking mode) |
| Narrator (thesis text on feed) | same `MainGroqClient` | **DeepSeek V4 Flash** |
| Post-close reflections | same `MainGroqClient` | **DeepSeek V4 Flash** (off-peak preferred) |
| Social evidence read | Groq via `GroqClient` / `SOCIAL_LLM_*` | **UNCHANGED — stays on Groq** |
| Deterministic gate / rules / exits | authoritative | UNCHANGED |

### Why this is a small change (§14 built for it)

- `llm/client.py` is already the provider-neutral `LLMClient.complete_json()`
  boundary (docs/08 §6); DeepSeek speaks the same OpenAI-compatible
  `/chat/completions`, including `response_format: {"type": "json_object"}`.
- DeepSeek-specific plumbing already exists in `LLMClient`: peak-window flag
  (01:00–04:00 + 06:00–10:00 UTC weekdays), `prompt_cache_hit_tokens` cache
  parsing, `is_peak_window` persisted on every result.
- `llm_call_usage` already records provider/model/tokens/cost/latency/
  degradation per call, and `model_version`/`prompt_version` stamp feed
  events + decision commits — Groq-vs-DeepSeek is queryable from day one.
- `backend/scripts/shadow_replay.py` + `outcome_labels.py` already exist to
  gate the swap.

### Implementation steps (when the funded key arrives)

1. **Config** (`backend/config.py` + `.env.example`) — re-add the DeepSeek
   block removed in the 2026-08-27 bug audit: `DEEPSEEK_API_KEY`,
   `DEEPSEEK_BASE_URL` (default `https://api.deepseek.com`), `DEEPSEEK_MODEL`
   (V4 Flash id — read the exact id + current prices from DeepSeek's pricing
   page at implementation time, per docs/08 §1), `DEEPSEEK_TIMEOUT_SECONDS`
   (12), `DEEPSEEK_MAX_TOKENS` (192). Add a `MAIN_LLM_PROVIDER` selector
   (`groq` | `deepseek`, default `groq`) so the swap is a reversible .env
   flip; keep `GROQ_*` as the warm rollback path.
2. **Client** (`backend/llm/client.py`) — add `DeepSeekClient(LLMClient)`
   (provider="deepseek", `is_main=True`) mirroring `MainGroqClient`, plus a
   `build_main_client()` factory keyed on `MAIN_LLM_PROVIDER`. Extend
   `_estimate_cost()` with a DeepSeek branch (peak/off-peak rates plus the
   discounted cached-input rate; cache-token plumbing already exists). Make
   the main-client timeout in `complete_json` provider-aware (today it is
   hardcoded to `GROQ_TIMEOUT_SECONDS`). Non-thinking mode only — no
   reasoning mode in the hot path until an offline benchmark proves a
   measurable gain (docs/08 §1).
3. **Thinker** (`backend/llm/thinker.py`) — `Thinker.__init__` takes
   `build_main_client()`; replace the hardcoded `groq:{GROQ_MODEL}` source
   label with `f"{provider}:{model}"`. Fail-closed fallback untouched: any
   failure still degrades to a template `pass` that may explain but never
   approve.
4. **Narrator + reflections** (`backend/llm/narrator.py`) — same client swap
   for `Narrator._main_llm` and `generate_reflection()`; same source-label
   treatment. Optional refinement per docs/08 §5: skip non-urgent reflections
   during DeepSeek peak windows.
5. **Dashboard** (`backend/api/routes/system_status.py`) — `narration_mode`
   reports the active main provider (`deepseek` | `groq` | `template`)
   instead of the hardcoded `"groq"`.
6. **Social** (`backend/llm/social.py`) — ZERO changes.
7. **Tests** — the 231-test suite stays green untouched (pytest forces mock →
   template path, no provider calls); add unit tests for `DeepSeekClient`
   construction, the peak/off-peak + cache cost branches, factory selection,
   and the `narration_mode` label.

### Gate before flipping the default (docs/08 §8–§9)

1. Run `shadow_replay.py` over sealed snapshots, DeepSeek vs Groq: ≥99%
   valid structured JSON, p95 latency inside the tick budget, verdict
   agreement, per-call cost.
2. Prove the fallbacks: provider down / timeout / malformed JSON / 429 /
   quota exhaustion → template degradation, zero trading-state effect.
3. Verify every `llm_call_usage` row carries tokens + estimated cost and
   daily spend stays under budget.
4. Only then set `MAIN_LLM_PROVIDER=deepseek` in `.env` and restart. Groq
   stays warm as rollback.

### Invariants that do not move

- DeepSeek `buy` remains a veto/input only: entry still requires
  `think.verdict == "buy"` AND all ten rules. The LLM never opens, closes,
  or sizes anything; no API failure can move money.
- Social reads stay evidence-only on Groq and can never flip a verdict.
- `PAPER_TRADING_ONLY=True` hardcoded; `live_execution` stays disarmed.
- Keys live only in the server `.env`; never in the repo, logs, or frontend.

### After the swap

Update §14 status, memory-bank (decisionLog / activeContext / progress),
docs/07_PROJECT_REPORT.md LLM section, and any doc still saying Groq-is-main.
✅ All done 2026-08-27 (§19).

## 19. DeepSeek main-provider swap — EXECUTED (2026-08-27)

§18 shipped end-to-end in one session: implemented, unit-tested,
shadow-replay-gated, flipped live, and verified on the live book.

### What shipped (code)

- **Config** (`backend/config.py`, `.env.example`): `DEEPSEEK_API_KEY`,
  `DEEPSEEK_BASE_URL` (https://api.deepseek.com), `DEEPSEEK_MODEL`
  (`deepseek-v4-flash` = DeepSeek-V4-Flash-0731, verified against
  api-docs.deepseek.com 2026-08-27), `DEEPSEEK_TIMEOUT_SECONDS=12`,
  `DEEPSEEK_MAX_TOKENS=192`, plus `MAIN_LLM_PROVIDER` (`groq`|`deepseek`,
  default `groq` in code; the live `.env` is flipped to `deepseek`).
- **Client** (`backend/llm/client.py`): `DeepSeekClient(LLMClient)`
  (`is_main=True`); `build_main_client()` factory (unknown value → loud
  warning + fail-closed to Groq); `main_max_tokens()` budget helper;
  provider-aware timeout attribute (was hardcoded `GROQ_TIMEOUT_SECONDS`);
  DeepSeek branch in `_estimate_cost()` — off-peak $0.22/1M cache-miss
  input, $0.007/1M cache-hit input, $0.66/1M output; peak = exactly 2×
  (01:00–04:00 + 06:00–10:00 UTC Mon–Fri); per-provider pricing snapshot
  ids (`groq_20260826`, `deepseek_20260827`).
- **Thinker / Narrator / reflections**: use the factory; source labels are
  now `f"{provider}:{model}"` (no hardcoded `groq:` strings); reflections
  skip to the template during DeepSeek peak windows (docs/08 §5; logged).
- **Dashboard**: `/api/system-status.narration_mode` reports the active
  main provider (`deepseek`|`groq`|`template`).
- **Social**: ZERO changes (verified).
- **Ops**: `start.sh` key check + banner are now provider-aware;
  `backend/test_llm.py` pings factory main + explicit DeepSeek + social
  with usage/cost/peak readouts; `scripts/shadow_replay.py` rebuilt on the
  repository layer (works on SQLite AND Supabase; replays sealed
  feed-event candidate snapshots and compares against the commit's
  original think verdict; reports agreement/degradation/latency p95).

### Bugs found and fixed during the swap (all regression-tested)

1. **`is_peak_window` was never computed** — `complete_json` initialized
   `is_peak = False` and never called `_is_peak_window()`, so every call
   was stamped off-peak and DeepSeek cost would have been understated 2×
   during peak hours. Fixed: computed per call for the deepseek provider
   (Groq has no peak pricing and stays False). Proven live: 08:57 UTC
   calls now stamp `peak=1` with exact 2× cost.
2. **DeepSeek thinking mode emptied `content`** — V4 Flash DEFAULTS to
   thinking mode; reasoning burned the 192-token budget and returned empty
   `content` (HTTP 200!), degrading every thinker call to a template pass.
   First live tick showed 20/20 `degraded:empty_content`. Fixed by sending
   `"thinking": {"type": "disabled"}` on every DeepSeek request
   (non-thinking mode per docs/08 §1). Shadow replay went 0/8 → 8/8.
3. **`shadow_replay.py` raw SQL** — SQLite `?` placeholders broke on
   Supabase (asyncpg syntax error) and it assumed a `candidate` key the
   commit payload doesn't carry. Rewritten on `get_recent_decision_commits`
   + `get_feed_events` (no raw SQL outside api/db*.py rule restored).

### Live verification (2026-08-27)

- Ping: DeepSeek `/models` health OK; completion OK with usage parsing
  (peak-window flag + 2× peak cost verified against the machine clock).
- Shadow replay (Supabase book): **8/8 verdict agreement, 0 degraded,
  100% valid structured JSON, latency p95 2629ms** (budget: 12s timeout).
- Flipped `.env` to `MAIN_LLM_PROVIDER=deepseek`, restarted via
  stop.sh/start.sh (port-free wait honored): `/api/system-status` →
  `narration_mode: "deepseek"`; first full tick completed
  (`tick done … 20 candidates, 0 entries`); every thinker row in
  `llm_call_usage` is `status=success` with tokens + peak cost
  (~$0.001/candidate at peak; ~1800 in / ~140 out typical); feed events
  carry `narration_source: deepseek:deepseek-v4-flash` and
  `model_version: deepseek-v4-flash`; zero tracebacks.
- Fail-closed proven live (unintentional experiment): the brief pre-fix
  window recorded 20 `degraded:empty_content` thinker calls → template
  passes, zero entries opened, zero money moved.

### Tests

- **30 new tests** in `backend/tests/test_llm_provider_swap.py`
  (httpx.MockTransport; no new deps): construction + provider-aware
  timeouts, factory selection incl. fail-closed unknown value,
  `main_max_tokens`, cost branches vs hand-computed values (docs/08 §3
  anchor $0.000176), peak flag + 2× peak cost, Groq-never-peak,
  thinking-disabled payload, usage/cache parsing, degradation paths,
  thinker/narrator source labels, fail-closed never-buy, reflection
  peak-skip, `narration_mode` endpoint labels.
- **Total: 261 passing** (was 231): backend 213, live_execution 48.

### Rollback

`MAIN_LLM_PROVIDER=groq` in `.env` + restart. NOTE: `GROQ_API_KEY` is
currently EMPTY in the live `.env` — rolling back today degrades the main
path fail-closed to template passes (safe, no entries) until the Groq key
is re-added. DeepSeek key stays in `.env` either way.

### Invariants held

Entry still requires `think.verdict == "buy"` AND all ten rules; the LLM
never opens/closes/sizes; `PAPER_TRADING_ONLY=True` untouched;
`live_execution` untouched and disarmed; keys only in server `.env`


---

## 20. FOLLOW-UP FIX — stealth-scrape chain: 429 rate-limit vs 402 credits (2026-08-27, DONE)

Operator reported Firecrawl/ZenRows "still have credits; only bee is full".
Live diagnosis (keys never printed) found the real story:

- **Firecrawl** was healthy (returning 200s) but a single `429 Too Many
  Requests` — a transient *rate-limit* — benched it for the full 30 min.
  **This was the bug.**
- **ZenRows**' key in `.env` is genuinely at its usage limit: `AUTH004
  "account has reached its usage limit"` on BOTH a cheap basic request and the
  premium one. (Operator may be viewing a different account/key.)
- **ScrapingBee** basic tier answers 200, but prod calls it with
  `stealth_proxy=true` which errors, and it can't forward the Privy bearer
  anyway — so it never serves fomo reads regardless of credits.

### Fix (`backend/data_providers/crowd.py`, `config.py`, `.env.example`)
- New `_handle_provider_status()`: **HTTP 402** (credit exhaustion) → long
  `STEALTH_BENCH_SECONDS` bench; **HTTP 429** (rate-limit) → short
  `STEALTH_THROTTLE_BACKOFF_SECONDS` (default **75s**) backoff. Applied to both
  the Firecrawl adapter and the generic GET template (bee/dog/zenrows/scrapeops).
- Provider's own error body is now logged on 402/429, so quota reasons (e.g.
  ZenRows `AUTH004`) are self-diagnosable from `logs/backend.log`.
- Empty scrape exceptions now log their exception type (was a blank message).
- `_bench(name, seconds=None)` gained an optional duration override.

### Tests
- New `test_throttled_firecrawl_gets_short_backoff`: a 429 fails over AND is
  benched only for the short backoff (proves it's eligible again quickly, which
  a 30-min bench would not allow). Existing 402→failover test still green.
- `fresh_state` fixture now also resets `_BENCHED_UNTIL` (no cross-test bleed).
- **Total: 262 passing** (was 261): backend 214, live_execution 48.

### Invariants held
Read-only fail-soft path; no money logic touched; `live_execution` untouched;
keys only in server `.env` (diagnostic script redacted them).



---

## 21. Omo-style brain — ported the LLM *reasoning layer* of omotrades/omo (2026-08-27, DONE)

Operator decision: "compare how the LLM thinker works in omotrades/omo and in our
repo — clone that to ours." We ported omo's **reasoning layer only** (never its
execution posture), so the bot now *thinks* like omo while every safety gate stays.

### What omo does that we cloned
omo is "not one model": each stage declares the mind it was designed around plus an
ordered fallback chain, and resolution is **honest** (it reports the model it
actually used and whether it ran degraded — it never claims an unreachable model).
Each tick is ONE richly-prompted call that reads the whole book + tape + crowd +
web and emits a structured JSON tick: `thoughts / actions / verdicts / theses /
watchlist / remember / fomo / break`. Every verdict carries 5–7 checks from
DIFFERENT research buckets (tape / people / crowd / smart-money / outside read /
counter-case), an entry condition, and an invalidation. Ground-truth rules force
every number to be copied from the snapshot (never invented). The wallet is fed in
as context (omo's `positionBlock`) so the brain reasons over live positions + pnl.

### What shipped (code)
- **`backend/llm/omo_brain.py`** (new, ~530 lines):
  - `run_role()` — role-based router (port of omo `models.server.ts`). Roles
    `reasoning`/`realtime`/`narration`, each a provider chain (`main`→`groq`).
    An unsupported-model error benches that provider for the process; any other
    failure just falls through for the call. Returns honest `ResolvedRole`.
  - `OMO_SYSTEM` + `OMO_OUTPUT_CONTRACT` — the omo tick prompt (hard filters,
    decision buckets, ground-truth + price-talk rules, minified-JSON contract),
    with omo's persona lore deliberately dropped.
  - `build_wallet_block()` / `build_snapshot_block()` / `build_tick_prompt()` —
    wallet mimicry + the screener rows the model may cite (None-safe).
  - `parse_omo_tick()` — strict schema/type/range validation. Invented symbols and
    invalid calls are **dropped**; malformed body → `None` (caller fails closed).
  - `OmoBrain.tick()` — one role-routed call grades up to 8 highest-volume
    candidates; fail-closed to an empty verdict map on any error.
- **`backend/config.py`**: `OMO_BRAIN` (default on), `OMO_BRAIN_MAX_TOKENS=4000`,
  `OMO_BRAIN_TIMEOUT_SECONDS=60` (the brain's large output needs a longer read
  timeout than the 12s per-candidate thinker).
- **`backend/main.py`**: `run_tick(..., brain=)` runs the brain in live mode; each
  candidate uses the brain's verdict if it produced a valid one, else falls back to
  the per-candidate thinker. `_think_from_omo()` maps omo `call:"buying"`→our
  `verdict:"buy"` (only a NECESSARY input — the deterministic gate still ANDs).
  The single brain call's usage is recorded once in `llm_call_usage`.

### Pre-existing bug found + fixed (crashed the tick)
`reused_if_stable()` (`llm/reuse.py`) always required `prior["stats"]`, but the
cross-tick thesis writer in `main.py` stored the `decision` dict **without** a
`"stats"` key. So any time a mint reappeared within the reuse window it raised
`KeyError: 'stats'` and killed the whole tick. Fixed both ways (defense-first):
- writer now stores `"stats": stats_signature(c)` (reuse works as designed);
- `reused_if_stable()` fails CLOSED (returns False, never raises) on a
  malformed/legacy prior missing any required key.

### Live verification (2026-08-27)
- **Brain ping (real DeepSeek V4 Flash, 10 synthetic candidates):** cap applied
  (10→8 graded), `DELTA=buying` (6 checks + concrete invalidation), `ETA/ALPHA=
  stalking`, rest `pass`; fomo 40, 9 thoughts, 3 watchlist; **2268 output tokens,
  15s, $0.003, no truncation, no timeout**. All numbers copied from the snapshot.
- **Fail-closed proven live:** an early run where the response truncated at the old
  1500-token budget degraded to 0 verdicts and every candidate safely fell back to
  the per-candidate thinker — 0 tracebacks, no bad buys. (Fixed by raising the
  budget to 4000 + capping graded candidates at 8.)
- Backend restarted clean on the new code: `PAPER_TRADING_ONLY=True | backend=live`,
  Supabase synced, 0 tracebacks.

### Tests
- New `tests/test_omo_brain.py` (23 tests): parse/validate (invented symbols and
  invalid calls dropped; malformed → None), call mapping (only `buying`→wants_entry),
  role routing (honest label, fallback labelled degraded, unsupported-model benches,
  timeout is NOT unsupported-model), wallet/snapshot builders (None-safe), and
  `OmoBrain.tick` fail-closed paths (mock hermetic, unparsable, empty candidates).
- New reuse regression tests (4): malformed/legacy prior fails closed, never raises.
- **Total: 289 passing** (was 262): backend 241, live_execution 48.

### Invariants held
The LLM remains a VETO/INPUT only — `wants_entry` is necessary, never sufficient;
entry still requires `gate.all_passed AND wants_entry`. The brain never opens,
closes, sizes, or touches execution. `PAPER_TRADING_ONLY=True` still hardcoded +
asserted; `live_execution/` untouched and disarmed. Mock mode is hermetic (brain
inert). Every degradation logged with a reason. Keys only in server `.env`.

(redaction verified in logs); no raw SQL outside api/db*.py.
