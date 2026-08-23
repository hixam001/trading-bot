# Active Context — trading-bot

**As of 2026-08-23 (omo-mimicry rebuild in progress).** Repo:
`/home/hixam/Downloads/Projects/trading-bot/`.

## Current focus: rebuilding the bot on omotrades' logic
Trigger: DB forensics showed −60% portfolio ($1,000 → $113 cash): 25 closed
trades, 16% win rate, stops realizing −40% avg on a −20% config, and DONT
re-entered 15×/stopped out 15× for −$709. User approved FULL mimicry of
omotrades' logic including the think→gate intersection (qwen3 verdict = a
necessary veto layer). Source-level study: omo's fomo.server.ts,
fomo-auth.server.ts, pipeline.server.ts, audit.server.ts, market.server.ts,
execute.server.ts, blocklist.ts, PROCESS.md.

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
zenrows → scrapeops (quota-exhausted providers benched 30 min; firecrawl
API host is firecrawl.DEV now — .app host TLS-dead). Enrichment runs ONLY
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

