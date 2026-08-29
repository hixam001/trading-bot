**Last updated:** 2026-08-28 · **Branch:** main · **Status:** LIVE
(real market data, simulated funds; Supabase Postgres persistence active) ·
**App:** http://localhost:8000
**Tests:** 516 backend/live_execution passing + 8 Playwright E2E (suite
fully green; the flag-state canary now pins the committed ARMED state — §33)

Read this top-to-bottom before touching anything. It contains everything a
new session needs: state, decisions, bugs fixed, invariants, and next steps.

---

## 1. What this project is

A local **paper-trading research system** for Solana memecoins. Every tick
(~60s) it fetches real candidates (Birdeye memepool trending), enriches them
(Dexscreener pairs), computes a market-wide regime snapshot, and evaluates
each candidate against **ten deterministic rules**. The AND of all rules is
the entire entry decision. Exits come only from three fixed numeric checks.
A cloud LLM (DeepSeek V4 Flash by default via `MAIN_LLM_PROVIDER`; Groq as
warm rollback — no local model, Ollama retired 2026-08-28) performs a
pre-trade **think/veto
  stage** and narrates decisions; entry still requires the model's buy verdict
  AND every deterministic rule to pass. The model never sizes, opens, closes,
  or overrides numeric exits. Everything is logged to SQLite
and visible in a React dashboard served by the backend itself.

**Non-negotiable:** the paper-trading pipeline (`backend/`) is paper only —
no wallet, no transaction construction anywhere there.
`PAPER_TRADING_ONLY = True` is hardcoded in `backend/config.py` and
runtime-asserted inside every position-opening function. The separate
`live_execution/` package at the repo ROOT (never imported by backend/) is
the only real-execution code. **Since 2026-08-28 it is committed ARMED**
(`LIVE_TRADING_ENABLED = True`, `REQUIRE_MANUAL_CONFIRMATION = False`) by
explicit operator direction after the §27 devnet drill passed 5/5 (§31/§33);
kill switch + daily-loss breaker + caps + identity pin all remain active.
The flags stay human-edit-only (no env bypass). If a task ever
seems to require real execution inside backend/ — stop and flag it.

## 2. How to run / stop / test

```bash
./start.sh        # one click: builds frontend, starts backend+tick loop on
                  # :8000 (serves dashboard), opens browser. Idempotent.
                  # No local model: LLM = DeepSeek/Groq cloud APIs via .env.
./stop.sh         # stops the backend (+ tick loop)
cd backend && ../.venv/bin/python -m pytest tests/ -q   # backend-only: 385 tests
.venv/bin/python -m pytest -q                           # full suite: 501 tests, ~2s
                                                         # (fully green while armed — §33 canary)
cd frontend && npm run test:e2e                          # Playwright E2E: 5 tests vs the
                                                         # running backend on :8000 (§35)
```

- Dashboard/API: http://localhost:8000 (single origin; backend serves the
  built frontend from `frontend/dist`)
- Logs: `logs/{backend,frontend-build}.log`; pids in `.run/`
- Dev frontend hot-reload: `cd frontend && npm run dev` (:5173, proxies /api,/ws)
- Knowledge ingest: `cd backend && ../.venv/bin/python scripts/ingest_directory.py <dir>`

## 3. File map (what lives where)

| Path | Purpose |
|---|---|
| `backend/config.py` | ALL thresholds + safety flag + provider keys via env |
| `backend/models.py` | Candidate (incl. `decimals`), Trade, FeedEvent, RuleResult, GateDecision, PortfolioState |
| `backend/rule_engine/` | `rules.py` (11 reference-parity entry rules), `exits.py` (the reference exit engine: stop/trail/liquidity-break/invalidation/stale/TP-ladder + sell risk gate), `gate.py` (no-short-circuit AND), `regime.py`, `liveness.py` (not_on_break) |
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
| `live_execution/` | REAL-MONEY execution package at repo ROOT (never imported by backend/). Fully wired: `run_live_cycle.py` manages live positions, runs the shared read/think/gate stages, and routes buys/sells through `place_order`; Jupiter quote/swap, local signing, rotating RPC broadcast, confirmation, commit binding, and ledger journaling are connected. **REF-R11 (§26):** every armed order publishes its decision hash as an on-chain memo BEFORE the fill (fail-closed). Safety layers: kill switch + daily-loss breaker, fail-closed confirmation expiry, idempotency ledger, caps, wallet identity checks, decimals guards, SOL-reserve + USDC funding checks. **ARMED in this repo since 2026-08-28** (§33): `LIVE_TRADING_ENABLED=True`, `REQUIRE_MANUAL_CONFIRMATION=False`, committed by explicit operator direction after the §31 devnet drill. Operator CLI: `python -m live_execution.scripts.confirm_trade list|approve|deny|kill|resume`. |
| `live_execution/memo.py` | REF-R11 commit–reveal: builds + publishes the on-chain precommit memo (`commit:v1:` + seal hash) via solders; `publish_commit_memo()` fails closed (`MemoPublishError`) so an unconfirmed memo blocks the fill |
| `frontend/DESIGN.md` | the frontend design system: tokens, component anatomy, five required states, a11y criteria, anti-patterns, QA checklist. All UI work must conform |
| `frontend/src/` | live-trading terminal (rebuilt §35): `lib/format.ts` (verbatim-value formatters, `—` for null), `components/ui.tsx` (Panel/Stat/Badge/Skeleton/Empty/ErrorState), `LiveBook` (headline real-money panel), `LiveFeed` (WS + REST-hydrated decision feed), `MarketRegimePanel`, `SystemStatus`; paper panels (stats/holdings/journal/gate) removed 2026-08-28 when the system went live |
| `frontend/e2e/dashboard.spec.ts` | Playwright E2E (5 tests): zero console errors, all panels reach data/empty (never blank), feed expand/collapse + aria-expanded, keyboard operability, offline banner on API loss. `npm run test:e2e` (needs backend on :8000) |
| `backend/api/routes/proof.py` | REF-R1 binding report (`/api/binding.json`), `/api/verify.json` (REF-R11: also re-verifies the on-chain commit memo hash + slot ordering), `/api/refusals.json`, `/api/theses.json`, `/api/proof.json`, `/api/exits.json` |
| `backend/api/routes/disclosure.py` | REF-R6 public machine-truth feeds: `/api/disclosure.json` (armed/break/config state + REF-R11 `commit_memo` block) + `/api/reasoning.json` (per-decision provenance) |
| `backend/retro_matcher.py` | REF-R7 retro audit-log signature matching: attributes out-of-pipeline fills to decision commit rows using the reference's exact algorithm (symbol+side match, 12h window, earliest fill wins, taken set) |
| `backend/thesis_restate.py` | A11 thesis re-authoring (§30): once per tick/cycle, rewrites open write-ups that are stale (>6h) or not model-authored against the position's current numbers (≤2/pass, oldest first). Narrative-only — can only ever change thesis text; reuses the tick's own price marks; fail-closed validation; never raises |
| `docs/00..07` | blueprint, architecture, feature list (+status), gantt, verification appendix, the reference bot comparison, project report |
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
10. **the reference bot reference check** (docs/06): their revealed payloads show
    verdict:"pass" WITH failed rules + side:null + refusal note → an agent
    layer decides between rules and action. Our architecture closes that
    gap deliberately. crowd_heat (fomo index) has no equivalent here;
    already_held is binary vs our scale-in cap (intentional). Commit-reveal
    mechanism verified byte-for-byte locally
    (scripts/verify_reference_commit.py, both hashes MATCH), documented in
    docs/05 — deliberately NOT built.
11. **One-click app**: start.sh/stop.sh; backend serves
    built frontend (SPA catch-all registered AFTER api routes). Ollama was
    retired from the stack 2026-08-28 (DeepSeek is the main model) — the
    scripts no longer start or manage it and clean up the old `.run/ollama*`
    markers on launch.
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
18. **Process management**: backend launch via `setsid` (own
    session — Konsole Ctrl+C / tab-close cannot kill it; nohup alone only
    blocks SIGHUP). (Historical: the old ollama launch path used
    `timeout 10 ollama list` guards; ollama retired 2026-08-28.) API
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
- Post-calibration scope, deliberately unbuilt: Full multi-wallet management, auto-adjustment of thresholds based on LLM feedback (docs/08), durable reference memory/events roadmap (REF-R5), and the remaining approved reference roadmap items in section 13.

### REF-R5 implementation (2026-08-26)

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
4. After calibration: complete approved reference roadmap work in section 13,
  starting with REF-R5 memory/events.
5. **LLM API migration:** implemented in section 14.
  docs/08_LLM_API_MIGRATION_AND_FEEDBACK_PLAN.md. Target Groq for the thinker (qwen3.8-27b), Groq for evidence-only social reads, and
  measured usage/outcome instrumentation before switching models.
   Next stage: main/narration model → DeepSeek V4 Flash (social stays Groq)
   — full plan in section 18, gated on a funded DeepSeek key + shadow replay.
6. No automatic learning, threshold changes, prompt changes, model changes,
   or live-trading promotion is permitted.
7. **Reference parity for the original scope is complete (REF-R1–R9 + R11).**
   The 2026-08-27 omo audit added a five-item **code queue** (A7 wash-trade
   filter, A6 symbol blocklist, A3 venue attribution, A2 chain book re-derivation,
   A4 own-basis read-back) — see §28. Those ship test-green FIRST; **§27 —
   Enable live execution** stays the operator's final manual task after them.
   No session may arm before every other task is done.
8. **Full remaining-task list lives in §28** (grouped: code queue, final gating
   task, in-progress calibration, post-calibration, optional upgrades, deferred,
   and never-to-build invariants). Read §28 before picking up any new work.

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




## 10. reference-parity batch (2026-08-25)

- **Candidate breadth**: chg5m/6h/24h, fdv, buys/sells/vol 6h, pool_count,
  total_liquidity_usd, top_pool_share, boosted (models.py + dexscreener.py).
- **research.py** (new): the reference researchToken port - cross-pool aggregates,
  wired into main.py read stage (live-only, RESEARCH_PER_TICK cap).
- **discovery.py rebuilt**: slot-composed board (flow core + newborn slots +
  mover slots + 5 guaranteed rotation slots, cap 16) + boost feeds -> boosted flag.
- **Refusals public**: get_refusal_events() in db.py AND db_pg.py;
  GET /api/refusals.json; refusals now inside /api/proof.json.
- **live_execution the reference engine**: solana.py (multi-RPC failover, send,
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
- **Gate is the reference bot 9 rules + security_clear** (market_regime_ok
  stays retired from ACTIVE_RULES — observability only; security_clear was
  RE-ACTIVATED as the 10th gate rule 2026-08-29 by explicit operator
  decision: KNOWN-bad-only semantics per B10, `None`/unknown always passes).
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

## 13. Approved reference-parity roadmap (2026-08-26) — TO BE IMPLEMENTED

Decision record from the source-level comparison vs the reference repository (full detail
in docs/P0_REPORT.md §6). Seven features APPROVED, one REJECTED, one DEFERRED.
Every approved item carries a stable ID (**REF-R#**) — use it in branch names,
commit messages and test files so later implementation is traceable. Order of
implementation: R5 first (marked important), then R4, R3, R2, R1, R6, R7.

> **All seven items below (REF-R1–R7) are IMPLEMENTED.** A second batch from the
> 2026-08-27 audit — **REF-R8 (drawdown sizing), REF-R9 (closed-loop learning),
> REF-R10 (live execution, gated), REF-R11 (on-chain memo, re-opened)** — is
> documented in **§22**.


### REF-R5 — Memory/events system ✅ IMPLEMENTED (2026-08-26)
- reference: event/memory types + hydrate logic in
  `src/lib/brain.server.ts`.
- Persistent event log with kinds `thought|did|refused|read|trade`, plus
  weighted memories (topic, note, weight, hits) recalled into the thinker
  prompt so accumulated lessons influence future decisions; `hits` increments
  on recall.
- Touch points: `events` + `memories` tables in BOTH api/db.py and api/db_pg.py
  (surfaces stay identical); writer hooks in every tick stage; recall injected
  as `{memory_line}` in the thinker prompt (same pattern as social/web lines);
  expose recent events via existing feed routes.
- Operator flagged this IMPORTANT: do this one before all others.

### REF-R4 — Self-regulating break system ✅ IMPLEMENTED (2026-08-26)
- reference: `not_on_break` gate rule + `breakUntil`/`breakReason` state in
  brain.server.ts.
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

### REF-R3 — Durable thesis book ✅ IMPLEMENTED (2026-08-26)
- reference: `src/lib/theses.server.ts` + public `/api/public/theses.json`.
- Per-position write-up as database state: created at entry (required author =
  `operator` | `model` plus model id when model), revised while held, retired
  at close with realized PnL filed against the row. Live size/pnl always
  refreshed from the book/chain, NEVER trusted from the row. Stored text wins
  over any late/cached feed read (the reference invariant).
- Touch points: `theses` table in db.py + db_pg.py; write hook after a live
  buy confirms AND after paper opens (paper rows carry their own thesis);
  retire hook in both exit paths; read-only `GET /api/theses.json`.

### REF-R2 — FOMO crowd intel upgrade: theses WITH author P&L ✅ IMPLEMENTED (2026-08-26)
- reference: `src/lib/fomo.server.ts` (`readFomoIntel`,
  `describeFomoIntel`, `readOwnBasis`).
- Current state: data_providers/crowd.py already reads thesis COUNTS off
  prod-api.fomo.family for crowd_heat (Privy session + Firecrawl stealth
  fallback, queue-gap + response-TTL plumbing done).
- Upgrade scope: return FULL thesis rows — text, handle, author position
  sizeUsd, unrealizedUsd, realizedUsd, closed — and render reference-style evidence
  lines ("@who on $SYM — holding $X, up $Y (Z%): \"text\"") into the thinker
  prompt as a new `{crowd_line}`; prompt instructs the model to weigh each
  claim by whether its author is actually up on the position.
- Extend crowd.py — do NOT duplicate its session/proxy plumbing. Fail-soft as
  today: no feed = empty line, loop continues. Env unchanged:
  FOMO_PRIVY_REFRESH_TOKEN + FIRECRAWL_API_KEY.

### REF-R1 — Independent verifier upgrade + binding report ✅ APPROVED
- reference: `src/lib/verify.server.ts` + `readBinding()` in
  precommit.server.ts.
- Scope NOTE: on-chain memo sealing was REJECTED (see below), so verification
  cannot mirror the reference's memo-hash checks. What IS verifiable without memos:
  extend /api/verify.json so each decision-commit row bound to a fill is
  checked against PUBLIC RPC instead of our own journal:
    1. fetch the fill tx via getTransaction on the bound signature
       (needs a get_transaction helper in live_execution/solana.py);
    2. checks per row: tx exists and confirmed; time ordering (commit
       sealed_at < fill blockTime); account key 0 == our wallet address;
       pre/postTokenBalances include the committed mint;
    3. new read-only `GET /api/binding.json` in api/routes/proof.py:
       pairs committed mint vs mint actually touched, with matched /
       mismatched counts (the reference BindingReport shape).
- Touch points: live_execution/solana.py (get_transaction), api/routes/proof.py
  (extend verify + add binding), commit rows already carry payload/nonce/
  signature. Tests: mock RPC fixtures; every mismatch case must report the
  FAILED check explicitly — a check that cannot run reports unknown, never
  pass.

### REF-R6 — Public machine-truth feeds (disclosure + reasoning) ✅ APPROVED
- reference: `src/lib/disclosure.server.ts`; `/api/public/disclosure.json`
  and `/api/public/reasoning.json`.
- Minimum scope: two more read-only JSON endpoints alongside proof/exits/
  verify/refusals:
    - `/api/disclosure.json` — live machine state: armed/disarmed flag,
      kill-switch state, break state (REF-R4), last cycle timestamp + step
      results, config truths (caps, floors), no secrets;
    - `/api/reasoning.json` — per-decision provenance: which model produced
      the thesis, stage timings, inputs snapshot hash (sha256 of the gated
      inputs), linked commit hash.
- Full web UI terminal is explicitly OUT OF SCOPE for now; JSON first, a UI
  can be layered later on the same endpoints.

### REF-R7 — Audit-log signature matching (retro attribution) ✅ APPROVED
- reference: `linkAuditToFills()` in `src/lib/audit.server.ts`.
- Purpose: attribute fills to decision rows when a fill BYPASSES the pipeline
  (e.g. a hand-placed trade against the live wallet once armed). Exact
  bind-at-execute (CommitLog.bind) stays the PRIMARY binding and is never
  overwritten by this layer — retro matching only ever touches rows whose
  signature is still null.
- Algorithm (the reference's, kept intact):
    1. pending = decision rows with verdict=act AND signature IS NULL
       (newest 60);
    2. candidates = recent fills (newest 120) whose signature is not already
      claimed by another row;
    3. match on: same symbol (case-insensitive, $ stripped) + same side +
       fill_at >= decision_at + within a 12h window;
    4. earliest unmatched fill wins; a `taken` signatures set grows during the
       run so nothing is claimed twice;
    5. write back signature, matched_at, phase=filled.
- Safeguards (beyond the reference): an "unattributed fills" listing must be visible in
  /api/proof.json rather than silently heuristic-matching every orphan; each
  matched row keeps a `matched_by: retro` marker so exact vs retro bindings are
  distinguishable in the public surface.
- Touch points: query helpers in api/db.py + api/db_pg.py (identical surfaces),
  matcher module callable from run_live_cycle.py post-cycle step and main.py,
  surfaced via api/routes/proof.py.
- Tests: double-claim prevention (3 decisions / 2 fills), window edge cases,
  side/symbol mismatch rejection, exact-bind precedence over retro.

### REJECTED / DEFERRED — do not re-litigate without operator approval
- ✅ **On-chain memo commitments + reveal protocol** (the reference precommit memo layer)
  — was REJECTED 2026-08-26, then RE-OPENED by the 2026-08-27 audit and
  **APPROVED + IMPLEMENTED 2026-08-27 as REF-R11 (§26)**. No longer a gap.
- ⏸ **Off-book multi-chain tracking** (BNB/other-chain positions marked and
  repriced into equity) — DEFERRED temporarily. Revisit only after the Solana
  side is armed and has a live track record.
- ℹ️ Armed trading history needs no build work: it accrues automatically once
  live cycles run armed.
- 📝 Audit-log retro signature linking was initially REJECTED (2026-08-26,
  rationale: exact bind-at-execute leaves no bypass channel while disarmed),
  then APPROVED as REF-R7 in the same session once the operator reviewed the
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
what comes next. Full requirements and reference references remain in sections 13
and 14 above.

### Approved — implement in this order

1. **REF-R5 Memory/events system** — ✅ IMPLEMENTED.
2. **REF-R4 Self-regulating break system** — ✅ IMPLEMENTED. *(Bug fixed 2026-08-26: `liveness.set_break(think.break_minutes, think.break_reason)` was passing int/str in wrong positional slots; fixed to `set_break(True, think.break_minutes, think.break_reason)`.)*
3. **REF-R3 Durable thesis book.** — ✅ IMPLEMENTED.
4. **REF-R2 FOMO intel with author P&L.** — ✅ IMPLEMENTED.
5. **REF-R1 Independent verifier and binding report.** — ✅ IMPLEMENTED. Four-check binding verification (`tx_confirmed`, `time_ordering`, `fee_payer`, `mint_present`) in `/api/binding.json`. Fail-closed: missing RPC data → `unknown`, never `pass`. New `signature/phase/matched_by` columns on `decision_commits`.
6. **REF-R6 Public disclosure and reasoning feeds.** — ✅ IMPLEMENTED. `/api/disclosure.json` (machine state, no secrets) + `/api/reasoning.json` (per-decision provenance: model source, inputs hash, commit hash).
7. **REF-R7 Retro audit-log signature matching.** — ✅ IMPLEMENTED. `retro_matcher.py` runs post-cycle in both `main.py` and `run_live_cycle.py`. Exact-bind rows (`signature IS NOT NULL`) never overwritten. Double-claim prevented by `taken` set.
8. **LLM migration completion** — Groq for thesis/thinker and reflections;
   Groq for social evidence; usage accounting, shadow replay, paper canary,
   and delayed outcome labels before any model promotion. DeepSeek migration is
   planned next once API keys with sufficient balance are available.
9. **REF-R8 Drawdown-adaptive position sizing** — ⏳ TO BE IMPLEMENTED (batch 2,
   §22). Equity-based ticket with a drawdown factor + hard order/daily ceilings,
   ported from the reference `risk.server.ts`. Paper-compatible, fail-closed.
10. **REF-R9 Closed-loop learning → conviction factor** — ⏳ TO BE IMPLEMENTED
    (batch 2, §22). Bounded [0.6, 1.2] conviction factor from realized outcomes
    that multiplies the R8 ticket, ported from the reference `learn.server.ts`.
    Build AFTER R8.

### Rejected or deferred — do not implement without approval

- **Rejected:** on-chain memo commitments and reveal protocol. Local
  CommitLog sealing remains the chosen mechanism. *(Re-opened ONLY as planning
  item REF-R11 in §22 per the 2026-08-27 audit; still requires explicit operator
  approval before any code.)*
- **Deferred:** off-book multi-chain tracking until Solana has a live track
  record.
- **Deferred-by-design:** live execution (REF-R10, §22) — the promotion path.
  The live execution package is wired but remains disarmed; a funded
  throwaway-keypair devnet drill plus promotion-gate green-light plus explicit
  operator approval are all required before any arming discussion.
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

### REF-R1–R7 audit (2026-08-27)

All seven Reference parity routes were reviewed for code quality and correctness:
- **R1** (`/api/binding.json`, `/api/verify.json`): four-check binding
  (`tx_confirmed`, `time_ordering`, `fee_payer`, `mint_present`); all
  unknown-data paths return `unknown`, never `pass`. ✅ correct.
- **R2** (`crowd.py` → thinker `{crowd_line}`): fomo theses with author P&L
  formatted as reference-style evidence lines, injected into thinker prompt. ✅ wired.
- **R3** (`/api/theses.json`, `api/db.py:upsert_thesis/retire_thesis`):
  per-position write-up stored, retired at close with realized PnL. ✅ correct.
- **R4** (`rule_engine/liveness.py`): file-backed break state with atomic
  tmp-rename write, fail-closed on corrupt file, expiry-aware. ✅ correct.
- **R5** (`events`/`memories` tables, `/api/events.json`): append-only event
  stream + weighted memory recall with hit accounting. ✅ correct.
- **R6** (`/api/disclosure.json`, `/api/reasoning.json`): armed/break/config
  truths surfaced; zero secrets; per-decision inputs hash. ✅ correct.
- **R7** (`retro_matcher.py`): reference-exact algorithm; double-claim protection
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
created 9 tables. Afterwards `events`/`memories` (REF-R5) and `theses`
(REF-R3) were added to that same file IN PLACE, and `002_llm_usage.sql`
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
  feed_events, REF-R5 events (`thought`/`refused`), and decision commits
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

## 21. Reference-style brain — ported the LLM *reasoning layer* of the reference repository (2026-08-27, DONE)

Operator decision: "compare how the LLM thinker works in the reference repository and in our
repo — clone that to ours." We ported the reference's **reasoning layer only** (never its
execution posture), so the bot now *thinks* like the reference while every safety gate stays.

### What the reference does that we cloned
the reference is "not one model": each stage declares the mind it was designed around plus an
ordered fallback chain, and resolution is **honest** (it reports the model it
actually used and whether it ran degraded — it never claims an unreachable model).
Each tick is ONE richly-prompted call that reads the whole book + tape + crowd +
web and emits a structured JSON tick: `thoughts / actions / verdicts / theses /
watchlist / remember / fomo / break`. Every verdict carries 5–7 checks from
DIFFERENT research buckets (tape / people / crowd / smart-money / outside read /
counter-case), an entry condition, and an invalidation. Ground-truth rules force
every number to be copied from the snapshot (never invented). The wallet is fed in
as context (the reference's `positionBlock`) so the brain reasons over live positions + pnl.

### What shipped (code)
- **`backend/llm/llm_brain.py`** (new, ~530 lines):
  - `run_role()` — role-based router (port of the reference `models.server.ts`). Roles
    `reasoning`/`realtime`/`narration`, each a provider chain (`main`→`groq`).
    An unsupported-model error benches that provider for the process; any other
    failure just falls through for the call. Returns honest `ResolvedRole`.
  - `LLM_SYSTEM` + `LLM_OUTPUT_CONTRACT` — the brain tick prompt (hard filters,
    decision buckets, ground-truth + price-talk rules, minified-JSON contract),
    with the reference's persona lore deliberately dropped.
  - `build_wallet_block()` / `build_snapshot_block()` / `build_tick_prompt()` —
    wallet mimicry + the screener rows the model may cite (None-safe).
  - `parse_llm_tick()` — strict schema/type/range validation. Invented symbols and
    invalid calls are **dropped**; malformed body → `None` (caller fails closed).
  - `LLMBrain.tick()` — one role-routed call grades up to 8 highest-volume
    candidates; fail-closed to an empty verdict map on any error.
- **`backend/config.py`**: `LLM_BRAIN` (default on), `LLM_BRAIN_MAX_TOKENS=4000`,
  `LLM_BRAIN_TIMEOUT_SECONDS=60` (the brain's large output needs a longer read
  timeout than the 12s per-candidate thinker).
- **`backend/main.py`**: `run_tick(..., brain=)` runs the brain in live mode; each
  candidate uses the brain's verdict if it produced a valid one, else falls back to
  the per-candidate thinker. `_think_from_llm()` maps the reference `call:"buying"`→our
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
- New `tests/test_llm_brain.py` (23 tests): parse/validate (invented symbols and
  invalid calls dropped; malformed → None), call mapping (only `buying`→wants_entry),
  role routing (honest label, fallback labelled degraded, unsupported-model benches,
  timeout is NOT unsupported-model), wallet/snapshot builders (None-safe), and
  `LLMBrain.tick` fail-closed paths (mock hermetic, unparsable, empty candidates).
- New reuse regression tests (4): malformed/legacy prior fails closed, never raises.
- **Total: 289 passing** (was 262): backend 241, live_execution 48.

### Invariants held
The LLM remains a VETO/INPUT only — `wants_entry` is necessary, never sufficient;
entry still requires `gate.all_passed AND wants_entry`. The brain never opens,
closes, sizes, or touches execution. `PAPER_TRADING_ONLY=True` still hardcoded +
asserted; `live_execution/` untouched and disarmed. Mock mode is hermetic (brain
inert). Every degradation logged with a reason. Keys only in server `.env`.

(redaction verified in logs); no raw SQL outside api/db*.py.

## 22. Reference-parity roadmap batch 2 — capability gaps from the 2026-08-27 audit (✅ R8+R9 IMPLEMENTED §23 · ✅ R11 IMPLEMENTED §26 · R10 deferred-by-design)

Source: the feature-by-feature audit of this repo vs the reference repository
(2026-08-27). Batch 1 (§13, REF-R1–R7) is fully implemented. This batch records
the four capabilities the reference has that we still lack, each with a stable ID
(**REF-R8 … REF-R11**) and concrete implementation detail. R8 + R9 were built
(§23). **REF-R11 (on-chain precommit memo) was APPROVED by the operator on
2026-08-27 and IMPLEMENTED — see §26.** REF-R10 remains deferred-by-design: it
is a *promotion path* (devnet drill → human arming), not an implementation task.

Implementation order for the approved pair: **R8 first, then R9** (R9's
conviction factor multiplies the R8 risk budget, so R8's sizing surface must
exist first).

### REF-R8 — Drawdown-adaptive position sizing ✅ IMPLEMENTED (2026-08-27 — see §23)
- reference: `computeBudget()` in `src/lib/risk.server.ts`.
- Gap: our `compute_ticket()` (`backend/paper_trading_engine.py`) sizes from
  cash × crowd-heat conviction only. It never shrinks when the book is under
  water on open risk. The reference ticket is a function of EQUITY and of a
  drawdown factor derived from live unrealized P&L — a desk would impose
  exactly this, expressed as checkable arithmetic.
- Reference constants (keep identical for parity):
    - `PER_ORDER_FRACTION = 0.035` (3.5% of equity at full conviction)
    - `DAY_MULTIPLE = 4` (a day may contain 4 full-size tickets)
    - `HARD_ORDER_CEILING_USD = 3000`, `HARD_DAILY_CEILING_USD = 12000`
    - `MIN_TICKET_USD = 25` (we already have `config.MIN_TICKET_USD = 25.0`)
- Reference formula (port verbatim):
    - `open_drawdown_pct = min(0, unrealized_usd) / equity`  (0 if equity ≤ 0)
    - `drawdown_factor  = clamp(1 + open_drawdown_pct * 2.5, 0.5, 1.0)`
      → −20% of equity in open losses halves the ticket; flat/green = full size.
    - `max_order_usd = round(clamp(equity * 0.035 * drawdown_factor, 25, 3000))`
      (if equity ≤ 0 → `MIN_TICKET_USD`, fail CLOSED, never open)
    - `max_daily_usd = round(clamp(max_order_usd * 4, 25, 12000))`
- What to implement:
    1. New PURE function `compute_risk_budget(equity_usd, unrealized_usd) ->
       RiskBudget` in `paper_trading_engine.py` (or a new `risk_budget.py`),
       returning a dataclass `{equity_usd, drawdown_factor, max_order_usd,
       max_daily_usd, formula, derived}`. Pure + deterministic so the published
       numbers are recomputable from the same public inputs (reference invariant).
    2. Add a `SIZING_MODE = "risk_budget"` branch to `compute_ticket()` that
       returns `max_order_usd`. KEEP `"fixed"` and `"conviction"` untouched for
       calibration comparability — do not change their outputs.
    3. Thread equity + unrealized P&L to the sizing call site in `main.py` /
       `run_live_cycle.py`: equity = `PortfolioState.cash_usd + Σ position
       value`; unrealized = Σ `compute_unrealized_pnl(trade, current_price)`
       over open positions (already exists). If no readable book → pass
       `(0, 0)` so the budget fails closed to `MIN_TICKET_USD`.
    4. Config additions in `config.py`: `PER_ORDER_FRACTION`, `DAY_MULTIPLE`,
       `HARD_ORDER_CEILING_USD`, `HARD_DAILY_CEILING_USD` (mirror reference
       defaults). `MIN_TICKET_USD` already exists.
    5. Enforce the DAILY ceiling: track USD deployed this UTC day and refuse an
       entry that would push deployed-today past `max_daily_usd` (new guard in
       the entry path, fail-closed, logged). This is the half of the reference
       budget we do not enforce at all today.
    6. Surface the computed budget (equity, drawdown_factor, max_order,
       max_daily, formula) via `/api/disclosure.json` (REF-R6) so the numbers
       are public and reproducible.
- Fail-closed rules: non-finite/≤0 equity → `MIN_TICKET_USD`; any exception in
  the budget path → `MIN_TICKET_USD` (never raise into the tick); the model
  still decides WHETHER to enter, never the size (size stays pure code).
- Tests (`tests/test_risk_budget.py`): flat/green book → factor 1.0; −20% open
  loss → factor 0.5 and ticket halved; clamp at both ceilings and the floor;
  equity 0 / negative / NaN → `MIN_TICKET_USD`; daily-ceiling refusal; formula
  string is stable + recomputable. Hand-compute every expectation (defense-first
  rule 4).

### REF-R9 — Closed-loop learning → conviction factor ✅ IMPLEMENTED (2026-08-27 — see §23)
- reference: `computeCalibration()` in `src/lib/learn.server.ts`.
- Gap: our `learning_loop.py` already computes win rate / profit factor / max
  drawdown from closed trades, but only LOGS advisory recommendations — nothing
  that happened after a trade changes what the next trade is allowed to be. The
  loop is OPEN. The reference closes it: realized outcomes produce a single
  bounded conviction factor that MULTIPLIES the order size, so a run of losses
  shrinks the next ticket and a run of wins restores it.
- Reference formula (port verbatim), over closed outcomes `[{pnl_pct, ...}]`:
    - `win_rate = winners / usable`; `avg_win_pct`, `avg_loss_pct` per side
    - `expectancy_pct = win_rate * avg_win_pct + (1 - win_rate) * avg_loss_pct`
    - `raw = expectancy_pct >= 0 ? 1 + min(expectancy_pct/50, 0.2)
                                 : 1 + max(expectancy_pct/25, -0.4)`
      (+10% expectancy → +20% ticket; −10% expectancy → −40% ticket)
    - `confidence = min(usable_count / 12, 1.0)`  (small samples pulled to 1 so
      three trades cannot rewrite the book)
    - `conviction_factor = clamp(1 + (raw - 1) * confidence, 0.6, 1.2)`
    - No usable closed trades → `conviction_factor = 1.0` (FLAT, no adjustment)
- What to implement:
    1. New PURE function `compute_calibration(closed_trades) -> Calibration`
       (dataclass `{samples, wins, win_rate, avg_win_pct, avg_loss_pct,
       expectancy_pct, conviction_factor, formula}`), in `learning_loop.py` or a
       new `calibration.py`. Reuse `realized_pnl_pct` from closed `Trade` rows
       (`db.get_all_closed_trades` already feeds `run_daily_learning`).
    2. PERSIST the calibration so the public surfaces read the same numbers the
       sizing used: store it in the daily-stats row (`stats_json`) and/or a
       dedicated meta row, mirroring the reference writing to `omo_meta`.
    3. WIRE it into sizing: `final_ticket = risk_budget.max_order_usd *
       conviction_factor` (R8 × R9), still clamped to the hard ceilings and
       floored at `MIN_TICKET_USD`. This is the single feedback term that closes
       the loop.
    4. Surface `conviction_factor` + formula via `/api/disclosure.json`.
- Fail-closed rules: no/insufficient samples → 1.0 (never shrink or grow on
  thin data); factor hard-bounded [0.6, 1.2]; any exception → 1.0. This is
  arithmetic, NOT model output and NOT automatic threshold changes (those stay
  manual per §15 standing conditions).
- Tests (`tests/test_calibration.py`): FLAT → 1.0; a winning sample → factor >1
  capped at 1.2; a losing sample → factor <1 floored at 0.6; small-sample
  confidence pulls toward 1; hand-computed expectancy for a fixed fixture.

### REF-R10 — Live execution (the promotion path) ⏸ DEFERRED-BY-DESIGN — GATED
- reference: `execute.server.ts`, `solana.server.ts`, `wallet.server.ts` — the
  reference trades real funds.
- Status: this is a DELIBERATE capability gap, not a defect. We are paper-only
  by design (`PAPER_TRADING_ONLY=True` hardcoded + asserted at boot and inside
  every position-opening function). This entry documents the intended end-state
  and the gates that must be cleared BEFORE any arming work. It is NOT a
  "just implement it" task.
- Already built (do not rebuild): `live_execution/` package is wired but
  disarmed — `jupiter_executor.py` (quote/swap), `solana.py` (RPC + send with
  preflight ON, deliberately stricter than the reference `skipPreflight=True`),
  `wallet.py`, `commit_log.py` (seal-before-broadcast), `confirmation_queue.py`,
  `kill_switch.py`, `drill.py`. `promotion_gate.py` is a READ-ONLY go/no-go
  readiness checklist that can never itself trigger live trading.
- Gates that must ALL be cleared, in order, before arming is even discussed:
    1. `promotion_gate.py` checklist fully green (read-only evaluation).
    2. A funded THROWAWAY-KEYPAIR devnet drill passes end-to-end (quote →
       seal → broadcast → confirm → journal), per §15 "Not yet validated".
    3. A paper track record exists and is reviewed (win rate, drawdown, the R8/R9
       sizing behaving as specified under real outcomes).
    4. EXPLICIT operator approval to flip `PAPER_TRADING_ONLY` — this remains a
       separate, human-reviewed step and is never automated.
- What implementation would touch (only after the gates): arm the executor path
  in `run_live_cycle.py`, wire real wallet keypair handling (secrets stay in
  server env, never logged), and keep every deterministic rule / exit / kill
  switch authoritative over the live path exactly as over paper.
- Invariant that does not move: the LLM stays veto/input-only; deterministic
  rules + manual approval retain authority over every execution path.

### REF-R11 — On-chain precommit memo (commit–reveal) ✅ IMPLEMENTED (2026-08-27 — see §26)

> **STATUS: APPROVED by the operator (2026-08-27) and IMPLEMENTED in §26.**
> The operator's instruction to "implement the task… use omotrades/omo as
> reference" is the explicit sign-off this item required (§13 rule). It ships
> DISARMED — the memo path is unreachable until `LIVE_TRADING_ENABLED` is
> hand-flipped (see §27, the final task).

- reference: `precommit.server.ts` (~481 lines) — writes a precommit memo
  ON-CHAIN before a fill, then reveals it later, so the decision is timestamped
  on-chain ahead of execution.
- History: this was **REJECTED on 2026-08-26** (§13 "REJECTED / DEFERRED") on
  the rationale that local `CommitLog` seal-before-broadcast is a sufficient
  sealing mechanism while disarmed, and REF-R1 was scoped to work WITHOUT
  on-chain memos. It was re-opened by the 2026-08-27 audit, then **approved and
  implemented on 2026-08-27 (§26)**.
- What was delivered (full detail in §26): a memo write step that posts the
  decision hash on-chain BEFORE the fill (fail-closed — a memo that cannot be
  confirmed blocks the fill); `commit_log.py` carries the memo signature/slot so
  the local seal and the on-chain commitment stay one record; and a REF-R1-surface
  verifier (`/api/verify.json`) that re-checks the memo hash + slot ordering from
  public RPC.

### Batch-2 implementation order + standing conditions
- Built (paper-compatible, no arming involved): **REF-R8 → REF-R9** (§23), then
  **REF-R11** (§26, operator-approved 2026-08-27).
- Documented-only, gated: **REF-R10** (promotion path) — requires the operator's
  manual arming checklist; it is deliberately the LAST task (§27).
- Standing safety conditions from §15 apply unchanged: `PAPER_TRADING_ONLY=True`
  and `LIVE_TRADING_ENABLED=False` stay hardcoded; deterministic rules, exits,
  cash guards, kill switches, and manual approval retain authority over every
  execution path; no automatic threshold/prompt/model/live-trading promotion.



## 23. REF-R8 + REF-R9 — IMPLEMENTED: drawdown-adaptive risk budget × closed-loop conviction (2026-08-27)

The approved pair from §22 is built, tested, and live-verified. Reference parity
is verbatim against `computeBudget()` (`src/lib/risk.server.ts`) and
`computeCalibration()` (`src/lib/learn.server.ts`), re-fetched from the reference
repository at implementation time. REF-R10/R11 remain gated / documented-only.

### What was built
1. **config.py** — `SIZING_MODE` gains a third value `"risk_budget"` (default
   stays `"fixed"`: nothing changes live until an operator flips it). New
   HARDCODED reference constants (never env-overridable, same philosophy as
   SLIPPAGE/FEE): `PER_ORDER_FRACTION=0.035`, `DAY_MULTIPLE=4`,
   `HARD_ORDER_CEILING_USD=3000`, `HARD_DAILY_CEILING_USD=12000`.
2. **paper_trading_engine.py** — `RiskBudget` dataclass + pure
   `compute_risk_budget(equity, unrealized)` (verbatim port incl. the
   `MIN_TICKET_USD` clamp low bound and Math.round half-up parity via
   `_round_half_up`); `portfolio_equity_and_unrealized(portfolio, price_map)`
   (unpriced/degenerate marks held AT COST, never fabricated);
   `compute_ticket()` gains the `risk_budget` branch:
   `ticket = budget.max_order_usd × conviction_factor` clamped to
   `[MIN_TICKET_USD, HARD_ORDER_CEILING_USD]`. `fixed`/`conviction` outputs
   frozen for calibration comparability.
3. **calibration.py** (NEW) — pure `compute_calibration(closed_trades)`
   (verbatim port): winners pnl>0 / losers ≤0, expectancy, raw scale
   (+10% exp → +20% ticket, −10% exp → −40%), confidence `min(n/12, 1)`,
   factor clamped [0.6, 1.2]; no usable trades → FLAT 1.0; any exception → FLAT.
4. **api/db.py + api/db_pg.py** (lockstep) — `get_daily_stats()` +
   `patch_daily_stats()` (key-merge into `daily_stats.stats_json`; PG uses
   JSONB `||` merge; SQLite read-modify-write). No schema migration — reuses
   the existing table, mirroring the reference `omo_meta` pattern.
5. **main.py** — once per tick: price open positions (fail-soft per mint),
   equity/unrealized → budget + calibration → logged + persisted into today's
   daily-stats row (fail-soft, never kills the tick). Per candidate: fresh
   portfolio → equity recompute → `compute_ticket(..., conviction_factor)`;
   daily ceiling = derived `max_daily_usd` in risk_budget mode (static
   `DAILY_DEPLOY_CAP_USD` unchanged in other modes), refusal journaled.
6. **learning_loop.py** — computes + persists calibration into the daily stats
   (merges, never clobbers sibling keys), logs the factor — advisory only,
   never a threshold change.
7. **api/routes/disclosure.py** — `/api/disclosure.json` gains `risk_budget`
   + `calibration` blocks: persisted-first, recomputed-from-DB at cost-basis
   equity as fallback (no external calls), fail-closed minimums on any error.
8. **run_live_cycle.py** (still DISARMED) — `_manage` now records the freshest
   mark per mint; sizing uses equity/unrealized from the live ledger and, in
   risk_budget mode only, `budget × conviction` + derived daily ceiling vs
   `ledger.deployed_today_usd()`. Legacy cash-fraction path unchanged otherwise.

### Fail-closed invariants (all tested)
- Unreadable equity (0/negative/NaN/inf) or NaN unrealized → minimum-ticket
  budget, `derived=False`.
- Malformed conviction factor (None/NaN/≤0) → treated as 1.0.
- Unpriced marks → held at cost; degenerate positions → at cost, never crash.
- Budget/calibration persistence failure → logged, never kills a tick.
- Reference parity detail confirmed against source: the clamp LOW bound is
  `MIN_TICKET_USD` — a $1000 book at −20% open sizes $25 (floored), exactly as
  the reference does; the drawdown factor bites above ~$715 equity.

### Verification
- 42 new tests (`tests/test_risk_budget.py` 30, `tests/test_calibration.py` 12),
  every expectation hand-computed from the published formulas.
- Full combined suite: **331 passed** (backend 283 + live_execution 48).
- Isolation grep: no new backend→live_execution references (pre-existing
  sanctioned state-file reads only).
- Live smoke: backend restarted clean; `/api/disclosure.json` serves both new
  blocks; the in-process tick persisted a real budget (equity $991, df 1.0,
  max order $35, max daily $140) and FLAT calibration (0 closed trades) —
  persisted-first path proven end-to-end. 0 errors / 0 tracebacks in log.

### Status / next
- `SIZING_MODE="fixed"` remains the live default — R8/R9 are opt-in via a
  one-line config edit (operator decision, log it when flipped).
- R9's factor multiplies sizing in risk_budget mode; in fixed/conviction modes
  the factor is computed + published but not applied (frozen baseline).
- This builds the §22 gate-3 artifact: a paper track record where sizing
  behaves as specified under real outcomes. REF-R10/R11 stay gated.


## 24. Dead-provider fail-fast + reference fomo-path audit (2026-08-27)

### Problem (operator-reported: "ticks stall ~15 min; maybe Firecrawl credits?")
Live log confirmed TWO credit-exhausted providers (Firecrawl 402, ZenRows 402
— both correctly benched by the existing 402 handler) **plus** the real stall
source: **ScrapingBee ReadTimeouts were never benched.** `_scrape_get_template`
caught the timeout, logged it, returned None — and the next candidate tried it
again. ~20 candidates × 45s timeout ≈ 15 min of pure waiting per tick, forever.

### Reference audit (how the reference gets fomo data)
Verbatim from its source — nothing exotic, and we already had both paths:
- **Primary** = exactly ours: Privy refresh-token → bearer, then direct
  `fetch()` to `prod-api.fomo.family` (9s timeout, **2 attempts**, fail-soft).
- **Fallback** = a scraping API too, just behind their own gateway:
  `POST {OMO_CONNECTOR_GATEWAY_URL}/scrape` with
  `X-Connection-Api-Key: {FIRECRAWL_API_KEY}`, body
  `{url, formats:["rawHtml"], onlyMainContent:false, proxy:"stealth",
  headers:{...}}`, **25s timeout**. That is Firecrawl stealth-proxy — the
  identical payload our `_scrape_firecrawl` sends. Their gateway is a routing
  layer over the same Firecrawl key (same credits). **No free mechanism
  exists** — fomo firewalls datacenter IPs, so some residential/stealth proxy
  is the only way through when direct fails.
- **The real difference = timeout discipline.** Reference: one proxy hop, 25s,
  fails soft. Ours (before): 5-hop chain, 45s each, dead hops never benched.

### Fix (`backend/data_providers/crowd.py`)
1. **Transport-error benching** — new `_CONSECUTIVE_ERRORS` counter +
   `_transport_error()`/`_transport_success()`. A provider that fails
   `_TRANSPORT_ERROR_BENCH_AFTER=2` times **in a row** (timeout / connection
   error) is benched exactly like a 402; any completed response resets the
   streak. Generalizes fail-fast from "out of credits" to "unusable".
2. **`_scrape_firecrawl` wrapped in try/except** — it previously raised
   uncaught on transport errors (would crash the chain). Now counted + benched
   like every other hop.
3. **Timeout parity** — `_FIRECRAWL_TIMEOUT(45s)` → `_STEALTH_TIMEOUT(25s)`
   on both stealth paths (reference parity).
4. **Direct-path parity** — `_direct_get` now makes **2 transport attempts**
   (reference does 2 tries); a real HTTP response (even 403) is never retried.

### Verification
- 6 new tests in `tests/test_crowd.py` (23 total there): 2 consecutive
  timeouts → benched + skipped; single timeout → transient, not benched;
  success resets the streak; firecrawl transport error fails soft + benches;
  direct-get retries transport once but not a 403; stealth timeout == 25s.
- **Full combined suite: 337 passed** (backend 289 + live_execution 48).
- **Live-verified after restart:** ScrapingBee timeout #1 at 18:39:09 (25s
  budget, counted), timeout #2 at 18:39:42 → "2 consecutive transport errors —
  benching" → benched 30 min. Only **2** scrapingbee errors in the whole log;
  every later candidate skips it instantly. Crowd stage now degrades to proxy
  heat in **seconds**, not ~15 min. Firecrawl + ZenRows still 402-benched as
  before. 0 tracebacks.

### Status / next
- **Refilling Firecrawl credits is the only way to restore REAL crowd heat** —
  no code fixes an empty account. After a top-up the chain self-heals with
  zero changes (firecrawl is hop #1). ZenRows renewal is optional backup.
- ScrapOps 401 (key rejected) and the direct-403 firewall remain as-is; both
  fail fast and are harmless now.
- Jupiter `lite-api` 429s in the exit loop are a separate pre-existing
  rate-limit, unrelated to this fix.


## 25. Fresh scraper keys activated + ScrapingDog bearer-forwarding (2026-08-27)

### Operator action
New API keys added to the **repo-root `.env`** (the file `config.py`'s
`load_dotenv()` resolves by walking up from `backend/`) for **Firecrawl,
ScrapingBee, ScrapingDog, and ScrapeOps**. ZenRows key left unchanged (still
at its usage limit). Backend restarted to load the new keys and clear the
in-memory bench state.

### Code change — ScrapingDog now forwards the Privy bearer
`_scrape_scrapingdog` was wired into the chain but did NOT forward headers, so
even a fresh key could not read `prod-api` (which requires the Privy bearer).
ScrapingDog's docs confirm the mechanism is identical to ScrapeOps: enable
`custom_headers=true` and pass the headers on the request (no extra cost). So
the template now appends `&custom_headers=true` and passes
`fwd_headers=dict(headers)` — mirroring the ScrapeOps `keep_headers` pattern
that is verified live to carry the bearer through Cloudflare. +1 regression
test (`test_scrapingdog_forwards_privy_bearer`).

### Live verification (after restart)
- **Firecrawl (new key): 15× `200 OK`** — real crowd heat restored, primary path.
- **ScrapeOps (new key): 1× `200 OK`** — caught the one candidate where
  Firecrawl returned a transient `500`; the failover chain cascaded
  firecrawl→scrapingbee→scrapingdog→zenrows→scrapeops exactly as designed.
- **0 tracebacks**; no 15-min stalls (dead providers bench fast per §24).
- Direct `prod-api` GETs all `403` (Cloudflare firewall — expected).

### Per-provider status
| Provider | Key | Result | Note |
|---|---|---|---|
| Firecrawl | new | ✅ 200 OK | primary; occasional transient 500 (fails over) |
| ScrapingBee | new | ⚠ ReadTimeout | can't forward bearer anyway; benches after 2 |
| ScrapingDog | new | ⚠ 403 | `custom_headers=true` now wired; 403 = either the key's plan excludes the Web-Scraping API or the base proxy can't pass Cloudflare (may need premium proxy). Backup hop; fails soft |
| ZenRows | unchanged | ❌ 402 | usage limit; benched 30 min |
| ScrapeOps | new | ✅ 200 OK | verified failover |

### Status / next
- Real crowd heat is LIVE again via Firecrawl (+ ScrapeOps backup).
- If ScrapingDog's 403 persists and you want it as a usable hop: confirm the
  key includes the Web-Scraping API, or add a premium-proxy param (costs more


---

## 26. REF-R11 — On-chain precommit memo (commit–reveal) + micro-bootstrap ✅ IMPLEMENTED (2026-08-27)

Operator-approved 2026-08-27 (the instruction to implement against
`omotrades/omo` is the §13 sign-off). Reference sources: `precommit.server.ts`,
`verify.server.ts`. Ships **DISARMED** — the memo path is unreachable until
`LIVE_TRADING_ENABLED` is hand-flipped (§27). Backend stays paper-only; all
transaction construction lives in `live_execution/`.

### What was built
- `live_execution/memo.py` (NEW): `MEMO_PROGRAM_ID` (reference-parity SPL Memo
  constant), de-branded prefix `commit:v1:`, `build_memo_transaction()` (solders:
  one memo ix, payer = key 0, `VersionedTransaction`), and
  `publish_commit_memo()` (blockhash → sign → send → confirm across rotating
  RPCs). Any failure raises `MemoPublishError` — never a partial success.
- `live_execution/commit_log.py`: statuses `sealed → published → bound`; new
  `memo_signature`/`memo_slot`/`memo_published_at` fields + `record_memo()` and
  `fail()` (a skipped trade stays visible, never silently dropped).
- `live_execution/executor.py`: armed order flow is now
  `guards → confirm → wallet → SOL reserve → USDC funding → SEAL → publish memo
  → CONFIRM memo → quote → build → sign → send → confirm → bind`. The memo goes
  out **before** the quote so the quote→fill window stays as tight as ever.
  `OrderResult` carries `commit_hash/nonce/payload` + `memo_signature/memo_slot`
  so the bridge journals the exact seal.
- `live_execution/solana.py`: `get_usdc_balance()` (real on-chain USDC ATA
  balance; missing account = 0.0; unreadable = None → refuse).
- `run_live_cycle.py`: cash is now the REAL USDC balance (fail-closed to 0 on
  unreadable); filled live orders journal their seal + memo into
  `decision_commits` via `_journal_live_commit()`; live ticket floor applied.
- Verifier surface (REF-R1): `decision_commits` +`memo_signature`/`memo_slot`
  (SQLite + PG self-heal + `migrations/supabase/003_commit_memos.sql`),
  `bind_commit_memo()` + `get_commit_id_by_hash()` in both `db.py`/`db_pg.py`,
  and `/api/verify.json` memo checks (memo confirmed, on-chain hash matches the
  recomputed seal, memo slot < fill slot). RPC unavailable → `unknown`, never
  `pass`; no memo → `not_published` honestly. `/api/disclosure.json` gains a
  `commit_memo` block (program id, scheme, fail-closed semantics, fee model).
- `live_execution/drill.py`: devnet drill now also sends a REAL memo tx
  (airdrop-funded) — the memo path is exercised end-to-end before any arming.

### Micro-bootstrap accommodations (operator: start from $3–5 and compound)
- `live_execution/config.py`: `MIN_SOL_RESERVE` now env-tunable
  (`SOLANA_MIN_SOL_RESERVE`, default **0.01 SOL** — the old 0.05 floor would
  brick a 0.03–0.05 SOL fee wallet after one order); new hardcoded
  `MIN_LIVE_TICKET_USD = 0.5`.
- `paper_trading_engine.compute_ticket()` / `compute_risk_budget()` gained an
  optional `min_ticket_usd` floor (default None → `config.MIN_TICKET_USD`, so
  every paper expectation stays bit-identical). The live path threads
  `MIN_LIVE_TICKET_USD` through both. Without this, a $4 book in `risk_budget`
  mode would size $25 tickets forever and never trade.
- Funding model: **0.03 SOL = fee reserve** (memo 5,000 lamports + fill
  5,000 lamports per order; token-account rent ~0.002 SOL per new mint), and
  **$3–5 USDC = trading capital** (buys are USDC→token). The pre-commit USDC
  check blocks entries before any memo fee is spent when capital runs dry.

### Deliberate deviations from the reference (documented, not accidental)
1. **Fail-closed blocking** — a memo that cannot be confirmed BLOCKS the fill.
   The reference publishes asynchronously and shows un-publishable commits as
   "unpublished"; §22 req. 4 chose the stricter behavior.
2. **Immediate reveal** — payload+nonce are already public in the decision
   record; the ordering proof is the on-chain hash timestamp.
3. **Single signer** — the memo is signed by the configured trading wallet (no
   separate burner memo key at this book scale).
4. **De-branded prefix** `commit:v1:` (our scheme was never branded).

### Verification
- **41 new tests**, all offline/hermetic, hand-computed hash fixtures:
  `live_execution/tests/test_memo.py` (7), `test_commit_log.py` (6),
  `test_executor_memo_flow.py` (6, incl. memo-before-fill ordering + memo-
  failure-blocks-fill), `test_solana_usdc.py` (3); `backend/tests/
  test_ref_r11_memo_verify.py` (12), `test_live_ticket_floor.py` (7).
- Full suite: **379 passed** (baseline 338 + 41), ~2s. Isolation grep clean —
  backend's only `live_execution` references are function-local optional imports
  (same sanctioned pattern as REF-R1's `get_transaction`).
- Live smoke (disarmed): `/api/verify.json`, `/api/binding.json`,
  `/api/disclosure.json` all 200; `commit_memo.active=False`, `armed=False`,
  `paper_only=True`; 0 tracebacks.
- `solders` 0.29.0 installed into `.venv` (memo/drill tx construction). A latent
  `Hash.from_string` incompatibility in `drill.py` was found and fixed (drill had
  never run because solders was absent).

### Cost / performance (live path only; $0 while disarmed)
- +1 minimum-fee tx (5,000 lamports ≈ $0.002) per executed order; no rent on the
  memo (writes no state). +~3–8s ordering latency per order (memo must confirm
  before the fill) — irrelevant at one-decision-per-cycle cadence. Reliability is
  strictly lower only under degraded RPC (two landings required, fail-closed) —
  the explicit price of a tamper-proof on-chain ordering proof.

  credits/req). Not required — Firecrawl + ScrapeOps already cover it.
- ZenRows stays exhausted until renewed (optional).


---

## 27. FINAL TASK: Enable live execution — COMPLETED 2026-08-28 (§31 drill, §33 committed)

**Status: DONE.** The operator ran the devnet drill (5/5, §31), funded the
mainnet wallet, hand-edited the flags ARMED, supervised live cycles, and on
2026-08-28 explicitly directed that the armed state be committed and pushed
(§33). The sequencing rule below is retained as history: every other handoff
task completed FIRST; arming was the operator's last manual act; no session
performed it. This is REF-R10's promotion path — a human checklist, not code.

### Preconditions (all must be true before arming)
- [x] All preceding handoff tasks implemented, tested, committed.
- [x] **The §28 code queue (A7/A6/A3/A2/A4) shipped test-green.** (+A11, §30)
- [x] REF-R11 memo layer + micro-bootstrap shipped and test-green (§26).
- [x] Full suite passing (474); isolation grep clean.

### Operator checklist (human-only steps, in order)
1. **Fund the wallet:** 0.03 SOL (fee reserve) + $3–5 USDC (trading capital).
   The USDC transfer creates the needed associated token account.
2. **`.env`:** set `WALLET_KEYPAIR_PATH` (+ recommended `EXPECTED_WALLET_ADDRESS`
   identity pin). Optionally tune `SOLANA_MIN_SOL_RESERVE` (default 0.01).
3. **Devnet drill** with a throwaway funded keypair:
   `python run_live_cycle.py --drill` — must pass **including the new memo step**.
   ✅ **PASSED 2026-08-28** (record: §31) — 5/5 steps incl. the commit-memo,
   after two latent bugs were found and fixed (commit d8e426f).
4. **Hand-edit `live_execution/config.py`:** `LIVE_TRADING_ENABLED = True`, then
   `REQUIRE_MANUAL_CONFIRMATION = False` (autonomous cycles). Deliberately no env
   bypass exists for either flag.
5. **Supervise one cycle:** `python run_live_cycle.py --once` before continuous.

Steps 1–3 are done for the THROWAWAY DEVNET wallet (§31). For mainnet the
operator still owes: real wallet chosen + funded (step 1, mainnet), `.env`
re-pointed at it (step 2), then the two hand-edited flags (step 4) and the
supervised cycle (step 5). No session may perform steps 4–5.

### Budget facts to remember
- 0.03 SOL ≈ 200 memo+fill pairs or ~5–9 new token-account rents. The reserve
  floor (`SOLANA_MIN_SOL_RESERVE`, default 0.01) blocks entries fail-closed when
  the fee budget runs low. The USDC balance check blocks entries before any memo
  is published. `MIN_LIVE_TICKET_USD = 0.5`.

### Rollback
Set `LIVE_TRADING_ENABLED = False` again — one line, instant disarm. Open
positions remain managed/journalled. Backend is unaffected
(`PAPER_TRADING_ONLY` stays `True` always).


---

## 28. Tasks yet to be implemented (roadmap snapshot, 2026-08-27)

Reference parity for the originally-scoped items is COMPLETE (REF-R1–R9 + R11).
The 2026-08-27 omo audit (docs/09_OMO_AUDIT_COMPARISON.md) surfaced five further
parity gaps — **all
five shipped test-green on 2026-08-27 (implementation record: §29).** A same-day
re-read of the reference (full local clone) surfaced one module the original
audit missed — **A11 thesis re-authoring, shipped test-green 2026-08-27
(implementation record: §30).** Everything
else below is operator-gated, post-calibration, or needs external credits/keys.
Grouped by kind so a future session knows what is code, what is a human action,
and what is deliberately out of scope. **§27 (enable live execution) remains the
final task no matter what.**

### Code queue — COMPLETE (from the omo audit; record in §29, A11 in §30)

- [x] **A7 — Wash-trade "fake chart" filter** — `backend/rule_engine/fake_chart.py`
      (all 13 omo thresholds), applied in the READ stage before think/gate.
- [x] **A6 — Hardcoded symbol blocklist** — `BLOCKED_SYMBOLS` +
      `is_blocked_symbol()` in `blocklist.py`, enforced in `filter_candidates()`.
- [x] **A3 — Venue attribution** — `live_execution/venue.py`, journaled to
      `decision_commits.venue`, surfaced in /api/binding.json.
- [x] **A2 — Chain book reconciliation** — `live_execution/reconcile.py` +
      `solana.get_token_balances()`, runs every live cycle; journal never mutated.
- [x] **A4 — Own-basis read-back** — `crowd.read_own_basis()` +
      `FOMO_OWN_HANDLE`, cross-checked against journal cost each live cycle.
- [x] **A11 — Thesis re-authoring** — `backend/thesis_restate.py` (§30), advances
      stale open write-ups each tick/cycle. Found in the 2026-08-27 re-read.

### A. Final gating task — operator-only, NO code (handoff §27) — STILL LAST
- [ ] **REF-R10 — Enable live execution.** The promotion path. Fund the wallet
      (0.03 SOL fee reserve + $3–5 USDC capital) → funded throwaway-keypair
      devnet drill (now incl. the memo step) → hand-flip
      `LIVE_TRADING_ENABLED=True` then `REQUIRE_MANUAL_CONFIRMATION=False` →
      supervise one `--once` cycle. Deliberately the LAST task; no session may
      arm before every other task is done — **including the entire code queue
      above (A7/A6/A3/A2/A4) shipping test-green first.**

### B. In progress
- [ ] **Calibration window (~10 days).** Let the paper book run; review daily
      via dashboard + learning-loop logs; tune ONE threshold at a time from
      rejection-breakdown evidence (manual edits to `config.py`). Regime/rule
      thresholds are placeholders until this completes.

### C. Post-calibration (deliberately unbuilt until then)
- [ ] **E8/E9 partial scaling + rolling history.**
- [ ] **D7 advisory LLM layer** (advisory only — never decision authority).
- [ ] **`crowd_heat` rule** — needs a fomo-index data source first.
- [ ] **Full multi-wallet management.**

### D. Optional upgrades (need operator action / credits / keys, not code-only)
- [ ] **Birdeye paid tier with `token_security`** — free tier 401s, so security
      fields are UNKNOWN; upgrading auto re-enables on restart.
- [ ] **A fomo-index source** to power the `crowd_heat`-style rule.
- [ ] **ZenRows renewal** — 402 credit-exhausted (optional backup scraper).
- [ ] **ScrapingDog 403** — confirm the key includes the Web-Scraping API or add
      a premium-proxy param (optional backup; Firecrawl + ScrapeOps already cover it).

### E. Deferred pending a live track record
- [ ] **Off-book multi-chain tracking** (BNB/other-chain positions marked and
      repriced into equity) — revisit only after the Solana side is armed and
      has a live track record.

### Never to be built (invariants, not tasks)
- Automatic learning, automatic threshold/prompt/model changes, or any
  automatic live-trading promotion (§15 / §8 rule 6). The LLM stays veto/input
  only; deterministic code decides. Arming is always a manual human act.

---

## 29. omo-audit code queue — implementation record (2026-08-27)

All five audit gaps implemented, tested (444 passing, baseline was 379), and
shipped DISARMED. No hardcoded safety flag was touched; arming stays §27.

### A7 — wash-trade "fake chart" filter
- `backend/rule_engine/fake_chart.py`: verbatim port of omo's `isFakeChart`
  (src/lib/market.server.ts) — all 13 thresholds: lifetime-fees-vs-fdv (<3%),
  fresh-launch low-float (<$150k fdv + <$2k fees), vol-vs-depth (1h >20× liq,
  24h >150×), thin-crowd (>$50k vol with <60 trades), fat-ticket (avg >$2.5k
  on <$150k depth), one-sided (>40 trades, zero buys or sells), straight-bleed
  (−25% 1h & −40% 6h; −55% 24h & −20% 6h), dead-tape (1h <0.15× liq & 24h <3×;
  0 5m-vol & <$5k 1h), headline-day-empty-present (6h/24h <6%), paper-float
  (fdv/liq >30).
- `Candidate.volume_5m_usd` added; dexscreener/discovery providers fill it.
- Applied in `main.run_tick` READ stage BEFORE think/gate — filtered rows burn
  no LLM or scrape credits. Filtered count is logged per tick.
- Deviations from omo: thresholds requiring `ageHours`/`fdv` skip (not fail)
  when the field is unknown — our providers don't always return them, and
  failing closed on missing optional data would starve the candidate pool;
  the numeric gate rules still apply afterwards.
- Tests: `backend/tests/test_fake_chart.py` — hand-computed cases for all 13
  thresholds + unknown-field skips.

### A6 — hardcoded symbol blocklist
- `backend/blocklist.py`: `BLOCKED_SYMBOLS` frozenset (omo's exact list:
  404/404LIFE/404LIFENOTFOUND/WAWA/POOPHORSI/MACI/SHEEP/BIST/KIO/KIONGAZI/
  CRASHIUS/HANDSEM/BASECAT/ZOE) + `^404` prefix rule + `is_blocked_symbol()`
  (normalizes `$`, whitespace, case — reference parity with blocklist.ts).
- Enforced in `filter_candidates()` alongside the mint blocklist, i.e. BEFORE
  think/enrichment. A rugged name re-launched under a fresh mint is caught.
- Tests: extended `backend/tests/test_churn_guards.py`.

### A3 — venue attribution (live path)
- `live_execution/venue.py`: `fill_venue_from_tx()` (pure parser) +
  `fetch_fill_venue(signature)` (fail-soft RPC wrapper). Labels the executing
  program from top-level instructions → inner instructions → account keys:
  pump.fun bonding curve/amm, jupiter router (v4/v6), raydium (+clmm), orca
  whirlpool, meteora dlmm; unknown routers are named `program XXXX…YYYY`,
  never guessed. OBSERVABILITY ONLY — can never block or alter an order.
- `decision_commits.venue` column (SQLite schema + idempotent ALTER, db_pg
  self-heal, `migrations/supabase/004_fill_venue.sql`), `bind_commit_venue()`
  in both db layers; `run_live_cycle` attributes after each filled order;
  `/api/binding.json` surfaces `venue` on every pair (null until bound).
- Tests: `live_execution/tests/test_venue.py` — mocked jsonParsed txs per venue.

### A2 — chain book reconciliation (live path)
- `live_execution/solana.py::get_token_balances()`: `getTokenAccountsByOwner`
  across BOTH token programs (legacy + token-2022); `{}` = answered-empty,
  `None` = unreadable (unknown is never empty).
- `live_execution/reconcile.py::reconcile()`: pure cross-check of journal
  positions vs chain balances. Chain is the sole authority on HOW MANY tokens
  we hold; the journal stays the sole authority for cost basis. Verdicts:
  match (tolerance 1e-6) → nothing; chain < journal → exit sizing clamped to
  chain truth; chain = 0 → position excluded from the book this cycle +
  flagged for operator review; chain > journal → keep journal (conservative);
  unjournaled on-chain mints → flagged, NEVER added (no fabricated basis).
  The ledger is NEVER mutated by a chain read — a lying RPC cannot corrupt
  the money journal. Every disagreement logged loudly + reported in the cycle
  outcome (`chain_reconciliation`).
- Deliberate deviation from omo: they re-derive the whole book from chain +
  tx history each sync; we keep the atomic §5.1 journal as money authority and
  use the chain as a loud cross-check + sizing clamp. Safer at this scale.
- Tests: `live_execution/tests/test_reconcile.py` (10),
  `live_execution/tests/test_token_balances.py` (7).

### A4 — own-basis read-back
- `backend/config.py::FOMO_OWN_HANDLE` (env, default "" = disabled). No new
  secret — reuses the existing Privy session chain.
- `crowd.py`: `fetch_fomo_theses` refactored onto a shared cached
  `_thesis_payload()` (contract unchanged); new `read_own_basis(picks)` finds
  the bot's own `authorTrade` on the raw board (case/@-insensitive, no
  substantive filter — our own short thesis still carries valid accounting),
  returns invested = max(0, value − unrealized) per reference `readOwnBasis`,
  capped at 10 mints.
- `run_live_cycle::_crosscheck_basis()`: each live cycle compares FOMO's
  invested figure against the journal cost (tolerance max(5%, $0.50));
  mismatches logged loudly + reported in the outcome (`basis_crosscheck`).
  OBSERVABILITY ONLY — the journal is never modified by FOMO's numbers.
- Tests: `backend/tests/test_own_basis.py` (9).

### Verification
- Full suite: **444 passed** (was 379 before the audit batch), 0 failures.
- Isolation grep clean: backend references to live_execution remain only the
  sanctioned function-local optional imports (proof.py, disclosure.py) + state
  file paths.
- Live smoke (disarmed): /api/verify.json, /api/binding.json,
  /api/disclosure.json, /api/proof.json all 200; binding pairs carry
  `venue: null`; `armed=false`, `paper_only=true`; 0 tracebacks.


## 30. A11 — thesis re-authoring (2026-08-27)

The 2026-08-27 re-read of `omotrades/omo` (full local clone, commit 48a86f9 —
unchanged since the audit) found one module the original audit's read list
missed: `src/lib/thesis-author.server.ts` (`restateTheses`). A write-up typed
once at entry and never touched again is a static string with extra steps;
the reference walks the open book on the live cadence and has the reasoning
model rewrite any write-up that is stale or not model-authored, against the
position's CURRENT numbers. Implemented same day under the standing
"implement against omotrades/omo" instruction.

### Semantics (reference parity)
- Due = open row (`closed_at IS NULL`) whose `updated_at` is older than
  `THESIS_RESTATE_STALE_HOURS` (6.0, reference STALE_MS), OR whose author is
  not `model*`, OR whose `updated_at` does not parse (fail toward refreshing —
  reference `isStale()` treats unparseable as stale).
- At most `THESIS_RESTATE_PER_PASS` (2) rows per pass, oldest text first, so
  a tick never turns into a batch job. Both constants hardcoded in
  `backend/config.py` (cadence knobs of a narrative-only job; not
  env-overridable, same philosophy as the sizing constants).
- Rewrite contract: under 60 words; why the position is still on; what
  changed since entry; the single condition that takes it out; advance the
  argument, never restate. Output validated fail-closed: <20 chars or >1000
  chars is REJECTED — old text kept, refusal logged.
- NARRATIVE ONLY: the pass can only ever change `theses.thesis` /
  `theses.author` / `theses.updated_at`. It never touches trades, cash,
  sizing, exits, or verdicts — size, P&L, and retirement come from the
  journal. The DB write itself is guarded by `closed_at IS NULL`, so a row
  retired mid-pass is never rewritten (rowcount 0 → skip, logged).

### Implementation
- `backend/thesis_restate.py` — pure helpers (`parse_ts`, `is_due`,
  `select_due`, `validate_restatement`, `position_numbers`, `build_brief`)
  + `restate_theses(conn, positions, price_map)` (never raises). Main
  provider via `build_main_client().complete_json(json_mode=False,
  task="thesis_restate")`; every call accounted in `llm_call_usage`
  (success AND degradation); each rewrite journaled as a `did` event
  (`action: thesis_restate`). DeepSeek peak-window skip (docs/08 §5 —
  non-urgent work); mock mode is a deterministic no-op.
- DB layer (lockstep, no raw SQL outside api/db*.py): `get_open_theses()`
  + `update_thesis_text()` in BOTH `api/db.py` and `api/db_pg.py`.
- Wiring: `main.py run_tick` — one pass after the risk-budget block,
  reusing that block's `price_map` + open positions (ZERO extra network
  I/O; documented deviation from the reference's per-row tape fetch).
  `run_live_cycle.py` — same pass after `_manage` re-prices the live book
  (marks from the cycle), reported in the cycle outcome as
  `thesis_restatements`.
- Surface: `/api/theses.json` already exposes author/updated_at (restated
  rows are publicly visible); `/api/disclosure.json` gains a
  `thesis_restatement` block (stale_hours, per_pass, scope).

### Verification
- 26 new tests (`backend/tests/test_thesis_restate.py`): selection math,
  validation bounds, hand-computed P&L reuse, both DB behaviors incl. the
  retired-mid-pass guard, PG surface parity, and mocked-HTTP orchestration
  (write/refuse/fail-closed/peak-skip/cap/never-raises). Full suite:
  **470 passed** (was 444), 0 failures.
- Isolation grep clean (no new backend→live_execution references).
- Live smoke: first tick after restart advanced BOTH stale open write-ups
  (aura +3.5% mark; ANSEM −8.1% with a tightened invalidation) via
  `model:deepseek:deepseek-v4-flash`, journaled two `did` events, skipped
  the retired row; all proof endpoints 200; `armed=false`; 0 tracebacks.


## 31. §27 pre-flight + DEVNET DRILL PASSED (2026-08-28)

The operator began the §27 promotion path. A session-assisted pre-flight ran
everything up to (but NOT including) the two human-only flag edits. Live
execution remains DISARMED — `LIVE_TRADING_ENABLED=False` and
`REQUIRE_MANUAL_CONFIRMATION=True` were untouched throughout.

### Pre-flight (all verified 2026-08-28)
- Arm flags confirmed disarmed; kill switch not engaged; confirm CLI works
  ("no active confirmations"); state dir writable; solders 0.29.0 present.
- Devnet RPC + the configured mainnet `SOLANA_RPC_URL` both reachable.
- Throwaway drill keypair generated (solders byte-array JSON) at
  `~/.config/solana/drill-keypair.json`; `.env` carries `WALLET_KEYPAIR_PATH`
  + `EXPECTED_WALLET_ADDRESS` (identity pin). A stale empty duplicate
  `WALLET_KEYPAIR_PATH=` template line was removed from `.env` (dotenv
  last-wins verified before and after).
- Identity pin verified through the project's own `wallet.load_keypair` +
  `verify_expected_address`.

### Two latent bugs found by the first REAL keypair load (commit d8e426f)
Both fail-closed (nothing dangerous), both invisible to the mocked suite,
both would have blocked arming day:
1. `wallet.load_keypair` passed the file **path** to solders
   `Keypair.from_json`, which expects JSON **content** — every real load
   refused with "expected value at line 1 column 1". Fixed to
   `Keypair.from_bytes` on the already-validated array (no re-read after
   validation) + explicit exactly-64-u8 check for a clear refusal reason.
2. `drill.py` used a `log` that was never defined (NameError on step 1), and
   `run_live_cycle.py` ran `--drill` before `logging.basicConfig`. Both fixed.
+4 regression tests incl. the missing success path (real keypair file → real
solders load → pubkey round-trip) and pin match/mismatch → **474 passing**.

### Drill result (devnet, throwaway wallet funded via faucet.solana.com)
```
PASS wallet:         identity pin verified
PASS devnet-rpc:     balance 1.000000 SOL
PASS chain-decimals: SOL mint decimals=9
PASS confirm:        real signed dust transfer broadcast + confirmed (slot 489023339)
PASS commit-memo:    REF-R11 publish_commit_memo end-to-end (slot 489023363)
DRILL COMPLETE: 5 of 5 steps passed, exit 0
```
Note: the RPC `requestAirdrop` faucet was at its daily limit (tried 4 amounts
× 2 endpoints, all 429); the web faucet is the working route.

### Remaining for the operator (human-only, in order)
1. Choose the MAINNET trading wallet (reuse this keypair funded on mainnet,
   or a fresh/exported one — exported wallet keys may need conversion to the
   64-byte JSON array format `Keypair.from_json` content expects).
2. Fund it on mainnet: 0.03 SOL + $3–5 USDC (USDC transfer creates the ATA).
3. Point `.env` `WALLET_KEYPAIR_PATH` + `EXPECTED_WALLET_ADDRESS` at it.
4. Hand-edit `live_execution/config.py`: `LIVE_TRADING_ENABLED = True`
   (later optionally `REQUIRE_MANUAL_CONFIRMATION = False`).
5. Supervise `python run_live_cycle.py --once` before continuous running.
Rollback stays one line: `LIVE_TRADING_ENABLED = False`.


## 32. Cash-corruption incident + bad-quote guards + final omo audit (2026-08-28)

### The incident (paper book)
After a `neet` close the dashboard showed an outrageous cash balance.
Forensics: a transient bad Jupiter quote priced the ~$0.04 token at
$119.0648 (~2,960×). The 15s exit scanner ratcheted high-water on the
poisoned mark, a take-profit trim fired against it, and `close/trim`
credited `price × quantity` ≈ **$94k of phantom cash** into the paper
accumulator. Root cause class: *exit math trusted a single unbounded price
sample for a money write*.

### The fix (two hardcoded, fail-closed guards — `backend/config.py`)
- `EXIT_PRICE_JUMP_MAX = 50.0` — scan-level: a single exit-scan price this
  many multiples ABOVE the position's established peak is a bad quote, not
  a move: skip the position this scan and do NOT ratchet high-water.
  Upward-only on purpose — a genuine collapse must still be able to exit.
- `MAX_EXIT_PROCEEDS_MULT = 200.0` — backstop in `close_position` AND
  `trim_position`: a single exit crediting more than 200× cost basis is
  refused BEFORE any state write (cash can never be corrupted even if a bad
  price reaches the exit math).
- Both deliberately generous: they only ever trip on data errors, never on
  real market moves. The corrupted book's cash was repaired to the true
  accumulator value (script-run, one-off, journaled in this record).
- Tests: `backend/tests/test_exit_price_guards.py` (9) — jump skip + no
  ratchet, legitimate moves pass, collapse still exits, proceeds backstop
  refuses on close and trim, guard boundaries.

### Live parity (same class of harm, different shape)
A live sell can never FABRICATE money — it is a real swap and cash is chain
truth (below) — but an early exit on a phantom spike is still real harm.
`run_live_cycle._manage` got the identical jump guard (skip cycle, high-water
untouched, upward-only). Tests: `live_execution/tests/test_manage_jump_guard.py`
(3), built on the exact incident numbers ($0.04022 entry, $119.0648 quote).

### Why live cash is accurate by construction (operator question)
- Live "cash" is NOT an accumulator: every cycle `_live_portfolio()` reads
  the wallet's REAL on-chain USDC balance (`solana.get_usdc_balance` →
  `getTokenAccountBalance` on the USDC ATA; missing account = 0.0;
  unreadable = None → cash 0, no entries, orders refused at the executor).
- Live proceeds are real fills — what the chain actually credits, journaled
  from the fill result. No simulated mark ever touches cash; a bad quote can
  at worst trigger an early exit (now guarded), never phantom money.
- A2 chain reconciliation cross-checks token quantities against the chain
  every cycle. The dashboard cash display is the PAPER book; the live book's
  truth is the wallet's on-chain USDC balance (any explorer).

### Final omo audit (docs/09 §F)
Full-coverage re-read of `omotrades/omo` (still commit 48a86f9 — unchanged).
Closed all standing open questions: (1) `exit.server.ts` EXISTS but is
unpublished — proven by their own `exit-rules.test.ts` importing it; their
published repo cannot run its own tests, and the test pins an exit contract
identical to our public engine; (2) their calibration factor is STILL not
wired into sizing (final grep: `convictionFactor` lives only in
`learn.server.ts`); (3) wash-trade filter applied at `market.server.ts:237`
— parity with our A7. Remaining deltas are UX polish (narration dedupe —
queued), scale custody (memo burner key — documented deviation), and hosting
plumbing. **No trading-critical parity gap remains.**

### Arming state (operator-executed; ARMED state committed 2026-08-28 — see §33)
The operator performed the §27 human-only steps on this machine:
`live_execution/config.py` now carries `LIVE_TRADING_ENABLED = True` and
`REQUIRE_MANUAL_CONFIRMATION = False` (hand-edited, as designed — no env
bypass exists). SUPERSEDED 2026-08-28: the operator explicitly directed that
this armed state be committed and pushed (§33); the canary test was
re-purposed to pin the committed state. Suite: **486 tests, all passing**.
Rollback is still one line: `LIVE_TRADING_ENABLED = False`.

## 33. Armed state committed and pushed (2026-08-28, operator-directed)

After §31 (devnet drill 5/5), §27's human steps, and supervised live cycles,
the operator explicitly directed: "push config as armed, no questions asked".
Done, exactly as directed:

- **Committed**: `live_execution/config.py` with `LIVE_TRADING_ENABLED=True`,
  `REQUIRE_MANUAL_CONFIRMATION=False` (the operator's own hand-edit; the
  diff contained no secrets — verified by scan; all keys live in the
  gitignored `.env`).
- **Canary re-purposed**: `test_safety_flags_are_hardcoded_safe_defaults` →
  `test_safety_flags_match_the_committed_state` — now pins the COMMITTED flag
  state, so any silent flip in either direction fails loudly. Suite fully
  green: **486 passing**.
- **Disclosure truthfulness fix**: `/api/disclosure.json`'s `armed` field
  previously read a nonexistent backend-config attribute (always False — it
  would have lied "disarmed" while armed). It now reads the real
  `live_execution` flag via the sanctioned function-local optional import
  (fail-closed False if the package is absent). Test updated accordingly.
- **Docs aligned**: config.py header, README live-trading section (incl.
  "if you clone this and don't want real trading, flip the flag" warning),
  handoff §1/§3/§27/§32, memory-bank, project report.
- **Unchanged**: no env bypass exists; kill switch, daily-loss breaker,
  caps, identity pin, SOL reserve, memo-before-fill all active; rollback is
  one line (`LIVE_TRADING_ENABLED = False`).
- **Honest trade-off on record**: a fresh clone of this repo is now armed by
  default. It still cannot trade without a funded wallet keypair + RPC config
  in `.env` (all gitignored), but anyone cloning should read the README
  warning first. This was the operator's explicit, informed choice.


## 34. Live-cycle hardening: 403-rejection benching + micro-bootstrap cash rule (2026-08-28)

Two operator-reported issues with the now-ARMED live cycle, both fixed and
live-verified the same day.

### Issue 1 — dead stealth scrapers re-tried every candidate, every tick
Operator: "if Firecrawl credits are finished, there's still ScrapingBee and
ScrapingDog tokens left." Reality in the logs: Firecrawl (402 credits) and
ZenRows (402 credits) bench correctly, but **ScrapingDog's proxy gets refused
by the fomo.fun origin (HTTP 403 — it can't pass that endpoint's Cloudflare
even with forwarded headers)** and was re-tried on every candidate (one wasted
request + ~2-3s latency each, every tick), and ScrapingBee was ReadTimeout-ing.
Only ScrapeOps actually gets through.

**Fix** (`backend/data_providers/crowd.py`): added an origin-rejection streak
counter (`_CONSECUTIVE_REJECTIONS`) mirroring the transport-error counter — two
consecutive 403s bench a provider for 30 min exactly like a 402. Kept in its
own counter because `_transport_success` resets the transport streak on any
completed response (a 403 IS a completed response). A 200 resets the rejection
streak, so a provider that recovers is used again.

**Live proof:** after restart, `scrapingdog: 2 consecutive origin rejections
(403) — benching`, called exactly 2× then skipped; ScrapeOps served all 20
candidates. The chain now converges on the working provider instead of burning
calls on dead ones. (Note: ScrapingBee/ScrapingDog tokens "being left" doesn't
help here — their proxies genuinely can't reach this endpoint right now; the
fix stops wasting calls on them, it can't make them work.)

### Issue 2 — $5 book refused every entry (cash rule sized for the paper book)
Operator: "when $5 is all the cash, if it passes on tokens it should buy as
well — it's not working as intended." Root cause: the paper `cash_available`
rule checks cash against `INTENDED_POSITION_SIZE_USD` ($100 — sized for the
$1,000 paper book). The live book starts from a few USDC (REF-R11
micro-bootstrap) and sizes from `MIN_LIVE_TICKET_USD` ($0.50), so the paper
threshold refused EVERY live entry before sizing even ran.

**Fix** (`run_live_cycle.py`): `LIVE_ACTIVE_RULES` — the paper `ACTIVE_RULES`
with exactly one swap: `cash_available` → `_live_cash_available`, which checks
`cash_usd >= MIN_LIVE_TICKET_USD`. Every other rule stays verbatim; paper
`ACTIVE_RULES` + `INTENDED_POSITION_SIZE_USD` are untouched (calibration-frozen)
— the same "paper frozen, live threads its own floor" pattern as
`compute_ticket(min_ticket_usd=...)`. `run_cycle` now evaluates
`LIVE_ACTIVE_RULES`.

**Live proof:** after restart, no candidate shows `cash_available` in its
failed list; several show `gate=PASS`. With `SIZING_MODE=fixed` a $5 book sizes
`min(5×0.15, 150) = $0.75` ≥ the $0.50 floor, so a model "buy" + gate pass now
places a micro-order. (Current candidates are being refused because the model
returns verdict "pass", not "buy" — DeepSeek 200 OK on every think call, no
degradation; that is the model veto working as designed, not a bug.)

### Verification
- **11 new tests → 498 combined passing** (was 486): 4 in
  `backend/tests/test_crowd.py` (two-403s-bench, single-403-transient,
  200-resets-streak, transport/rejection counters independent) + 7 in
  `live_execution/tests/test_live_cash_rule.py` (only-cash-rule-swapped,
  paper-rule-frozen, live-floor pass/fail, gate-outcome-flips,
  run_cycle-uses-live-rules).
- Isolation unchanged; no new backend→live_execution references (the live rule
  lives in the root bridge `run_live_cycle.py`, which already imports both).
- Live smoke (ARMED): system-status / live/portfolio / disclosure.json all 200;
  both fixes observed in `logs/live_cycle.log` on the first cycle after restart.


## 35. Frontend rebuild — terminal design system + Playwright E2E (2026-08-28)

Operator-directed: "rebuild the frontend using all .clinerules skills,
especially awesome-design-skills; install and use Playwright; it must be
completely functional, fully wired, and not look vibecoded."

### Design system (`frontend/DESIGN.md` — new source of truth)
Synthesized from the awesome-design-skills pack (mono + sleek + impeccable),
held to defense-first (never invent values) and performance-discipline (no
new runtime deps): dark high-contrast terminal; token-only colors
(`tailwind.config.js` — surface ladder ink/panel/raised/line, text ladder
bright/body/dim/faint, semantics pos/neg/warn/info); JetBrains Mono for all
data (tabular-nums everywhere), Inter for labels; flat 6px panels, no
shadows; five required states (loading skeletons / explicit empty / error /
global offline banner / stale); a11y gates (aria-expanded rows, keyboard
operability, copy announce, never color-only meaning).

### What shipped
- `src/lib/format.ts` — verbatim-value formatters (signed money `+$/−$`,
  small-price precision, em-dash for null; NO client-side money math).
- `src/components/ui.tsx` — shared primitives (Panel/Stat/Badge/Skeleton/
  Empty/ErrorState) so every panel implements the required states the same way.
- Rebuilt all four live panels: `LiveBook` (headline real-money equity strip +
  positions table), `LiveFeed` (accessible expand/collapse rows, contract
  copy, verbatim model answer, rule breakdown), `MarketRegimePanel`,
  `SystemStatus` (now shows the real reasoning model + recent LLM calls from
  `llm_usage_recent`).
- Feed history: `useWebSocket` hydrates the last 50 decisions from
  `/api/feed?limit=50` on mount, then live-appends over `/ws/feed` (deduped by
  id, newest-first) — the dashboard no longer starts blank after a reload.
- Old `term-*` token system fully retired (grep: 0 refs; 0 hex literals
  outside the token file). Fonts self-hosted via @fontsource-variable.

### Playwright E2E (`npm run test:e2e` — 5 passing)
Zero console errors on load; every panel reaches data or an explicit empty
state (never blank, no stuck skeletons); feed rows expand/collapse with
`aria-expanded` and are Enter-key operable; offline banner appears when the
API is unreachable (route-abort). Config pins the already-running backend on
:8000 (it serves `frontend/dist`); `frontend/test-results/` gitignored.

### Found + fixed while wiring: STATE_DIR empty-env bug
`LIVE_EXECUTION_STATE_DIR=` (empty) in `.env` made `os.getenv(..., default)`
return `""` → `Path("")` = CWD → the live CommitLedger (`commits.json`, real
order nonces) was written to the REPO ROOT — one `git add -A` from being
published. Fixed: empty value now falls back to `live_execution/state/`
(`+3` tests in `live_execution/tests/test_state_dir.py` incl. override-wins);
stray ledger moved into `live_execution/state/`; `/commits.json` +
Playwright artifacts added to `.gitignore`; app restarted clean.

### Verification
- `npm run build` clean (tsc strict + vite); **Playwright 5/5 passing**;
  **pytest 501 passing** (backend 385 + live_execution 116, +3 state-dir).
- Screenshot review of the running dashboard (live book $ figures, feed with
  expanded row, regime BAD/OK column, system status) — layout is calm, dense,
  and terminal-true.
- Live smoke after restart: system-status / live/portfolio / feed /
  market-regime all 200; no `commits.json` at repo root.

## 36. Live execution UNBLOCKED + Journal/Holdings pages restored (2026-08-28)

Operator report: "the bot says enter but it doesn't execute any transaction"
+ "the journal page and the other page are gone". Root-caused and fixed the
three stacked bugs that blocked EVERY armed order, then restored the two
missing pages as live-only views.

**The three execution blockers (all in `live_execution/`):**
1. **Quote verb bug** — `get_jupiter_quote` POSTed to
   `https://lite-api.jup.ag/swap/v1/quote`, which is a **GET** endpoint →
   405 ×3 retries → `ExecutionError`. (The paper side always GETs it; the
   live module docstring even says so.) Fixed: new `_get_json` helper
   (GET-with-query-params twin of `_post_json`, same 3-attempt/429
   semantics); the buy quote uses it. The **sell path** built its own inline
   quote with `_post_json` too — same 405 — switched to `_get_json`.
2. **`NameError: ExecutionError`** — `executor.py` caught `ExecutionError`
   in four `except` clauses but never imported it, so the FIRST quote
   failure crashed the whole cycle instead of returning `status="failed"`.
   Fixed: imported; `_post_json` import dropped (unused after #1).
3. **`VersionedTransaction.deserialize` doesn't exist** — solders 0.29's
   parse constructor is `from_bytes`. The first order that survived the
   quote (GTA6, 16:22 — quote 200 OK, swap build 200 OK) died at signing.
   Fixed in `_sign_transaction`; the drill/memo paths were never affected
   (they build from a `Message`, they never parse Jupiter bytes).

**Hardening (defense-first, all fail-closed + journalled):**
- Every post-memo failure phase in `place_buy`/`place_sell` now calls
  `logc.fail(sealed_hash, reason)` — quote refused/failed/crashed, impact
  floor, build/sign/broadcast error, unconfirmed fill. The CommitLog's own
  contract: "a skipped trade must be as visible as an executed one." Before
  this, a failed fill left the commit stuck at `published` forever with no
  explanation — exactly the "says enter but nothing happens" opacity.
- The build/sign/broadcast phase now catches `Exception` (not just
  `ExecutionError`): no network-phase error can crash the cycle again.

**Live proof (ARMED, real mainnet):** 16:36 cycle — GTA6 `think=buy
gate=PASS` → sealed → memo on-chain → quote GET 200 → **blocked at the
2.5% price-impact floor (5.30%)** → commit journalled
`failed | price impact 5.30% above floor 2.5%`. The whole pipeline now runs
end-to-end; the first fill lands when a candidate quotes under the impact
floor (market-dependent; the machinery is proven). No `cycle crashed` since.

**New endpoint — `GET /api/live/executions`** (`api/routes/live_book.py`):
read-only view of the live state dir — `commits` (CommitLog, newest 100,
full lifecycle sealed→published→bound/failed + fail_reason + memo/fill
signatures) + `records` (ExecutionLedger money movements, newest 100) +
totals. Same fail-soft contract as `/api/live/portfolio` (never 500s,
degrades to `{"enabled": false, "reason": ...}`).

**Frontend — Journal + Holdings pages restored** (removed in b49bb10 with
the paper components; the operator wanted them back as live pages):
- `App.tsx` — three-page tab bar (dashboard / holdings / journal), plain
  buttons with `aria-current`, keyboard-operable.
- `components/Holdings.tsx` — live positions detail (size/entry/mark/value/
  uP&L/opened/mint) from `/api/live/portfolio`; documented empty state.
- `components/Journal.tsx` — order-decisions table (status badges:
  filled / memo-only-no-fill / sealed / failed; expandable proof row with
  fail reason, commit hash, memo + fill solscan links) + money-ledger table.
  This page answers "why didn't it buy?" directly.
- Types: `LiveCommitEntry`, `LiveExecutionRecord`, `LiveExecutionsResponse`.
- Playwright: +3 tests (tab navigation, holdings data/empty, journal proof
  expand) → **8/8 E2E passing**.

**Tests:** +5 (quote-GET verb regression incl. MockTransport proof,
buy/sell quote-failure→failed-not-NameError, sell full-flow GET fill +
ledger reduce, real-solders signing round-trip) → **506 passing**.


## 37. Stale-holdings dust fix + first REAL fill + items 1 & 3 (2026-08-28)

Operator report: "the bot bought a token, then sold it, yet it still shows up
in holdings … journal even shows it's closed" + "shows enter but doesn't
enter." Root cause was ONE ledger bug with three symptoms; fixing it unblocked
entries and the bot immediately landed its **first real fill**. Then shipped
the two deferred omo-audit items (1 narration anti-repetition, 3 commit
orphan reconciliation).

**The dust bug (root cause of all three symptoms).** A reconcile-clamped FULL
exit produced a sell fraction just under the 0.999 close threshold
(chain/journal mismatch, e.g. 0.99889), so `ExecutionLedger.reduce_position`
booked it as a *trim* and left a dust row `status="confirmed"` (OPEN). That
phantom row (a) showed in Holdings, (b) counted against `MAX_OPEN_POSITIONS=3`
so every subsequent ENTER was refused ("would hold 4 mints"), and (c) left the
journal saying closed while the book said open.
- **Fix:** threaded the exit engine's full-close intent through
  `models.reduce_position(..., full_close=False)` →
  `executor.place_sell(..., full_close)` → `executor.place_order(...,
  full_close)` → `run_live_cycle._manage` (`full_close=(decision.action ==
  "close_full")`). With `full_close=True` the position is CLOSED outright and
  PnL is realized against the FULL cost (the journal-vs-chain dust is written
  off, never left open). Trim behaviour (`full_close=False`) is unchanged.
- **One-time data repair** (services stopped → edit → restart): flipped the
  stuck `2NffKvfZ…` dust buy row to `closed` (its sell had already filled and
  the close record already carried the correct −$0.19 PnL). Backup kept at
  `executions.json.bak-dust`. Freed the position slot + removed the ghost.
- **Tests:** +3 (`test_ledger_full_close.py`: sub-threshold full-close closes
  + realizes on full cost; trim-without-full-close leaves remainder; full-cost
  PnL).

**Live proof (ARMED, real mainnet):** 18:40 cycle — `PINK think=buy
gate=PASS` (no longer blocked) → memo on-chain → Jupiter quote **GET 200** →
swap build 200 → **`FILLED buy AVBN6kXd $0.66 -> 491.146377 tokens`** →
venue attributed (jupiter router). First real fill; the whole manage→read→
think→gate→execute pipeline now trades real money. `think=pass -> refused`
rows are the model veto working as designed.

**Item 3 — commit orphan reconciliation** (`live_execution/commit_log.py`):
new `CommitLog.reconcile_orphaned(max_age_seconds=600)` marks any commit still
`"published"` (memo on chain, no bound fill) that is older than the window as
`failed` with reason "memo published but no fill followed (orphan reconciled)"
— reusing the existing `failed` status so no UI/proof route needs a new state.
Wired at the top of `run_live_cycle.run_cycle` (cheap, idempotent, fail-soft).
Heals the historical orphans that predate the §36 post-memo `fail()` wiring.
**Live:** first cycle reconciled **7** orphans → commits.json now
`{failed: 8, bound: 5}`, **zero ambiguous `published`**. Tests: +4.

**Item 1 — narration anti-repetition** (`backend/llm/narrator.py`, omo
cabin-ritual parity, lightweight): a rotating set of style angles
(`_ANGLES`) is appended to the LLM prompt and the deterministic template
opener rotates (`_ENTER_OPENERS`/`_REJECT_OPENERS`) so consecutive narrations
read distinctly. Style-only: never changes which rules are cited or the
verdict; grounding unaffected. Tests: +3.

**Tests:** 506 → **516 passing** (+3 full-close, +4 reconcile, +3 rotation).
All endpoints 200, 0 tracebacks after restart.

## 38. Security audit + hardening (2026-08-28)

Operator-requested audit against a 20-rule security checklist (hide keys, purge
git secrets, RLS, encryption, auth, parameterized queries, input validation,
escaping, uploads, response trimming, security headers, HTTPS, dependency
scans). Result: **11 pass · 3 partial · 2 gaps · 4 not-applicable** (no
accounts/login/cookies/public surface exist — rules 9–12 have nothing to
protect). Full-history git scan: **zero secrets ever committed**; all keys
live only in the untracked `.env`. Supabase RLS is ON for all 13 tables with
zero permissive policies; DB access is server-side service-role over the
direct pooler (no anon key to leak). Both DB layers fully parameterized; the
only f-string SQL interpolates hardcoded whitelisted table literals.
`npm audit --omit=dev`: 0 vulns; `pip audit` (33 pkgs): 0 known vulns.

**Findings fixed this session:**

- **F1/F2 — file perms:** `.env` and `~/.config/solana/drill-keypair.json`
  were 644 (world-readable); now `600` (mainnet wallet already was).
- **F3 — unauthenticated mutating endpoints:** `POST /api/admin/reset` and
  `POST /api/knowledge-base/ingest` now require the `X-Admin-Token` header
  matching `config.ADMIN_TOKEN` (new `api/auth.py::require_admin_token`,
  constant-time compare). **FAIL CLOSED:** an unset/empty token disables the
  endpoints entirely (403) — a destructive endpoint is never open without a
  credential, even on loopback. A 32-char token was generated into `.env`
  (untracked); `.env.example` documents the knob.
- **F4 — ingest size cap:** `loader.ingest_file` rejects documents over
  `config.MAX_INGEST_CHARS` (default 200,000, env-overridable) before they
  touch disk/DB/prompt context.
- **F5 — security headers:** new middleware sets `X-Content-Type-Options:
  nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` on every
  response and `Cache-Control: no-store` on `/api/*`.
- **F6 — CORS narrowed:** `allow_methods ["*"]→["GET","POST"]`,
  `allow_headers ["*"]→["Content-Type"]` (only what the dashboard uses).

**Accepted risk (documented, not fixed):** HTTP on loopback only (rule 19) —
the API binds `127.0.0.1:8000`, traffic never leaves the machine, and all
external calls are TLS; browsers forbid HTTPS-only for localhost. **Operator
action item (F7):** the GitHub repo is PUBLIC — verified secret-free, but
consider making it private (one-click setting, cannot be done from here).

**E2E hardening (same session):** the three feed/panel tests were timing-
fragile (empty feed between live cycles; slow first chain reads after
restart). Now deterministic: skeleton check polls up to 45s; feed tests
accept rows OR the documented empty state (same pattern as holdings/journal).

**Tests:** 516 → **527 passing** (+11 in new `test_security_hardening.py`:
token guard missing/wrong/unset/valid/confirm-still-required, kb-ingest
guard, size cap, empty-doc refusal, headers, no-store, CORS narrowing).
Playwright **8/8**. Live-verified after restart: headers present; admin reset
403 without/with-wrong token, 200 with the real token (prune_only); kb
ingest 403 without token; all endpoints 200; 0 tracebacks.


## 39. Roadmap items #1, #3, #6 — security_clear live, dumped-author heat discount, unified read/think/gate core (2026-08-29)

**Item #1 — `security_clear` RE-ACTIVATED as the 10th gate rule** (operator
decision, roadmap item #1). `ACTIVE_RULES` is now the reference 9 + `security_clear`
(inserted between `already_held` and `not_on_break`; order pinned by
`test_active_rules_are_exactly_reference_nine_plus_security_clear`). Reference
parity is knowingly broken by this one addition — documented as such in
`rules.py`, docs/06/07/09. Safe by construction: the rule fails only on
KNOWN-bad values (B10) — live mint authority (`mint_authority_revoked is
False`) or a honeypot flag (`is_likely_honeypot is True`); `None`/unknown
ALWAYS passes, so missing data can never block a trade. Because
`run_live_cycle.LIVE_ACTIVE_RULES` derives from `ACTIVE_RULES` via a
comprehension, the rule went live on BOTH books with one edit. Live-verified:
`/api/feed` rule breakdowns now carry 10 rules with `security_clear` present;
the mock `HONEYPT` archetype once again exercises the honeypot refusal path
(it was built for exactly this rule).

**Item #3 — crowd author-P&L attribution (omo exit-liquidity-dump parity).**
`crowd.py::_is_dumped(row)`: a thesis is DUMPED when its author closed at a
realized profit (`closed AND realized_usd > 0`) — someone shilling a token they
already exited with a win is marketing, not conviction. KNOWN-data only
(closed-at-a-loss, open positions, missing `authorTrade` all keep full credit
— unknown ≠ dumped, same discipline as `security_clear`).
`fetch_fomo_theses` now also returns `dumped_count` + `effective_total`
(= `total − dumped_seen × (1 − FOMO_DUMPED_THESIS_WEIGHT)`); `total` stays the
board's raw number (thinker/UI/tests unchanged). `enrich_crowd_heat` feeds
`heat_from_count(effective_total)` — so a board whose visible thesis authors
all took profits and left stops counting as live crowd heat. New env knob
`FOMO_DUMPED_THESIS_WEIGHT` (default `0.0`; `1.0` = old behavior). The
thinker already saw author P&L per row (`thinker.py` evidence lines) — this
closes the HEAT half of the audit gap. +6 tests in `test_crowd.py`.

**Item #6 — paper + live pipelines unified on `backend/decision_pipeline.py`.**
Both entry points now run the SAME stages through the new shared module
(imports only backend/ — isolation contract intact, grep-pinned by test):
  - `read_candidates(provider)` — fetch + blocklist + FAKE-CHART filter.
    This closed a live-side drift: `run_cycle` was missing the A7 fake-chart
    filter entirely (it ran only in the paper tick).
  - `enrich_candidates(candidates)` — the live-only enrichment chain, paper's
    order, fail-soft per feed; returns social usages for journaling.
  - `think_candidate(c, thinker, memory_line)` — think + template fallback.
    Closed live drift #2: a thinker exception used to kill the whole live
    cycle; now the paper's fail-closed template degrades that one candidate.
  - `apply_break(think)` — self-regulating break. Closed live drift #3: the
    live copy called `set_break(minutes, reason)` — missing the leading
    `taking` positional, a latent TypeError the moment a thinker ever
    requested a break. Fixed for both books in one place.
  - `gate_candidate(c, portfolio, regime, rules)` / `entry_decision` — one
    gate, one rule-set injection point (paper `ACTIVE_RULES`; live swaps only
    the cash rule via `LIVE_ACTIVE_RULES`, unchanged).
Sizing, seals, ledgers, exits stay per-book (deliberate differences).
`main.run_tick` and `run_live_cycle.run_cycle` are now thin over the shared
core; parity pinned by new `test_decision_pipeline.py` (backend, 13 tests)
and `test_pipeline_parity.py` (live side, 4 tests: source delegation, shared
gate + LIVE rules, and paper-vs-live decisions differing in EXACTLY the
cash rule).

**Verification:** full suite **550 passing** (backend 418 + live_execution
132; +23 over the 527 baseline: +6 crowd, +13 decision-pipeline, +4 parity).
Playwright 8/8. Live cycle restarted on the new code: clean cycle with gate
decisions flowing (`think=buy gate=PASS` / refused rows), `security_clear`
in every breakdown via `/api/feed`, 0 tracebacks, process stable. Docs
aligned: rules.py header, docs/06 §1 table, docs/07 §14.1/§14.3, docs/09 §
"9 vs 10" + rule table, FOMO_INTEGRATION.md §2, this handoff, memory-bank.


## 40. security_clear unblinded: dead on-chain RPC fallback fixed + Birdeye quota fast-fail (2026-08-29)

**The incident.** Asked to analyze how omo gets token security via
Dexscreener (because "Birdeye returns errors too many times"). The analysis
found THREE facts, two of them bugs:

1. **omo reads no token security from anywhere** (no authority/honeypot
   fields anywhere in their lib; their only rug defenses are the fake-chart
   filter + liquidity floors). Dexscreener's API cannot provide token
   security at all — its pair payload has no authority/honeypot fields. So
   there was nothing to port: security is OUR advantage, and the right fix
   was making OUR security path actually work.
2. **The Birdeye key was quota-exhausted** — every trending AND token_security
   call answered 400 `{"success":false,"message":"Compute units usage limit
   exceeded"}`, each burning 3 retries + backoff (~1,371 error lines in one
   live log).
3. **The keyless on-chain RPC fallback (the safety net for exactly this day)
   had NEVER worked.** `onchain_security.get_authority_flags` POSTed
   `{"method", "params"}` WITHOUT the JSON-RPC envelope (`jsonrpc: "2.0"`,
   `id`). mainnet-beta answers that with **200 + EMPTY body** (its
   rate-limit masquerading as success — `x-ratelimit-endpoint-remaining`
   negative); publicnode answers 400 "Parse error". Either way `resp.json()`
   threw, every endpoint "failed", and every live `security_clear` detail
   read `unknown, unknown, unknown` — the rule (activated §39!) was blind
   since its activation. `live_execution/solana.py` has sent the full
   envelope all along (its chain reads work — proof the pattern was known,
   just never copied into the backend fallback).

**Fixes (all proven live before commit):**

- `onchain_security.py`: full JSON-RPC envelope + empty-200 treated as a
  failure (rotate to the next RPC, never parse garbage) + non-mint parsed
  accounts rejected. Live proof after the fix:
  `get_authority_flags(MEW)` → `{'mint_authority_revoked': True,
  'freeze_authority_revoked': True}` from the real chain.
- `base.py`: new `ProviderQuotaError` — a 400 whose body says quota/limit
  ("Compute units usage limit exceeded") raises IMMEDIATELY (zero retries;
  same treatment as 401/403). A generic 400 (bad address) keeps the normal
  retry/fail path. Sniff is phrase-based, never a guess.
- `birdeye.py`: both surfaces self-disable for the session on quota-400 or
  401/403 (they already did for security; trending now does too — operator
  approved fast-fail on both). Birdeye answering 401 today (tier denial
  after quota reset) still lands in the same one-line session-disable.
  Discovery was never at risk: the keyword scanner + Dexscreener + new
  listings carry it (Birdeye trending was already optional).

**Result (live, same session):** `security_clear` details now read
`mint authority revoked: yes, freeze authority revoked: yes` on every
decision — real on-chain truth per candidate, keyless, omo cannot do this.
The Birdeye burn went from 3 retries × N candidates per cycle to a single
session-disable line. 0 tracebacks, process stable across cycles.

**Tests:** +12 → **562 passing** (new `test_onchain_security.py`: the
envelope-shape regression test, empty-200 rotation, non-mint rejection,
authority parsing both directions, quota-body sniff true/false/empty, and
the two Birdeye session-disable paths).


## 41. Out-of-band sell repair: `close_out_of_band` + `repair_vanished` CLI (2026-08-29)

**The incident.** The operator manually sold a coin the bot held (their own
wallet, during the broken-RPC window) — `GE8q5h6e…pump`, 4015.376685 tokens,
$0.5370 cost, proceeds **1.11715 USDC** (operator-confirmed). The chain
balance went to 0; the ExecutionLedger still showed the buy OPEN (it never
saw the sell). reconcile() worked EXACTLY as designed — flagged the mint
`chain_excluded`, logged "operator review needed" every cycle, never
mutated the money ledger — but there was no sanctioned way to COMPLETE the
review, so the position showed in Holdings forever (second occurrence of
this class after §37's dust row; the §37 hand-fix was one-off, this time it
got a real tool).

**What shipped:**
- `ExecutionLedger.close_out_of_band(mint, proceeds_usd=None, note="")` —
  the operator decision, recorded as such: closes EVERY open buy of the
  mint, appends one close record whose idempotency_key carries
  `outofband` + a note for forensics. HONEST P&L: known proceeds realize
  against the summed cost; unknown proceeds record `pnl_usd=None` — never
  fabricated — and `realized_pnl_today()` skips None rows, so an
  unknown-proceeds close can never trip the daily-loss breaker on a
  made-up number. Refuses (ValueError) when nothing is open for the mint.
- `live_execution/scripts/repair_vanished.py` — operator CLI
  (`python -m live_execution.scripts.repair_vanished list|close`), mirroring
  confirm_trade.py's conventions. `list` shows every open position with its
  CURRENT chain balance (UNREADABLE reported as such, never guessed).
  `close` has TWO safety gates: (1) the mint must be an open ledger
  position; (2) the chain balance must be VERIFIABLY 0 (a live position or
  typo is refused; an unreadable RPC is refused — fail closed). On success:
  ledger repaired + `did` event journaled + the open thesis write-up
  retired. Loads `.env` via the backend config import so the wallet/RPC
  resolve exactly like the live cycle's process. NEVER executes, quotes,
  or signs anything — bookkeeping only.

**The repair (executed live):** cycle briefly stopped (avoiding a
concurrent-write race on executions.json — last-writer-wins would lose
records), repair run, cycle restarted. Verified: `GE8q5h6e` absent from
Holdings, ZERO reconcile warnings in the new log (was one per cycle),
realized P&L includes the **+$0.5801** profit, 0 tracebacks, and the cycle
opened a fresh genuine fill (TIT, $0.53 → 1306.93 tokens) minutes later —
proving the book is healthy end-to-end.

**Tests:** +6 → **568 passing** (`test_ledger_full_close.py`:
with-proceeds realization incl. note forensics + realized_pnl_today,
honest None-proceeds skipped by the breaker, multi-buy summed cost,
typo'd-mint refusal, other-mints untouched, idempotent second call).




