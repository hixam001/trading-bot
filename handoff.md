# HANDOFF — trading-bot

**Last updated:** 2026-08-26 · **Branch:** main · **Status:** LIVE
(real market data, simulated funds; Supabase Postgres persistence active) ·
**App:** http://localhost:8000

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
cd backend && ../.venv/bin/python -m pytest tests/ -q   # 145 tests, ~1s
.venv/bin/python -m pytest -q                           # 193 incl. live_execution
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
| `backend/api/db_pg.py` | asyncpg/Supabase Postgres twin of db.py — identical surface incl. §5.1 atomicity (rowcount from execute status); TLS via SHA-256 cert-fingerprint pinning (TOFU, `.supabase_fp.txt` gitignored) |
| `migrations/supabase/001_init.sql` | run ONCE in Supabase SQL editor: 9 tables, JSONB/TIMESTAMPTZ, one-open-position exclusion constraint, RLS locked |
| `backend/api/main.py` | FastAPI app; serves built frontend; TICK_LOOP_IN_PROCESS env runs tick loop in-process |
| `backend/data_providers/` | base(protocol,retry,counters), birdeye, dexscreener, jupiter, live(stack), mock |
| `backend/llm/` | narrator.py (prompt, Ollama client, template fallback, reflection), grounding.py |
| `backend/knowledge_base/loader.py` | static KB, digest-at-ingest, budgeted get_context |
| `backend/main.py` | run_tick(): regime once/tick → per-candidate gate+narrate → exit checks |
| `backend/promotion_gate.py` | READ-ONLY 5-criteria readiness report. Never writes. Ever. |
| `live_execution/` | REAL-MONEY execution package at repo ROOT (never imported by backend/). Fully wired: `run_live_cycle.py` manages live positions, runs the shared read/think/gate stages, and routes buys/sells through `place_order`; Jupiter quote/swap, local signing, rotating RPC broadcast, confirmation, commit binding, and ledger journaling are connected. Ships DISARMED: hardcoded `LIVE_TRADING_ENABLED=False`, `REQUIRE_MANUAL_CONFIRMATION=True`, kill switch + daily-loss breaker, fail-closed confirmation expiry, idempotency ledger, caps, wallet identity checks, and decimals guards. Operator CLI: `python -m live_execution.scripts.confirm_trade list|approve|deny|kill|resume`. Offline/mock wiring tests pass; a funded throwaway-keypair devnet drill is still REQUIRED before any mainnet use. |
| `frontend/src/` | dashboard panels (feed WS, holdings, journal, stats [equity/spend/realized/unrealized/cash], regime, gate, status); no knowledge tab, no paper-trading banner (removed 2026-08-25) |
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
- Post-calibration scope, deliberately unbuilt: LLM API migration and usage
  accounting (docs/08), durable OMO memory/events roadmap (OMO-R5), and the
  remaining approved OMO roadmap items in section 13.

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
5. **LLM API migration:** implement section 14 and
  docs/08_LLM_API_MIGRATION_AND_FEEDBACK_PLAN.md. Target DeepSeek V4 Flash
  direct API for the thinker, Groq for evidence-only social reads, and
  measured usage/outcome instrumentation before switching models.
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
  `run_live_cycle.py` → shared provider/read/DeepSeek think stages →
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

### OMO-R2 — FOMO crowd intel upgrade: theses WITH author PnL ✅ APPROVED
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

## 14. LLM API migration and feedback plan (2026-08-26) — TO BE IMPLEMENTED

Full plan: docs/08_LLM_API_MIGRATION_AND_FEEDBACK_PLAN.md. This is a planned
provider migration. 
**Status update (2026-08-26):** DeepSeek V4 Flash is now the primary thinker API, and Groq is the primary social read API. Both are wired and functioning correctly. Instrumentation and shadow replay steps remain in progress.

### API/model decisions

- **Thinker primary:** DeepSeek V4 Flash direct API, non-thinking mode, strict
  JSON output, bounded prompt/output, and a provider-neutral adapter.
- **Thesis/reflection analysis:** DeepSeek also supplies post-close thesis
  reflection; it never performs execution itself.
- **Thinker fallback:** deterministic refusal (`pass`) for timeout, outage,
  quota exhaustion, invalid JSON, or provider error. A template may explain
  the refusal but must never approve an entry while the provider is unavailable.
- **Twitter/social:** retain Groq initially. It is an evidence-only,
  low-latency classifier (`organic|peaked|unclear`), not a trade decision.
  DeepSeek may be a later fallback only after an independent benchmark.
- **Reflections/reports:** queue DeepSeek V4 Flash work off-peak; never block
  exits or the tick loop. V4 Pro is an evaluation challenger only for now.

### Required implementation order

1. Add provider/model/prompt versioning and per-call token, cache, cost,
   latency, status, and peak-window accounting in both database backends.
2. Extract a shared OpenAI-compatible JSON client with DeepSeek, Groq, and
   template adapters; preserve key redaction, bounded retries, and breakers.
3. Run Qwen-versus-DeepSeek shadow replay over sealed snapshots and the DONT
   corpus; shadow results must not alter paper trades.
4. Canary DeepSeek in paper mode only, recording precision, missed upside,
   adverse excursion, drawdown, p95 latency, JSON validity, fallback rate,
   and cost per candidate/entry/closed trade.
5. Add delayed outcome labels at 5m, 15m, 1h, 6h, and 24h, then produce
   human-approved experiment proposals. Never auto-edit thresholds, prompts,
   models, or trade state.
6. Update older docs that still call Qwen narration-only after measured
   migration results are available.

### Cost and feedback controls

- Target thinker budget: 300–600 input tokens and 60–140 output tokens,
  maximum 192 output tokens, one request per candidate at most.
- Preserve blocklist-before-think, candidate prioritization, thesis reuse,
  no duplicate in-flight mint requests, daily USD/token budgets, and queues.
- DeepSeek pricing is variable; the 2026-08-26 official V4 Flash example is
  $0.22/M cache-miss input and $0.66/M output off-peak, with peak rates
  currently double. Store the peak-window flag on every request.
- Current learning is measurement-only: daily P&L, win rate, profit factor,
  drawdown, rejection breakdowns, and post-close reflections. Reviewed OMO
  evidence shows adaptive context and auditability, not demonstrated
  autonomous model training or weight updates.

## 15. Final implementation queue (2026-08-26)

This is the last section in the handoff and is the authoritative short list of
what comes next. Full requirements and OMO references remain in sections 13
and 14 above.

### Approved — implement in this order

1. **OMO-R5 Memory/events system** — ✅ IMPLEMENTED.
2. **OMO-R4 Self-regulating break system** — ✅ IMPLEMENTED.
3. **OMO-R3 Durable thesis book.** — ✅ IMPLEMENTED.
4. **OMO-R2 FOMO intel with author P&L.**
5. **OMO-R1 Independent verifier and binding report.**
6. **OMO-R6 Public disclosure and reasoning feeds.**
7. **OMO-R7 Retro audit-log signature matching.**
8. **LLM migration completion** — DeepSeek for thesis/thinker and reflections;
   Groq for social evidence; usage accounting, shadow replay, paper canary,
   and delayed outcome labels before any model promotion.

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
