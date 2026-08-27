# Active Context — trading-bot

**As of 2026-08-27 (DeepSeek main-provider swap executed + live-verified).**
Repo: `/home/hixam/Downloads/Projects/trading-bot/`.

## DONE
### Main LLM Groq → DeepSeek V4 Flash (handoff §18 → §19) (2026-08-27)
`MAIN_LLM_PROVIDER` selector (`groq`|`deepseek`, code default groq; live
`.env` flipped to `deepseek`). `DeepSeekClient` + `build_main_client()`
factory + `main_max_tokens()` in `llm/client.py`; provider-aware timeouts;
DeepSeek cost branch (off-peak $0.22/$0.007-cache/$0.66 per 1M; peak 2×,
window 01:00–04:00 + 06:00–10:00 UTC Mon–Fri); pricing snapshot ids per
provider. Thinker/Narrator/reflections use the factory with
`provider:model` source labels; reflections skip to template in DeepSeek
peak windows. `narration_mode` reports the active provider. Social reads
UNCHANGED (Groq). Two latent bugs fixed + regression-tested: `is_peak`
was never computed (always off-peak), and V4 Flash defaults to THINKING
mode which emptied `content` → every DeepSeek request now sends
`thinking:{type:disabled}`. `shadow_replay.py` rebuilt on the repository
layer (SQLite+Supabase). Live-verified: ping OK, shadow replay 8/8 verdict
agreement (100% valid JSON, p95 2.6s), first full DeepSeek tick completed
(20 candidates, all thinker calls success with peak cost ~$0.001), zero
tracebacks. 30 new tests → **261 passing**. Rollback = `MAIN_LLM_PROVIDER=groq`
+ restart (note: GROQ_API_KEY currently empty in .env → fail-closed
template passes until re-added).

## DONE
### Stealth-scrape chain: 429 rate-limit vs 402 credit split (2026-08-27)
Live-diagnosed operator report that Firecrawl/ZenRows still had credits.
Root cause of the Firecrawl dropout: a `429 Too Many Requests` (a transient
rate-limit) was benched for the full 30 min like credit exhaustion. Fix in
`data_providers/crowd.py`: `_handle_provider_status()` now benches 402 for
`STEALTH_BENCH_SECONDS` but 429 only for the new
`STEALTH_THROTTLE_BACKOFF_SECONDS` (default 75s), and logs the provider's own
error body so quota reasons are self-diagnosable. ZenRows' `.env` key is in
fact at its usage limit (AUTH004 on both cheap and premium requests); ScrapingBee
basic tier answers but its stealth-proxy errors and it can't forward the Privy
bearer, so it never serves fomo reads. +1 regression test → **262 passing**.

## DONE
### DB maintenance: prune + reset + OMO audit (2026-08-27)
Added `prune_feed_events(conn, keep_rows)`, `prune_market_regime(conn, keep_rows)`,
and `reset_book(conn, initial_cash_usd)` to both `api/db.py` (SQLite DELETE NOT IN)
and `api/db_pg.py` (TRUNCATE RESTART IDENTITY CASCADE). New operator-only endpoint
`POST /api/admin/reset` in `api/routes/admin.py` (requires `?confirm=yes`,
`mode=reset_book` or `mode=prune_only`; logged at WARNING; never touches wallet or
live_execution). Config knobs `FEED_PRUNE_KEEP=2000` and `REGIME_PRUNE_KEEP=500`
added to `config.py`. 9 new tests in `tests/test_admin_reset.py`. Full suite now
**231 passing**. All OMO-R1–R7 routes audited and confirmed correct.

## DONE
### Groq LLM migration + bug audit complete (2026-08-27)
Groq API (`qwen/qwen3.8-27b`) is the live primary LLM for Thinker, Narrator,
reflections, and social reads. `MainGroqClient` (`GROQ_API_KEY`) powers
thinker/narrator; `GroqClient` (`SOCIAL_LLM_API_KEY`) powers social reads.
Full instrumentation (tokens, latency, cost, degradation reason) wired to
`llm_call_usage`. Seven post-migration bugs fixed (stale `DEEPSEEK_MODEL`
refs, `mint_address` AttributeError, bad import path, wrong pricing branch,
stale narration_mode label, start.sh ollama references). All Python files
syntax-clean. LLM ping test: both providers respond correctly.
DeepSeek migration is deferred pending a funded API key.

## DONE
### OMO-R1 Independent verifier + binding report (2026-08-26)
`/api/binding.json` runs four binding checks per sealed decision commit: tx_confirmed (meta.err == null), time_ordering (commit_at < blockTime), fee_payer (account key 0 == wallet), mint_present (mint in pre/postTokenBalances). Each check that cannot run reports `unknown`, never `pass` (fail-closed). New `signature`, `phase`, `matched_by` nullable columns added to `decision_commits` via idempotent ALTER TABLE migrations in both SQLite and Postgres backends. `live_execution/solana.py` gains `get_transaction()` helper. `/api/verify.json` extended with `created_at` and `signature` fields.

## DONE
### OMO-R6 Public disclosure + reasoning feeds (2026-08-26)
`/api/disclosure.json`: machine state — armed/disarmed, kill-switch state, break state, config truths (caps/floors/thresholds). Zero secrets. `/api/reasoning.json`: per-decision provenance — model source (from payload), inputs snapshot hash (sha256 of canonical payload_json), linked commit hash. Both registered in `api/main.py` via the same optional try/except pattern.

## DONE
### OMO-R7 Retro audit-log signature matching (2026-08-26)
`backend/retro_matcher.py`: omo-exact algorithm — same symbol (case-insensitive, $-stripped) + side + fill_at >= decision_at + 12h window; earliest fill wins; `taken` set prevents double-claim. Runs post-cycle in both `main.py` and `run_live_cycle.py`. Exact-bind rows (signature IS NOT NULL) protected by WHERE clause in `bind_commit_signature`. Three new DB functions in both backends: `get_pending_unsigned_commits`, `get_recent_fills_for_retro`, `bind_commit_signature`.

## DONE
### OMO-R4 Bug fix (2026-08-26)
`liveness.set_break(think.break_minutes, think.break_reason)` was silently passing int/str to the wrong positional slots (`taking=break_minutes`, `minutes=break_reason`). Fixed to `set_break(True, think.break_minutes, think.break_reason)`.

## DONE
### Root pytest.ini restored (2026-08-26)
Root `pytest.ini` with `asyncio_mode = auto` and `testpaths = backend/tests live_execution/tests` restored (was removed in commit 20ddc0a). Full suite: **222 tests passing**.

## DONE
### OMO-R2 FOMO crowd intel upgrade (2026-08-26)
Full thesis rows with author P&L are now fetched from fomo.fun and injected into the LLM thinker prompt. The prompt instructs the model to weigh each claim by whether its author is actually up on their position.

## DONE
### OMO-R4 Self-regulating break system (2026-08-26)
File-backed state (`break_state.json` inside `live_execution/state`) implements the `not_on_break` rule parity. The thinker can pass a `"break": {"taking": true, "minutes": 15, "reason": "..."}` block in its JSON verdict, which sets a persistent UTC expiry timestamp. While on break, the gate fails closed loudly on the `not_on_break` rule, blocking entries while exits continue functioning normally. Fail-safe semantics apply on state file corruption. LLM API migration for thinker (Groq) and social (Groq) is verified.

## DONE
### OMO-R5 memory/events system (2026-08-26)
Durable mirrored `events` and weighted `memories` tables now exist in both
repositories. Tick stages write read/thought/did/refused/trade events;
topic-scoped memory recall increments hits and is injected into thinker
prompts as context only. Read-only `/api/events.json` exposes recent events.
Regression coverage passes for validation, persistence, hit accounting,
prompt injection, and mock tick stage events.

## DONE
### Dashboard overhaul (2026-08-25)
User-requested frontend rework: feed rows now read ENTER/PASS; the
`[model veto]` prefix was stripped from main.py so theses display verbatim
(also fixed the double-appended "invalidates if" sentence); expanded feed
rows show the COMPLETE model answer un-truncated plus the token contract
address with click-to-copy, and an amber "model chose not to enter:" block
when all rules pass but the model declines. /api/stats gained
realized_pnl_usd, unrealized_pnl_usd (compute_unrealized_pnl over live
marks, fail-soft on unavailable prices), total_spend_usd (open cost basis
incl. fee+slippage). Portfolio-stats panel shows ONLY total equity / total
spend / realized p&l / unrealized p&l / cash (equity-curve chart removed
from UI; API field kept for compat). Knowledge tab and PaperTradingBanner
deleted from the frontend (verified the THINK prompt never consumes the KB;
read-only backend endpoint left intact). Holdings now render only in the
right sidebar. 145 backend tests pass; vite build clean.

## Current focus: calibration window continues
Trigger: DB forensics showed −60% portfolio ($1,000 → $113 cash): 25 closed
trades, 16% win rate, stops realizing −40% avg on a −20% config, and DONT
re-entered 15×/stopped out 15× for −$709. User approved FULL mimicry of
omotrades' logic including the think→gate intersection (qwen3 verdict = a
necessary veto layer). Source-level study: omo's fomo.server.ts,
fomo-auth.server.ts, pipeline.server.ts, audit.server.ts, market.server.ts,
execute.server.ts, blocklist.ts, PROCESS.md.

## Completed phase: LLM API migration continuation (2026-08-26)

See `docs/08_LLM_API_MIGRATION_AND_FEEDBACK_PLAN.md` and handoff section 14.
Groq direct API is configured for the thinker in strict non-thinking JSON mode; Groq is also configured for evidence-only Twitter/social reads. (Superseded 2026-08-27: main path now DeepSeek V4 Flash via MAIN_LLM_PROVIDER — see top DONE block; social reads remain Groq.)
Instrumentation of tokens, cache hits, cost, latency, model/prompt versions, and delayed outcomes have been implemented. Provider failure must return thinker `pass` for entry; a template may explain but cannot approve an entry.

Current learning is measurement-only: daily aggregates, rejection breakdowns,
and post-close reflections. Reviewed OMO evidence shows adaptive context and
auditability, not demonstrated autonomous weight training.

### Live execution status (2026-08-26)
The root-only `live_execution/` path is fully wired but disarmed. The bridge is
`run_live_cycle.py` -> shared read/DeepSeek think/deterministic gate ->
`live_execution.executor.place_order` -> Jupiter quote/swap -> local wallet
signing -> rotating Solana RPC broadcast/confirmation -> commit-log binding ->
execution ledger. Open-position management reprices live ledger positions and
routes deterministic exit decisions through the sell path. `backend/` never
imports `live_execution`.

Safety state is unchanged and non-negotiable: hardcoded
`LIVE_TRADING_ENABLED=False`, mandatory manual confirmation, wallet identity
check, kill switch, daily-loss breaker, exposure/position/daily caps,
idempotency, confirmation expiry, price-impact guard, SOL reserve, and
unknown-decimals refusal. Solana endpoint selection for the devnet drill and
offline execution tests were verified; a funded throwaway-keypair devnet drill
is still required before any mainnet consideration. No live money has been
executed.

## DONE
### Exit engine (81b9898) — rule_engine/exits.py
omo's exit set ported: stop −20%, trail 50%-activation/40pp give-back vs
persisted HWM, liquidity break <$8k, invalidation −25%&1.4×sells, stale
14d, TP ladder +100/300/900 trims 33/33/50%. Sell risk gate ($25 clip,
30-min/mint cooldown, ≤8 exits/24h; risk-off BYPASSES gate — documented
deviation). E8/E9 partial closes via trim_position (atomic).
main(): dedicated 15s price-only exit scan loop alongside tick. DB
migration: trades.high_water_usd + trades.tranches_taken.

### Old logic purged + entry gate = omo verbatim (ebdd1a1)
liq ≥$15k, vol ≥$8k, newborn 24h/−15%, strict already_held (scale-ins
REMOVED entirely), not_on_break liveness (rule_engine/liveness.py),
crowd_heat act-band [36,100]. exposure_cap + volume_mcap_ratio_ok deleted
with constants/tests. Old TAKE_PROFIT_PCT removed (ladder governs).

### Crowd conviction feed LIVE-VALIDATED (a14be2c…50232ed)
data_providers/crowd.py reads the REAL fomo.fun board:
prod-api.fomo.family/feed/token/thesis via Privy session with
AUTO-RENEWING auth — rotated refresh tokens captured from every mint
response and persisted to gitignored .fomo_privy.json (state file beats
stale .env bootstrap). Transport: full browser header set (origin/referer/
x-supported-chains were missing in our first attempt → that's why direct
reads 403'd), sequential queue w/ 220ms gap, TTL dedupe, junk filter,
board-total extraction (olderThesis+newerThesis+page). Stealth-scrape
FAILOVER CHAIN when challenged: firecrawl → scrapingbee → scrapingdog →
zenrows → scrapeops (credit-exhausted 402 benched 30 min; a rate-limited 429
gets only a short ~75s backoff so a healthy provider isn't sidelined — 2026-08-27
split; firecrawl API host is firecrawl.DEV now — .app host TLS-dead). Enrichment runs ONLY
when DATA_BACKEND=live (mock hermeticity: a real feed answering for mock
mints once flipped all e2e verdicts). Live proof: mint F8hVFDi8… → 40
board theses → heat 100 [fomo] with author positions parsed.
NOTE: privy refresh tokens ROTATE on use — if logs show privy[...]401,
re-extract fresh from a fomo.family re-login (dedicated browser profile).

### Churn guards + sizing + seal (this batch)
- blocklist.py: manual + AUTO blocks (2 consecutive stop-outs on a mint →
  auto-block, enforced in run_tick BEFORE think — saves qwen/scraper credits
  too). Corrupt state file quarantined, never fatal.
- compute_ticket conviction sizing (SIZING_MODE fixed|conviction, default
  fixed for calibration comparability; conviction = min(1, heat/100+0.3),
  base min(cash×15%, $150), floor $25) + DAILY_DEPLOY_CAP_USD=$300 with
  journal-tagged refusals ([daily deploy cap reached]).
- decision_commits table (omo 'seal' parity): every decision sealed with
  sha256(nonce|canonical payload) BEFORE acting; plaintext payload stored
  for recomputation. Wired into run_tick; unique hash index.
- open_position/compute_position_size accept conviction-sized tickets.

### Discovery rotation (this batch)
- data_providers/discovery.py: KeywordScanner — rotating DexScreener
  keyword pool (~45 queries, 5/tick), fake-chart filter (vol/liq > 50x),
  dedup by mint keeping highest-liq pair. Wired as third lens in
  LiveProviderStack.get_candidates() alongside trending + new_listings.
  Keyword candidates carry richer 1h flow data that enriches trending hits.
- Fixes "keeps revolving around same tokens": every tick sees a different
  market slice without repeating within a rotation window.


2. Loop reorder manage-first + decision_commits seal/reveal audit table
3. ✅ THINK STAGE DONE (llm/thinker.py): qwen3 pre-trade {thesis,
   invalidation, verdict}; entry requires verdict==buy AND all rules pass;
   Ollama-down/unparsable ⇒ deterministic template w/ 'degraded' tag;
   mock mode always template (hermetic). LIVE-VALIDATED: full tick ran with
   real Birdeye candidates — CYBERLEEK passed all rules but was MODEL-VETOED;
   DONT refused by both layers; cash untouched. Thinker ~10s/candidate
   (20 candidates ≈ 3min/tick — consider MAX_CANDIDATES_PER_TICK tuning).
5. Conviction sizing min(cash×15%, cap)×conviction + daily notional cap
6. Local proof.json/exits.json endpoints + replay harness over historical
   snapshots (incl. DONT corpus)

## Key evidence to remember
- DONT: 15 entries, 15 stop-outs, −$708.92; holds ~25s; re-entry every tick
- Stops realized −40.2% avg vs −20% config: fixed by fast scan loop
- Winners existed: 4 TPs avg +59.6% — old +50% TP capped them; ladder fixed

### Supabase runtime swap (this batch)
- .env fully configured by operator (USE_SUPABASE_DB=1, all keys set);
  migration 001_init.sql verified applied (schema_migrations row present).
- backend/api/db_pg.py: full asyncpg Postgres backend, identical public
  surface to db.py (all 30 repo functions). Key translations:
  $n params, BOOLEAN, rowcount from execute() status string, RETURNING id,
  ON CONFLICT DO NOTHING, TIMESTAMPTZ via ::text read-back (ISO strings
  preserved for consumers), _ts() converts ISO str -> datetime for writes
  (asyncpg rejects str for timestamptz).
- db.py: backend selection at module bottom — PG overrides via
  globals().update() when USE_SUPABASE_DB=1 + SUPABASE_DB_URL set;
  pytest guard ("pytest" not in sys.modules) FORCES SQLite in tests.
- requirements.txt += asyncpg>=0.30 (0.31.0 installed).
- LIVE SMOKE vs real Supabase PASSED: feed-event JSONB roundtrip, atomic
  open (dup refused), high-water, trim, cash guard (refuse+apply+restore),
  idempotent close, deployed_today, cooldown lookups, regime, provider
  counters, decision-commit seal dedupe, kb, daily stats. Cleanup removed
  its own rows.
- 145 backend tests still pass (SQLite); app imports with PG active.
- NOTE: timestamps read back as "2026-08-23 12:00:01+00" (Postgres style,
  space separator) — fromisoformat parses it fine on py3.11+.

### Supabase TLS hardening + live app boot (this batch)
- Pooler serves a SELF-SIGNED chain — strict system-CA verify impossible;
  leaf-as-CA pinning also rejected (no keyUsage ext).
- Implemented SHA-256 FINGERPRINT PINNING in db_pg._tls_context():
  1) try system CAs; 2) probe cert, compare hash vs backend/.supabase_fp.txt
  (TOFU on first run, exact-match after; MISMATCH = hard abort w/ re-pin
  instructions); 3) loud unverified fallback. Pin file is gitignored.
- Live boot test: uvicorn started with PG active -> lifespan init_db OK,
  /api/stats /api/system-status /api/holdings /api/feed all serving
  Supabase data (fresh $1000 book). Server killed after test.
- 145 tests still pass; smoke re-ran clean twice on pinned path.

### Caveat RESOLVED: header forwarding via stealth providers (this batch)
- ScrapeOps email confirmed by operator; keep_headers=true VERIFIED
  forwarding our Privy bearer -> REAL board data through prod-api
  Cloudflare (BONK theses parsed, board-total correct).
- ZenRows custom_headers=true forwards bearer too; REQUIRED premium_proxy
  for prod-api (standard proxies RESP001 vs its Cloudflare). Costs ~10-25
  credits/request — flagged in code comment.
- ScrapingBee CANNOT carry Authorization: their platform consumes that
  header as their own API key ("Invalid api key" pre-origin). Stays
  keyless-routes/credit-breadth only. Unresolvable.
- BUGFIX _json_from_body: rejected ANY body containing statusCode key,
  but prod-api includes statusCode:200 in SUCCESS envelopes -> scrape
  path silently discarded valid board data. Now rejects only >=400.
- Verified live: scrapeops + zenrows(premium) both return real theses;
  firecrawl/scrapingbee unaffected; 145 tests pass.

- Tested via production adapters against JSON echo targets:
  firecrawl PASS (~2s), scrapingbee PASS stealth_proxy=true (~3s),
  zenrows KEY VALID but flaky: REQS001=domain blocklist (ipify/github),
  RESP001=target-fetch-fail retryable; js_render slow (30s+ timeouts).
  scrapeops BLOCKED operator-side: email confirmation required at
  scrapeops.io/app/proxy. scrapingdog SKIPPED (site down, per operator).
- Reminder (documented limitation): GET-template providers do NOT forward
  our Privy bearer -> prod-api.fomo.family may 401 them regardless;
  Firecrawl stays the primary fallback (forwards headers). New keys add
  credit-failover breadth.

### Ops incident + log redaction (this batch)
- Backend found DOWN (graceful shutdown ~16:22; a 20:16 start.sh attempt
  stalled at the `ollama list` check and never launched uvicorn).
  Restarted manually; verified ticks fetching again.
- Found ZenRows API key leaking into logs/backend.log in plaintext (httpx
  logs full URLs; key rides as query param). Fixed with _ApiKeyRedactor
  logging.Filter installed at crowd.py import on httpx+root loggers
  (idempotent). NOTE: main.setup_logging() never runs under uvicorn — that
  was why an earlier attempt (filter inside setup_logging) silently no-op'd.
- Restart race lesson: killing uvicorn then rebinding :8000 after only 3s
  fails (old instance still shutting down) -> new instance dies, stale one
  keeps serving. Always wait for the port to free before relaunching.
- Verified live: raw key occurrences since restart = 0; apikey=<REDACTED>
  present; 145 tests pass.

### Ops incident + log redaction (this batch)
- Backend found DOWN (graceful shutdown ~16:22; a 20:16 start.sh attempt
  stalled at the `ollama list` check and never launched uvicorn).
  Restarted manually; verified ticks fetching again.
- Found ZenRows API key leaking into logs/backend.log in plaintext (httpx
  logs full URLs; key rides as query param). Fixed with _ApiKeyRedactor
  logging.Filter installed at crowd.py import on httpx+root loggers
  (idempotent). NOTE: main.setup_logging() never runs under uvicorn — that
  was why an earlier attempt (filter inside setup_logging) silently no-op'd.
- Restart race lesson: killing uvicorn then rebinding :8000 after only 3s
  fails (old instance still shutting down) -> new instance dies, stale one
  keeps serving. Always wait for the port to free before relaunching.
- Verified live: raw key occurrences since restart = 0; apikey=<REDACTED>
  present; 145 tests pass.
- start.sh hardened: `timeout 10 ollama list` (the 20:16 stall point);
  backend + ollama launched via setsid (own session — Konsole Ctrl+C /
  tab-close can no longer kill them; nohup alone only blocks SIGHUP).
  Old logs scrubbed: 17 plaintext keys removed from backend.log.

### PG migration bug: raw SQL outside db layer (this batch)
- Supabase logged 42883 `boolean = integer` every ~15s: FOUR files had raw
  SQLite SQL outside api/db — journal.py (is_open=0), proof.py (is_open=1),
  main.py (thesis UPDATE ?-placeholders), paper_trading_engine.py (DELETE
  rollback). Dashboard polls made it repeat on a 15s cadence.
- FIX: moved ALL of it into the db layer — 7 new functions
  (count_closed_trades, get_recent_decision_commits, get_recent_fills,
  get_open_position_marks, get_verify_commits, set_trade_thesis,
  delete_trade_row) implemented in BOTH db.py and db_pg.py. Zero raw SQL
  outside api/db*.py (grep-verified).
- Also removed stale `from paper_trading_engine import default_ledger` in
  proof.py get_exits (name no longer exists — 500 on /api/exits.json).
- Verified live: all 6 endpoints 200; verify.json 80/80 seals match;
  0 tracebacks / 0 boolean=integer in current instance log; 145 tests.
- RULE going forward: NO raw SQL outside api/db*.py — every query must be
  a repository function so both backends stay in lockstep.

## Watch-outs


- ⚠ TERMINAL: login shell is FISH — no `$?`, no heredocs; `bash -c` quoting
  breaks silently. Write bash scripts to files; read outputs from /tmp files.
- pytest canonical: cd backend && ../.venv/bin/python -m pytest tests/ -q
- Suite now: backend 136, combined root 184 (root pytest.ini asyncio_mode)
- isolation grep (backend must not mention live_execution) must stay clean
- .env.example documents ALL env fields incl. crowd-feed keys; user has
  FOMO_PRIVY_REFRESH_TOKEN + FIRECRAWL_API_KEY filled


### Security audit + Supabase prep (this batch)
- Secret scans CLEAN: no hardcoded keys in tracked code, no JWT/key-prefix
  material, .env/keypair never committed in git history.
- Deleted stale files: verify_tasks.sh (one-shot verifier), trading-bot.desktop
  (referenced a nonexistent path).
- Fixed .env.example duplicated section 6c; added section 8 SUPABASE
  (USE_SUPABASE_DB, SUPABASE_URL, SERVICE_ROLE_KEY, ANON_KEY, DB_URL).
- config.py: Supabase fields added (default OFF — SQLite stays authoritative
  until USE_SUPABASE_DB=1 and db layer is swapped).
- migrations/supabase/001_init.sql: full Postgres schema (9 tables, JSONB,
  TIMESTAMPTZ, one-open-position-per-mint exclusion constraint, RLS locked).
- .gitignore += *.db-journal, wallet-keypair.json.

