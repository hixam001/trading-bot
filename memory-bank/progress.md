# Progress — trading-bot

## Works (all verified)
- [x] §49 anti-churn v2, one memory, both books (2026-08-30): audit vs
      the reference found the LIVE book had NO loss memory (auto-block
      existed only in the paper engine and only counted exit_stop_loss
      rule IDs; the reference has no automatic per-mint loss counter at
      all). blocklist.py is now the single source of truth:
      `record_close_outcome()` (PnL-based, any exit rule, both books,
      newest-first closes history capped at 10) feeds `maybe_autoblock()`
      (N=2 consecutive loss closes → block until a human clears it) +
      a 24h re-entry cooldown enforced in `filter_candidates` (read
      stage, both books, self-expiring, zero quota). Both books wired
      (paper close path AND `run_live_cycle._manage`): record →
      autoblock → (on loss) soft memory (upsert_memory + loss_close
      event — reference layer 5). Disclosure gains `anti_churn` truths.
      Audit-surfaced hardening: conftest now isolates BLOCKLIST_STATE_FILE
      (mock-tick tests had been polluting the operator's real sidecar
      and auto-blocking mock mints; file cleaned, backup kept);
      `_load()` fails OPEN on unreadable paths like on corrupt state.
      10 new tests. **647 passing** (495 backend + 152 live). Live
      cycle restarted; first ticks clean. NOTE: Firecrawl /v1/search is
      returning 402 (credits exhausted) — fail-soft handled it, but
      paid search evidence is empty until refilled.
- [x] §48 web-search spend discipline — staged + cached (2026-08-30): the
      Firecrawl `/v1/search` evidence stage left the unconditional read
      chain and became stage 4 of `gate_candidate_staged` (only
      all-passed/thinker-bound candidates get searched; §44-style
      `web search skipped … (no quota spent)` logs), behind a two-tier
      mint-keyed cache (hits 7200s / misses 1800s; same-ticker
      different-mint never share). `search_web` never-raises now.
      8 new tests; read-stage call pinned must-NOT-run. **637 passing.**
      Live: 3 searches in first ~2 min vs ~4–6/tick before (>95% cut).
      Deferred (designed): Brave→DDG→Firecrawl free-transport chain,
      provider counter, 1-week shadow.
- [x] §47 Scrapling local stealth transport — IMPLEMENTED & LIVE
      (2026-08-30): Phase-0 drill 6/6 per hop (curl-cffi p50 0.42s,
      browser p50 1.00s vs httpx 0/6 always-430) proved fomo's block is
      TLS-fingerprint, not IP. New `stealth_browser.py` (curl hop + warm
      AsyncStealthySession with CF solver, idle close 600s, recreate after
      3 failures) + `crowd.py` chain (curl-first direct, `_scrape_scrapling`
      before the paid providers, same bench machinery) + config knobs
      (`SCRAPLING_ENABLED=0` reverts) + pinned dep + Dockerfile browser
      layer + 9 new tests. **629 passing.** LIVE: every fomo read 200 via
      scrapling, ZERO paid /v1/scrape, ZERO 430s; Firecrawl credits now
      only fund the web-search stage. README credits added (GitHub only).
      Follow-ups: ~1-week shadow then optionally empty paid scrape keys;
      re-run spike on Oracle (IP-reputation check).
- [x] §46 Oracle Always Free deployment decision + Scrapling deferred
      (2026-08-30): engine deploys as the existing single Docker image
      (state on named volumes); sizing corrected to the VERIFIED Always
      Free allowance — **2 OCPU + 12 GB** always-on (1,500 OCPU-h +
      9,000 GB-h/month), NOT the 4/24 previously documented (oversized A1
      instances are disabled→deleted 30 days after trial end on free
      tenancies; PAYG upgrade keeps the free allowance); docs/11 updated.
      Frontend ships single-origin (baked SPA on :8000) or Vercel Hobby
      (`VITE_API_BASE_URL` + `FRONTEND_ORIGIN`). Scrapling analysis
      complete and DEFERRED to post-deploy: `scrapling[fetchers]` pip dep;
      `AsyncStealthySession` (`solve_cloudflare=True`, Privy bearer via
      `extra_headers`) first in the `crowd.py` chain; `_direct_get`
      upgraded to curl-cffi impersonation; Groq social reads off + Groq
      main code deleted; candidates 20→12, research 8→4, web-search 8→4;
      paid keys as 1-week failover; BSD-3 credit in the GitHub README
      ONLY (never the website); Phase-0 gate ≥80% solve success from the
      Oracle IP. Fresh Firecrawl credits added 2026-08-30 (paid chain
      primary until Scrapling lands).
- [x] §45 equity-proportional live minimum ticket (handoff §45) (2026-08-30):
      fixed the "site says ENTER but nothing executes" incident — TREE passed
      the staged gate every cycle, then sizing refused the $0.4962 ticket
      (cash $3.31 × 0.15) against the hardcoded $0.50 `MIN_LIVE_TICKET_USD`.
      New formula `live_execution/config.py::min_live_ticket_usd(equity) =
      max($0.10, at-cost equity × 0.10)` (operator chose 10% of 3.5%/5%/10%);
      ONE function drives the `_live_cash_available` gate rule, the sizing
      refusal, AND the risk_budget floor threading, so the gate and sizing can
      never disagree on the threshold (that disagreement was the root cause).
      Below-floor refusals now record in `outcome["entries"]` + a 4dp log
      line instead of vanishing. At-cost equity (cash + Σ position cost) —
      never price-dependent, fails closed to the $0.10 dust floor. Paper $25
      floor frozen; isolation intact; hardcoded never-env-settable. At the
      $4.59 incident book the floor is $0.4586 → the TREE ticket places.
      **620 tests green (468 backend + 152 live: §45 formula suite — scaling,
      dust clamp, fail-closed inputs, incident regression; cash-rule rewrite
      incl. over-deployed-book failure).** Live cycle needs a restart to load
      the new constants.
- [x] §44 staged gate: rules → fomo scrape → crowd rules → LLM (handoff §44)
      (2026-08-30): `decision_pipeline.gate_candidate_staged()` sequences the
      decision per candidate — every cheap (non-crowd) rule first
      (unconditionally), the fomo.fun scrape ONLY if they all passed, the
      crowd rule last against whatever the scrape returned. A candidate that
      fails any cheap rule is never scraped (`crowd scrape skipped for SYM:
      failed liquidity_floor (no quota spent)` in the log) and never costs an
      LLM call: the once-per-tick brain is unchanged, but the per-candidate
      thinker runs only when `gate.all_passed` (and now sees REAL crowd
      theses), while a rule-refused candidate gets the deterministic template
      write-up (`source="template:rules-refused"`, verdict forced "pass") so
      its journal row stays complete. `cheap_rules()`/`crowd_rules()`
      partition the injected list on `__name__` (incl. `LIVE_ACTIVE_RULES`);
      `gate.py::decision_from_results()` is the single definition of
      `all_passed`/`failed_rule_ids`/`not_evaluated_rule_ids`; assembled
      breakdowns keep the DECLARED rule order. Per-tick spend is now 1 brain
      call + N scrapes + N thinker calls, N = candidates passing all nine cheap
      rules, and the saving compounds within a tick (cash/slot exhaustion makes
      later candidates fail cheaply). Preserved: fail-closed skips, honest
      rejection stats, mock hermeticity (no scrape off live), dead-feed
      fail-soft. **615 tests green (464 backend + 151 live: staged-gate
      ordering/proxy/defer tests, both pipelines gate-before-think).**
- [x] §43 crowd-feed quota: shortlist-only `crowd_heat` lookups (handoff §43)
      (2026-08-30): the metered fomo.fun board read left the unconditional
      enrichment chain — `decision_pipeline.enrich_crowd_for_shortlist()` runs
      the same `evaluate_gate` on the same injected rule list MINUS the crowd
      rule (`cheap_rules()`) and fetches only for candidates that already
      cleared everything else; the rest get `crowd_lookup_deferred=True` and
      `crowd_heat` reports `evaluated=False` / "not evaluated" instead of a
      presence-proxy number. New `RuleResult.evaluated` +
      `GateDecision.not_evaluated_rule_ids`; the gate FAILS CLOSED on skips
      (`all_passed` = `passed AND evaluated`) and keeps them out of
      `failed_rule_ids`. Deliberate, scoped trade-off: rejects no longer show a
      real crowd number for that one field; everything else (all nine other
      rules always evaluated, breakdown completeness, honest rejection stats,
      mock hermeticity, dead-feed proxy fallback) preserved. Both books wired;
      dashboard shows a neutral `SKIP`. **610 tests green (460 backend + 150
      live: +6 rule/gate semantics, +6 pipeline behavior, +1 live delegation).**
- [x] §42 deployable restructure (handoff §42) (2026-08-30): `live_execution/`
      → `backend/live_execution/` + `run_live_cycle.py` → `backend/` (git mv,
      history kept); cross-package sys.path juggling removed everywhere;
      Dockerfile (SPA baked in) + docker-entrypoint.sh (armed+wallet gate) +
      docker-compose.yml (persistent state volumes) + .dockerignore;
      `WALLET_KEYPAIR_JSON` env channel + keypair log redactor + identity pin
      on both channels; `FRONTEND_ORIGIN` comma-list CORS; `solders` pinned
      in requirements; frontend `VITE_API_BASE_URL` + .env.example;
      docs/11_DEPLOYMENT.md; §42b root-cause fix of the live-state paths:
      `config.LIVE_STATE_DIR`/`BREAK_STATE_FILE`/`KILL_SWITCH_FILE` as the
      single source of truth, readers resolve per call, suite retargeted to a
      tmp dir (it had been reading the operator's REAL break state — 6 tests
      failed whenever the bot took a break); entrypoint fixes (missing
      shebang, unchecked `cd`, single-process `wait`, hardcoded health port).
      **597 tests green (448 backend + 149 live: +11 wallet-secrets, +6
      state-path co-location, +12 docker-entrypoint).**
- [x] §38 security audit + hardening (handoff §38) (2026-08-28): 20-rule
      checklist → 11 pass / 3 partial / 2 gaps / 4 N-A. Zero secrets in full
      git history; RLS ON everywhere; parameterized queries; npm+pip audits
      clean. Fixed: `.env`+keypair → 600; admin/ingest endpoints token-gated
      fail-closed (`api/auth.py`, X-Admin-Token); ingest 200k char cap;
      security-headers middleware; CORS narrowed to GET/POST+Content-Type.
      E2E made deterministic. 527 unit + 8 E2E green; live 403/200 verified.
- [x] §37 stale-holdings dust fix + FIRST REAL FILL + items 1 & 3 (handoff
      §37) (2026-08-28): one ledger bug → three symptoms (holdings ghost,
      ENTER blocked by MAX_OPEN_POSITIONS, journal/book disagree). Fix:
      `full_close` threaded `reduce_position`→`place_sell`→`place_order`→
      `_manage`; full close realizes PnL on full cost + one-time repair of the
      stuck dust row. Live proof: PINK buy gate=PASS → **FILLED $0.66 → 491.15
      tokens** (first real fill). Item 3 `CommitLog.reconcile_orphaned` healed
      7 memo-only orphans (0 ambiguous `published` left). Item 1 narrator
      anti-repetition rotation. 516 unit + 8 E2E green.
- [x] §36 live execution UNBLOCKED + Journal/Holdings restored (handoff §36)
      (2026-08-28): three stacked bugs fixed — quote verb (POST→GET via new
      `_get_json`, buy AND sell paths), `ExecutionError` import NameError,
      solders `from_bytes` (no `.deserialize` in 0.29); every post-memo
      failure now journalled via `logc.fail` + network phase catches all
      exceptions fail-closed; live proof: GTA6 sealed→memo→quote 200→blocked
      at 2.5% impact floor (5.30%) with honest journal reason; new read-only
      `/api/live/executions`; Journal + Holdings pages restored (three-page
      tab bar, proof-expand rows with solscan links); 506 unit + 8 E2E green.
- [x] §35 frontend rebuild + STATE_DIR fix (handoff §35) (2026-08-28):
      terminal design system (`frontend/DESIGN.md`, token-only
      `tailwind.config.js`, shared `lib/format.ts` + `components/ui.tsx`);
      all four live panels rebuilt (LiveBook / LiveFeed / MarketRegime /
      SystemStatus with real reasoning model + llm_usage_recent); feed
      REST-hydrates 50 rows then WS live-append (deduped); `term-*` tokens
      retired; fonts self-hosted; Playwright `npm run test:e2e` 5/5 (console-
      clean load, data/empty never blank, aria-expanded + Enter operability,
      offline banner). FOUND + FIXED: empty `LIVE_EXECUTION_STATE_DIR=` →
      `Path("")` = CWD → live CommitLedger at repo root; now or-fallback to
      `live_execution/state/`, ledger moved, gitignore entries added, +3
      tests. vite build clean; **501 backend tests + 5 E2E passing**.
- [x] §34 live-cycle hardening (handoff §34) (2026-08-28): two operator-reported
      issues with the ARMED live cycle. (1) 403-rejection benching
      (`backend/data_providers/crowd.py`): `_CONSECUTIVE_REJECTIONS` — a stealth
      provider whose proxy keeps getting refused by the origin (HTTP 403) is
      benched after two, exactly like a 402, instead of being re-tried on every
      candidate every tick; own counter (a 403 is a completed response so it
      must not clear the transport streak), reset on any 200. (2)
      Micro-bootstrap live cash rule (`run_live_cycle.py`): `LIVE_ACTIVE_RULES`
      swaps the paper `cash_available` (checks $100 `INTENDED_POSITION_SIZE_USD`)
      for `_live_cash_available` (checks `MIN_LIVE_TICKET_USD` $0.50); every
      other rule verbatim, paper rules calibration-frozen. Both live-verified
      ARMED: scrapingdog benched after 2× 403 (ScrapeOps served all 20), no
      `cash_available` gate failures, several `gate=PASS`, $5 book sizes $0.75
      ≥ floor. 11 new tests → **498 combined passing**.
- [x] §33 armed state committed + pushed (handoff §33) (2026-08-28):
      operator-directed ("push config as armed, no questions asked").
      `live_execution/config.py` committed ARMED (`LIVE_TRADING_ENABLED=True`,
      `REQUIRE_MANUAL_CONFIRMATION=False`); canary test re-purposed to pin the
      committed state; disclosure `armed` fixed to read the real flag (it
      previously always said False); README/handoff/report aligned with a
      clone-warning. 486 passing, fully green. §27 is now fully COMPLETE.
- [x] §32 cash-corruption fix + bad-quote guards + final omo audit (handoff
      §32) (2026-08-28): phantom-cash incident root-caused (bad quote ~2,960×
      → poisoned high-water → TP trim credited ≈$94k); two hardcoded
      fail-closed guards (`EXIT_PRICE_JUMP_MAX=50` scan-level skip/no-ratchet;
      `MAX_EXIT_PROCEEDS_MULT=200` proceeds backstop on close AND trim);
      book cash repaired; identical jump guard in the live `_manage` loop
      (3 tests on the exact incident numbers); 9 paper-guard tests. Live cash
      verified accurate by construction (chain USDC balance re-read every
      cycle, never accumulated). Final full-coverage omo audit (docs/09 §F):
      exit module exists-but-unpublished (their tests import it), calibration
      factor still unwired in their sizing (grep proof), wash-trade parity;
      no trading-critical gap remains; remaining deltas = UX polish / scale
      custody / hosting plumbing. Operator ARMED this machine (§27 human-only
      flags flipped by hand) — armed config deliberately uncommitted; the
      ships-disarmed canary test is red by design while armed. **486 tests;
      485 pass while armed**
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
- Live execution is fully wired (incl. the REF-R11 on-chain commit memo) and
      this repo is committed **ARMED** (§31/§33: devnet drill 5/5, then the
      operator's own hand-edit + explicit direction). Arming remains hardcoded
      and human-edit-only — no env bypass. Since §42 the engine is packaged
      for deployment (Docker + entrypoint double-gate: ARM flag AND a wallet
      secret), with wallet secrets resolvable from a mounted file or the env
      JSON channel, both fail-closed and identity-pinned.
      *(This bullet previously said DISARMED / arming-is-the-final-task —
      superseded on 2026-08-28.)*

## Status
**As of 2026-08-30 (§42/§42b): the repo is DEPLOYABLE.** `live_execution/`
and `run_live_cycle.py` moved inside `backend/`, so the engine is one
Docker-packaged module (entrypoint starts the live cycle only when armed AND
a wallet secret is configured, and exits non-zero if either half dies); the
dashboard deploys separately via `VITE_API_BASE_URL` or is served
same-origin. Wallet secrets resolve via `WALLET_KEYPAIR_PATH` (preferred) or
`WALLET_KEYPAIR_JSON` (in-memory), fail-closed, identity-pinned, redacted
from logs. §42b made `config` the single source of truth for live-state
paths after finding that two readers composed them off the old repo root —
which also revealed the suite had been reading the operator's REAL break
state (now a tmp dir, hermetic). Four entrypoint defects fixed (missing
shebang = image could not exec, unchecked `cd`, single-process `wait`,
hardcoded health port). **597 tests green (448 backend + 149
live_execution)**; engine restarted and verified on the new layout, all
endpoints 200, disclosure `armed: true` with a clear kill switch. Remaining
work is operator-side deployment only — `docs/11_DEPLOYMENT.md`.
Previous status (§39): roadmap items #1/#3/#6 shipped — security_clear is
the live 10th gate rule (KNOWN-bad-only), crowd heat discounts dumped thesis
authors (`FOMO_DUMPED_THESIS_WEIGHT`), and the paper + live pipelines run the
same read/think/gate core (`backend/decision_pipeline.py`; three live-side
drifts fixed: fake-chart filter, thinker template fallback, set_break arity).
550 tests + Playwright 8/8 green; live cycle restarted on the new code and
verified (security_clear in every /api/feed breakdown, 0 tracebacks).**
Previous status: Live calibration day ~2. A11 thesis re-authoring shipped (2026-08-27, handoff
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
**Tests: 527 backend + 8 Playwright E2E — fully green** (backend 399 +
live_execution 128; +11 security-hardening tests this batch; 8 E2E in
frontend/e2e/dashboard.spec.ts via `npm run test:e2e`).
The flag-state canary now pins the committed ARMED state — green while armed
(handoff §33/§34).

