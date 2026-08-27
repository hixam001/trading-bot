# Progress — trading-bot

## Works (all verified)
- [x] §27 pre-flight + DEVNET DRILL PASSED (handoff §31) (2026-08-28): first
      real keypair load exposed + fixed two latent fail-closed bugs (wallet
      from_json path-vs-content → from_bytes on validated array + 64-u8
      check; drill.py undefined log + logging order) — commit d8e426f, +4
      regression tests. Drill 5/5 on devnet incl. real signed+broadcast+
      confirmed dust transfer and the REF-R11 commit-memo path. Arm flags
      untouched (still DISARMED); remaining steps are operator-only: mainnet
      wallet funding, `.env` re-point, the two hand-edited flags, supervised
      --once
- [x] A11 thesis re-authoring (handoff §30): `backend/thesis_restate.py` —
      the module the original omo audit missed (`thesis-author.server.ts`),
      found in the 2026-08-27 re-read (full local clone). Once per tick/cycle,
      rewrites open write-ups that are stale (>6h) or not model-authored
      against the position's current numbers (≤2/pass, oldest first, under-60-
      word contract). Narrative-only (thesis text/author/updated_at only;
      retired-mid-pass rows guarded out); reuses the tick's own price marks;
      fail-closed validation; peak-window skip; never raises. Wired into the
      paper tick + live cycle, both DB layers, disclosure.json, llm_call_usage
      (task thesis_restate), `did` events. 26 new tests; **470 combined
      passing**; isolation grep clean; live-verified first tick advanced both
      stale open write-ups. Ships with live execution still DISARMED — arming
      is §27 (2026-08-27)
- [x] omo-audit code queue A7/A6/A3/A2/A4 (handoff §29): wash-trade "fake
      chart" filter (all 13 omo thresholds, READ-stage before think/gate),
      hardcoded symbol blocklist (omo's list + `^404`, enforced in
      `filter_candidates`), venue attribution (`live_execution/venue.py` →
      `decision_commits.venue` → /api/binding.json), chain book reconciliation
      (`solana.get_token_balances` + `live_execution/reconcile.py`; journal
      never mutated, exit sizing clamped to chain truth, vanished positions
      excluded + flagged), own-basis read-back (`crowd.read_own_basis` +
      `FOMO_OWN_HANDLE`, cross-checked vs journal cost each live cycle,
      observability only). 65 new tests since REF-R11; **444 combined
      passing**; isolation grep clean; live smoke disarmed all endpoints 200
      with `venue` surfaced. Ships DISARMED — arming is §27 (2026-08-27)
- [x] REF-R11 on-chain precommit memo (commit–reveal) + micro-bootstrap (handoff
      §26): every armed order seals `sha256(nonce|canonical_payload)`, publishes
      the hash on-chain as a Solana memo (`commit:v1:`, SPL Memo program) BEFORE
      the fill, fail-closed (unconfirmed memo blocks the fill). New
      `live_execution/memo.py`; `commit_log.py` `sealed→published→bound`;
      `executor.py` memo-before-quote ordering + `OrderResult` carries seal+memo;
      `solana.get_usdc_balance()`; `run_live_cycle.py` real-USDC cash + journals
      seal+memo into `decision_commits`. Verifier: `memo_signature`/`memo_slot`
      columns (SQLite + PG self-heal + `003_commit_memos.sql`),
      `bind_commit_memo()`/`get_commit_id_by_hash()`, `/api/verify.json` memo
      checks, `/api/disclosure.json` `commit_memo` block. Micro-bootstrap:
      `MIN_SOL_RESERVE` env (default 0.01 SOL), `MIN_LIVE_TICKET_USD=0.5`,
      sizing floor threading (paper bit-identical). Devnet drill sends a real
      memo. 41 new tests; **379 combined passing**; isolation grep clean; live
      smoke disarmed all endpoints 200. Ships DISARMED — arming is §27 (2026-08-27)
- [x] Fresh scraper keys activated + ScrapingDog bearer-forwarding (handoff
      §25): new Firecrawl/ScrapingBee/ScrapingDog/ScrapeOps keys in root .env
      (ZenRows unchanged/exhausted); backend restarted to load them + clear
      benches. `_scrape_scrapingdog` now appends `custom_headers=true` and
      forwards the Privy bearer (ScrapingDog docs: same mechanism as ScrapeOps
      keep_headers, no extra cost). Live-verified: Firecrawl 15× 200 OK (real
      crowd heat restored), ScrapeOps 1× 200 OK failover, 0 tracebacks. +1 test
      (2026-08-27)
- [x] Dead-provider fail-fast + reference fomo-path audit (handoff §24):
      transport-error benching (`_CONSECUTIVE_ERRORS`, 2-in-a-row → bench like
      a 402, any response resets the streak) so a dead scraper can never stall
      a tick; `_scrape_firecrawl` wrapped in try/except (was uncaught);
      `_FIRECRAWL_TIMEOUT(45s)` → `_STEALTH_TIMEOUT(25s)` reference parity;
      `_direct_get` 2 transport attempts. Reference audit confirmed its fomo
      fallback is the same Firecrawl stealth-proxy we already call (no free
      mechanism). 6 new tests (23 in test_crowd.py); 337 combined passing;
      live-verified: ScrapingBee benched after exactly 2 timeouts, crowd stage
      degrades in seconds not ~15 min (2026-08-27)
- [x] REF-R8 + REF-R9 reference-parity batch 2 (handoff §22→§23): drawdown-
      adaptive risk budget (`compute_risk_budget`, verbatim reference port,
      fail-closed to $25, published formula) × closed-loop conviction factor
      (`calibration.py`, bounded 0.6–1.2, confidence-pulled, FLAT on no data);
      `SIZING_MODE="risk_budget"` opt-in branch (default `"fixed"` unchanged);
      derived daily ceiling enforced + journaled; `patch_daily_stats()`
      key-merge persistence in db.py + db_pg.py; disclosure.json surfaces both
      blocks; learning_loop + disarmed run_live_cycle wired. 42 new hand-
      computed tests; 331 combined passing; live smoke verified (tick persists
      real budget, endpoint serves it, 0 tracebacks) (2026-08-27)
- [x] De-brand + rename brain module (2026-08-27): brain module renamed to
      `backend/llm/llm_brain.py` and every identifier de-branded (brain class → `LLMBrain`,
      parser → `parse_llm_tick`, system prompt → `LLM_SYSTEM`, config → `LLM_BRAIN*`,
      requirement ids → `REF-R#`); test files → `test_llm_brain`/`test_ref_r*`; removed
      scratch `verify_reference_commit.py`. Repo-wide upstream-branding → "the
      reference"/"reference" across 54 files (407/407 balanced). Kept `fomo`/`promotion`/
      token tickers (not the brand). 289 tests pass; backend restarted clean on the
      renamed module (0 import errors).
- [x] Reference-style brain (handoff §21): ported the reference repository's LLM *reasoning layer*
      into `backend/llm/llm_brain.py` — role-based router (honest resolution +
      fallback + unsupported-model bench), brain tick prompt (decision buckets,
      ground-truth + price-talk rules, minified-JSON contract), wallet mimicry,
      strict parse/validate, `LLMBrain.tick()` grades ≤8 candidates fail-closed.
      `main.py` uses brain verdicts in live mode (per-candidate thinker fallback);
      the reference `buying`→`buy` is NECESSARY only (gate still ANDs). Fixed pre-existing
      `reused_if_stable` `KeyError: 'stats'` tick-crash (writer stores stats +
      reuse fails closed). Live-verified: brain ping 8/10 graded (`DELTA=buying`,
      2268 tokens, no truncation). 27 new tests, 289 total (2026-08-27)
- [x] DeepSeek main-provider swap (handoff §18→§19): `MAIN_LLM_PROVIDER`
      selector + `DeepSeekClient` + `build_main_client()` factory;
      peak/off-peak/cache cost model; provider-aware timeouts;
      non-thinking mode enforced; thinker/narrator/reflections live on
      `deepseek-v4-flash`; shadow replay gate 8/8; first full DeepSeek
      tick verified; 30 new tests (2026-08-27)
- [x] Stealth-scrape chain 429/402 split: rate-limit (429) → short
      `STEALTH_THROTTLE_BACKOFF_SECONDS` backoff instead of the 30-min credit
      bench; credit exhaustion (402) keeps the long bench; provider error body
      logged (self-diagnoses e.g. ZenRows AUTH004). Restores a healthy
      Firecrawl after a transient throttle. +1 regression test, 262 total
      (2026-08-27)
- [x] Supabase schema-drift fix: self-healing `_SCHEMA_SYNC_SQL` in `db_pg.init_db()` (events/memories/theses/llm_call_usage + versioning columns + RLS + migration bookkeeping); db_pg surface fixes (`status` param, `::text` casts, feed-event versioning parity); live-verified: first full tick completed, all endpoints 200 (2026-08-27)
- [x] DB maintenance: prune_feed_events/prune_market_regime + reset_book in both db.py + db_pg.py (2026-08-27)
- [x] POST /api/admin/reset (confirm=yes required; reset_book + prune_only modes) (2026-08-27)
- [x] REF-R1–R7 audit: all routes confirmed correct and well-tested (2026-08-27)
- [x] REF-R1 Independent verifier + binding report: `/api/binding.json`, 4-check binding (2026-08-26)
- [x] REF-R6 Public disclosure + reasoning: `/api/disclosure.json` + `/api/reasoning.json` (2026-08-26)
- [x] REF-R7 Retro audit-log signature matching: `retro_matcher.py` post-cycle (2026-08-26)
- [x] REF-R4 bug fix: `liveness.set_break` call site wrong positional args fixed (2026-08-26)
- [x] Root pytest.ini restored: `asyncio_mode=auto` for full combined suite (2026-08-26)
- [x] REF-R2 FOMO crowd intel upgrade: theses WITH author P&L (2026-08-26)
- [x] REF-R5 durable events + weighted memory recall, prompt context,
      stage event hooks, `/api/events.json` (2026-08-26)
- [x] REF-R4 Self-regulating break system wired into thinker and gate loops (2026-08-26)
- [x] REF-R3 Durable thesis book hooked into live and paper cycles + /api/theses.json (2026-08-26)
- [x] 10 deterministic rules, both branches tested, no short-circuit (B1–B12)
- [x] Candidate model with all fields incl. decimals + None semantics (B13)
- [x] Market regime: computed once/tick, own table, API endpoint (C1–C5)
- [x] Atomic paper engine: open/close/scale_in, decide_and_act, exits,
      PAPER_TRADING_ONLY runtime asserts (E1–E7) — double-open/double-close/
      crash-replay proven by tests
- [x] Money math with hand-computed expectations, raise-on-invalid (E5/E6)
- [x] Providers live+mock: Birdeye memepool/security, Dexscreener full
      enrichment, Jupiter decimals-aware, retry/429/counters (A1–A9)
- [x] LLM narration pre-decided verdicts + grounding validation + Ollama
      health + reflections (D1–D6); template fallback for offline runs
- [x] Knowledge base: static + ingest CLI/API + digests + budgeted context
      + bucket stats (F1–F9)
- [x] Learning loop daily stats + rejection breakdown (G1–G3)
- [x] Read-only promotion gate, 5 criteria (G4–G6)
- [x] Full API surface + WS feed broadcaster (H1–H5)
- [x] Dashboard panels incl. persistent safety banner (I1–I10; build passes)
- [x] Tests J1–J5 → **222 passing** (full suite: backend + live_execution)
- [x] One-click launcher verified start/stop/restart
- [x] the reference bot comparison + commit-reveal verification (docs/06)
- [x] Task C: live_execution/ seven-file safety model at repo root,
      offline-tested (**48 passing**; 182 combined via root pytest.ini)
- [x] reference-mimicry rebuild in progress: exit engine + fast scan loop,
      entry gate = the reference rules verbatim, old logic purged, crowd conviction
      feed (fomo.fun board) live-validated → **backend 136 / combined 184**
- [x] Security audit: secret scans CLEAN, stale files removed, gitignore
      hardened (*.db-journal, wallet-keypair.json)
- [x] Supabase Postgres backend (optional): migrations/supabase/001_init.sql
      applied; api/db_pg.py asyncpg twin of db.py (identical surface,
      §5.1 atomicity preserved); db.py backend selection with pytest
      SQLite guard; live smoke passed all atomicity checks against real
      Supabase + uvicorn boot serving PG data on all endpoints
- [x] Stealth-scrape chain upgraded: scrapeops keep_headers + zenrows
      custom_headers+premium_proxy forward the Privy bearer — verified
      pulling REAL fomo board data through Cloudflare; scrapingbee
      keyless-only (platform limitation); _json_from_body statusCode≥400
      bugfix (success envelopes were silently discarded)
- [x] Live execution wiring verified: root bridge, Jupiter quote/swap,
      local signing, rotating RPC send/confirm, commit binding, ledger
      journal, live manage/sell path, and fail-closed preflight are connected;
      disarmed by hardcoded `LIVE_TRADING_ENABLED=False`; 45 focused tests
      pass. Funded throwaway-keypair devnet drill remains required.

## Deliberately not built (per spec sequencing)
- E8/E9 partial scaling + rolling history (post-calibration)
- D7 advisory LLM layer (post-calibration)
- crowd_heat rule (needs a fomo-index source)

## Known issues / watch items
- Birdeye free tier: token_security 401 → security fields UNKNOWN
- Ticks take 40–90s with 20 candidates (LLM-bound); acceptable
- Regime/rule thresholds are placeholders — calibration will move them
- ScrapingBee fallback is keyless-only (platform consumes Authorization);
  it also ReadTimeouts on stealth reads — now benched after 2 consecutive
  transport errors so it can't stall ticks (handoff §24)
- ZenRows premium tier costs ~10–25 credits/request (required for prod-api)
- ⚠ ZenRows is 402 credit-exhausted (benched; optional renewal). Firecrawl +
  ScrapeOps got fresh keys 2026-08-27 and now serve REAL crowd heat (handoff
  §25). ScrapingBee ReadTimeouts and ScrapingDog 403 are harmless backups.
- Supabase pooler cert self-signed → fingerprint pin (.supabase_fp.txt);
  delete the file to re-pin after a legitimate cert rotation

- LLM API migration: DONE — main path (thinker/narrator/reflections) live
      on DeepSeek V4 Flash (`MAIN_LLM_PROVIDER=deepseek`, non-thinking mode,
      shadow-replay-gated 8/8); Groq remains the rollback main provider and
      powers the evidence-only social reads. Usage/outcome accounting,
      shadow replay, and canary gates are implemented.
- Live execution is wired (now incl. the REF-R11 on-chain commit memo) but
      remains DISARMED; no mainnet execution is authorized. Arming is the
      operator's FINAL task (handoff §27): fund wallet (0.03 SOL + $3–5 USDC) →
      funded throwaway-keypair devnet drill (incl. memo step) → hand-flip the two
      hardcoded flags. No session may arm before every other task is done.

## Status
Live calibration day ~2. A11 thesis re-authoring shipped (2026-08-27, handoff
§30): the omo re-read (full local clone) found `thesis-author.server.ts` —
missed by the original audit — and it is now ported: open write-ups stale >6h
are advanced against current position numbers (≤2/tick, narrative-only,
fail-closed). Live-verified on the first tick (two write-ups advanced via
DeepSeek, retired row skipped, 0 tracebacks). The same re-read resolved both
remaining audit caveats verbatim (placeOrder guards: our executor is a strict
superset; their calibration factor: still unwired in their public code). The
code queue is EMPTY again — everything left is operator-gated (§27 arming),
post-calibration, or needs external credits/keys.
Dead-provider fail-fast shipped (2026-08-27, handoff
§24): a provider that times out / fails to connect twice in a row is now
benched exactly like a 402, so a dead scraper can never stall a tick again
(was ~15 min/tick when ScrapingBee ReadTimeouts went un-benched). Stealth
timeout cut 45s→25s and direct read gets 2 transport attempts (reference
parity). Reference audit confirmed its fomo fallback is the same Firecrawl
stealth-proxy we already call — no free scrape mechanism exists. Firecrawl +
ZenRows are credit-exhausted (402, benched); refilling Firecrawl restores real
crowd heat with zero code changes. Main LLM now DeepSeek V4 Flash (2026-08-27,
handoff §19): shadow-replay-gated, flipped via MAIN_LLM_PROVIDER, first
full tick verified (all thinker calls success, peak-window cost accounting
correct, zero tracebacks). Fresh $1,000 book. Supabase schema-drift incident
FIXED (2026-08-27): live book was missing events/memories/theses/llm_call_usage
(every tick died, system-status 500'd); `db_pg.init_db()` now self-heals via
idempotent `_SCHEMA_SYNC_SQL` — verified live: first full tick completed, all
endpoints 200. DB maintenance endpoint added
(2026-08-27): prune_feed_events, prune_market_regime, reset_book, POST /api/admin/reset.
REF-R1–R7 audited and confirmed correct. Dashboard v2 shipped
(2026-08-25): ENTER/PASS feed labels, verbatim model answers + contract
address in feed detail, five-number portfolio stats panel; knowledge tab +
paper banner removed. App runnable via ./start.sh (rebuilds frontend/dist).
**Tests: 474 combined passing** (backend 370 + live_execution 104; +4 wallet
regression tests this batch).
