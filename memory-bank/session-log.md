## Memory-bank update - 2026-08-29 (§41 out-of-band sell repair)

- **Task**: the operator manually sold a coin the bot held (during the
  broken-RPC window); it kept showing in Holdings.
- **Diagnosis**: GE8q5h6e…pump — chain balance 0, ledger buy still OPEN.
  reconcile() behaved exactly as designed (chain_excluded + "operator review
  needed" every cycle, never mutating the money ledger), but no sanctioned
  path existed to COMPLETE the review.
- **Shipped**: `ExecutionLedger.close_out_of_band` (honest P&L: known
  proceeds realize against summed cost; unknown = pnl None, never
  fabricated, skipped by the daily-loss breaker) +
  `live_execution/scripts/repair_vanished.py` (operator CLI, two safety
  gates: open-position check + chain-VERIFIABLY-0; journals did event +
  retires thesis; bookkeeping only — never executes/quotes/signs).
- **Repair executed live**: cycle briefly stopped (concurrent-write race on
  executions.json), closed with operator-confirmed proceeds 1.11715 USDC →
  realized +$0.5801, cycle restarted. Verified: GE8q5h6e gone from
  Holdings, 0 reconcile warnings (was 1/cycle), 0 tracebacks, and the cycle
  opened a fresh genuine fill (TIT $0.53 → 1306.93 tokens) minutes later.
- **Tests**: +6 → **568 passing** (ledger regression tests in
  test_ledger_full_close.py). Docs: handoff §41, decisionLog #54,
  session-log, project report §25.

## Memory-bank update - 2026-08-29 (§40 security_clear unblinded)

- **Task**: operator asked how omo gets token security via Dexscreener
  ("Birdeye returns errors too many times") — wanted omo's Dexscreener
  wired in as replacement.
- **Analysis verdict**: omo reads NO token security from anywhere (no
  authority/honeypot reads in their entire lib; their rug defenses are the
  fake-chart filter + liquidity floors). Dexscreener's API has no
  authority/honeypot fields, so nothing CAN be ported from either source —
  our on-chain RPC security read is a differentiator omo lacks, but ours
  was blind.
- **Bug 1 (critical, shipped-dead)**: `onchain_security.get_authority_flags`
  POSTed `{"method","params"}` without the JSON-RPC envelope. mainnet-beta
  answers 200 + EMPTY body (rate-limit masquerade — visible via negative
  `x-ratelimit-endpoint-remaining`); publicnode answers 400 Parse error.
  `resp.json()` threw on both, so the fallback NEVER worked and every live
  `security_clear` read `unknown, unknown, unknown`. The full-envelope
  pattern was already proven in-repo (`live_execution/solana.py`).
- **Bug 2**: Birdeye key quota-exhausted — 400 "Compute units usage limit
  exceeded" on both trending + token_security, 3 retries + backoff each,
  1,371 error lines in one live log.
- **Fixes**: full envelope + empty-200-rotates + non-mint-reject in
  `onchain_security.py`; `ProviderQuotaError` in `base.py` (quota-phrase 400
  raises immediately, generic 400 keeps retry path); session self-disable on
  both Birdeye surfaces (security already had it for 401/403 — added quota;
  trending gained both, operator approved).
- **Live proof before commit**: `get_authority_flags(MEW)` → both
  authorities revoked=True from the real chain; live cycle restarted on the
  fixed code; every `security_clear` detail now reads `mint authority
  revoked: yes, freeze authority revoked: yes`; Birdeye burn = one
  session-disable line (now 401 tier-denial — same path); 0 tracebacks.
- **Tests**: +12 → **562 passing** (new `test_onchain_security.py`).
  Docs: handoff §40, decisionLog #53, activeContext, this log, project
  report §24.

## Memory-bank update - 2026-08-29 (§39 roadmap items #1, #3, #6)

- **Task**: implement the three approved roadmap items — activate
  `security_clear` (live + paper), crowd author-P&L attribution (omo
  exit-liquidity-dump parity), and unify the paper + live pipelines.
- **#1 security_clear live**: added to `ACTIVE_RULES` between
  `already_held` and `not_on_break` (10 rules now). KNOWN-bad-only (B10):
  fails on `mint_authority_revoked is False` or `is_likely_honeypot is
  True`; None/unknown always passes. One edit activated both books
  (`LIVE_ACTIVE_RULES` derives from `ACTIVE_RULES`). Tests updated: the
  `== 9` pin, the exact-list pin (now asserts `security_clear` present),
  and the no-short-circuit expectation set. Mock `HONEYPT` archetype once
  again exercises the honeypot refusal.
- **#3 dumped-author heat discount**: `crowd.py::_is_dumped` (author closed
  at realized profit) → `fetch_fomo_theses` returns `dumped_count` +
  `effective_total`; `enrich_crowd_heat` consumes `effective_total`
  (fallback to raw `total` for older payloads). New env knob
  `FOMO_DUMPED_THESIS_WEIGHT` (default 0.0). `total` stays the board's raw
  number. Unknown authorTrade = full credit (fail-soft). +6 tests.
- **#6 unified core**: new `backend/decision_pipeline.py` —
  `read_candidates` (blocklist + fake-chart), `enrich_candidates`
  (live-only chain, paper order), `think_candidate` (template fallback),
  `apply_break` (correct `set_break` arity), `gate_candidate` /
  `entry_decision` (rule-list injection). Paper `run_tick` and live
  `run_cycle` delegate; sizing/seals/ledger/exits stay per-book. Fixed
  three live-side drifts found during extraction: missing fake-chart
  filter, thinker exception killing the cycle, `set_break(minutes,
  reason)` missing the `taking` arg (latent TypeError). Isolation intact
  (backend imports only backend; pinned by test). +13 backend
  characterization tests + 4 live parity tests (paper-vs-live decisions
  differ in EXACTLY the cash rule).
- **Verification**: **550 passing** (backend 418 + live 132), Playwright
  8/8, live cycle restarted and verified on the new code — clean cycle,
  `security_clear` present in every `/api/feed` rule breakdown (10 rules),
  0 tracebacks. Docs aligned: handoff §39, docs/06/07/09,
  FOMO_INTEGRATION.md §2, rules.py header, decisionLog #52, activeContext,
  progress Status.

## Memory-bank update - 2026-08-28 (§38 security audit + hardening)

- **Task**: operator-requested full-codebase audit against a 20-rule security
  checklist (hide keys, purge git secrets, RLS, encryption, auth, parameterized
  queries, validation, escaping, uploads, response trimming, security headers,
  HTTPS, dependency scans, etc.).
- **Verdict**: 11 pass / 3 partial / 2 gaps / 4 not-applicable. No accounts,
  login, cookies, or public surface exist (rules 9–12 have nothing to protect);
  the security perimeter is loopback + `.env` + the wallet keypair.
- **Verified clean**: full-history git scan = ZERO committed secrets; Supabase
  RLS ON for all 13 tables with zero permissive policies; both DB layers fully
  parameterized; `npm audit --omit=dev` 0 vulns; `pip audit` (33 pkgs) 0 known
  vulns; React auto-escaping (no dangerouslySetInnerHTML).
- **Fixed**: F1/F2 `.env` + drill keypair → 600; F3 new
  `api/auth.py::require_admin_token` gates admin reset + KB ingest via
  X-Admin-Token (constant-time, FAIL CLOSED when unset); F4
  `MAX_INGEST_CHARS=200000` cap; F5 security-headers middleware (nosniff /
  DENY / no-referrer / no-store on /api/*); F6 CORS narrowed to
  GET/POST + Content-Type.
- **Accepted risk**: HTTP on loopback only (never leaves the machine; all
  external calls TLS). **Operator item F7**: GitHub repo is public — consider
  making it private.
- **E2E determinism**: skeleton check now polls ≤45s; feed tests accept rows OR
  the documented empty state (the earlier 3 failures were cycle-timing, not a
  regression — journaling re-verified live).
- **Tests**: +11 (`test_security_hardening.py`) → **527 passing**; Playwright
  **8/8**. Live-verified: headers present; admin reset 403/403/200
  (no-token/wrong/real); kb ingest 403; all endpoints 200; 0 tracebacks.
- Handoff §38; decision #51.

## Memory-bank update - 2026-08-28 (§37 stale-holdings dust fix + FIRST REAL FILL + items 1 & 3)

- **Task**: operator reported "the bot bought a token, then sold it, yet it
  still shows up in holdings … journal even shows it's closed" and "it shows
  enter but also doesn't enter and shows failed txns". Then: implement the two
  deferred omo-audit items (1 narration anti-repetition, 3 fill-linking).
- **Root cause (one bug, three symptoms)**: a reconcile-clamped FULL exit made
  a sell fraction just under the 0.999 close threshold (chain/journal dust,
  e.g. 0.99889), so `reduce_position` booked a trim and left a dust row OPEN —
  it showed in Holdings, counted against `MAX_OPEN_POSITIONS=3` (blocking every
  ENTER with "would hold 4 mints"), and disagreed with the journal. The
  "failed txns" was the GTA6 2.5%-impact guard correctly refusing a 5.30% trade.
- **Fix**: threaded `full_close` through `models.reduce_position` →
  `executor.place_sell` → `place_order` → `run_live_cycle._manage`
  (`decision.action == "close_full"`); full close realizes PnL on full cost +
  one-time repair flipped the stuck `2NffKvfZ…` row closed (backup kept).
- **Live proof**: freeing the slot unblocked entries — PINK `think=buy
  gate=PASS` → memo → quote GET 200 → **FILLED $0.66 → 491.15 tokens** (first
  real fill), venue attributed.
- **Item 3**: `CommitLog.reconcile_orphaned(600s)` marks old memo-only
  `published` commits failed/no-fill (reuses `failed` status), wired at
  `run_cycle` start; healed 7 orphans → commits.json `{failed:8, bound:5}`,
  0 ambiguous. **Item 1**: narrator rotates style angles + template openers
  (style-only, grounding intact).
- **Tests**: +10 (3 full-close, 4 reconcile, 3 rotation) → **516 passing**;
  all endpoints 200, 0 tracebacks after restart. Handoff §37.

## Memory-bank update - 2026-08-28 (§36 live execution unblocked + Journal/Holdings restored)

- **Task**: operator reported "the bot says enter but it doesn't execute any
  transaction" and "the journal page and the other page is gone".
- **Root cause (three stacked bugs, all in `live_execution/`)**: (1) the buy
  quote POSTed to Jupiter's GET-only `/swap/v1/quote` → 405 ×3 (the sell path
  had its own inline POST quote — same bug); (2) `executor.py` caught
  `ExecutionError` in four except-clauses without importing it → the first
  quote failure crashed the cycle with NameError; (3) solders 0.29 has no
  `VersionedTransaction.deserialize` (parse constructor is `from_bytes`) —
  the first order that survived the quote (GTA6: quote 200, swap 200) died at
  signing. None of the three was reachable by the mocked test suite; each was
  fail-closed (no order ever went out wrong), but together they blocked all.
- **Fixes**: new `_get_json` GET helper (buy + sell quotes), import fix,
  `from_bytes`, and honest-journal hardening — every post-memo failure now
  calls `logc.fail(hash, reason)` and the network phase catches all
  exceptions fail-closed (no crash can leave a commit stuck at `published`
  without explanation again).
- **Live proof (ARMED mainnet)**: GTA6 `think=buy gate=PASS` → sealed → memo
  on-chain → quote GET 200 → blocked at the 2.5% price-impact floor (5.30%)
  → journalled `failed | price impact 5.30% above floor 2.5%`. Zero cycle
  crashes since; the first fill is market-dependent (needs a candidate under
  the impact floor) — the machinery is proven end-to-end.
- **Restored pages**: Journal (order decisions with lifecycle badges +
  expandable proof: fail reason, commit hash, memo/fill solscan links — plus
  the money ledger) and Holdings (live positions detail), behind a new
  read-only `/api/live/executions`; three-page tab bar (dashboard/holdings/
  journal). The pages were removed with the paper components in b49bb10; they
  are back as live-only views.
- **Tests**: +5 unit (GET-verb MockTransport proof, buy/sell quote-failure →
  failed-not-NameError, sell full-flow fill + ledger reduce, real-solders
  signing round-trip) → 506 passing; +3 E2E (tab nav, holdings, journal
  proof-expand) → 8/8 Playwright.
- **Docs**: handoff §36 + header counts; activeContext/progress/decisionLog
  #49; project report §20. Committed + pushed (no contributor trailers).

## Memory-bank update - 2026-08-28 (§35 frontend rebuild + STATE_DIR fix session)

- **Task**: "rebuild the frontend, use all .clinerules skills especially
  awesome-design-skills, install and use Playwright, completely functional,
  fully wired, not vibecoded." A reported Qwen3.8 "text-only" Groq error was
  checked first — it was the operator's own chat tool, NOT the bot (Groq
  social reads 8/8 HTTP 200 in live logs at that time, DeepSeek brain OK,
  0 errored usage rows, error absent from every log). Diagnosis documented;
  no code change needed.
- **Design system**: `frontend/DESIGN.md` = new source of truth (synthesized
  from awesome-design-skills mono/sleek/impeccable + defense-first +
  performance-discipline). Token-only colors in `tailwind.config.js`
  (ink/panel/raised/line surface; bright/body/dim/faint text; pos/neg/warn/
  info semantics; JetBrains Mono data + Inter labels; flat 6px panels, no
  shadows; 8pt grid; five required states; a11y gates). Old `term-*` tokens
  fully retired — grep: 0 refs, 0 hex literals outside the token file.
- **Code**: new `src/lib/format.ts` (verbatim-value formatters, signed
  money, `—` for null, NO client-side math) + `src/components/ui.tsx`
  (Panel/Stat/Badge/Skeleton/Empty/ErrorState). All four live panels rebuilt:
  LiveBook (headline equity strip + positions table), LiveFeed (accessible
  expand/collapse, contract copy, verbatim model answer, rule breakdown),
  MarketRegimePanel, SystemStatus (shows the real reasoning model + recent
  LLM calls). Feed now REST-hydrates `/api/feed?limit=50` on mount then
  live-appends over WS, deduped by id — reloads are never blank.
- **Playwright**: `npm i -D @playwright/test` + `playwright.config.ts`
  (against the running backend on :8000) + `e2e/dashboard.spec.ts` — 5
  tests: zero console errors on load, all panels reach data/empty (never
  blank, no stuck skeletons), feed expand/collapse with aria-expanded, Enter-
  key operability, offline banner on route-abort. **5/5 passing**. npm
  scripts `test:e2e` / `test:e2e:ui` added; `frontend/test-results/`
  gitignored.
- **BUG FOUND + FIXED while wiring** (this is the important one):
  `LIVE_EXECUTION_STATE_DIR=` (EMPTY) in `.env` → `os.getenv(name, default)`
  returns `""`, so `Path("")` = CWD → the live CommitLedger (`commits.json`
  — real order nonces/seals) was written at the REPO ROOT, one `git add -A`
  from being published. Fixed to `os.getenv("LIVE_EXECUTION_STATE_DIR") or
  <default>`; stray `commits.json` moved into `live_execution/state/`;
  `/commits.json` + Playwright artifacts added to `.gitignore`; +3 tests
  (`live_execution/tests/test_state_dir.py`: empty-unset fallback, explicit
  override wins). App restarted clean; no root ledger.
- **Verification**: `npm run build` clean (tsc strict + vite); Playwright
  5/5; **pytest 501 passing** (backend 385 + live_execution 116); restart
  smoke all 200; screenshot review of the running dashboard.

## Memory-bank update - 2026-08-28 (§34 live-cycle hardening session)

- **Task**: two operator-reported issues with the now-ARMED live cycle:
  (1) "if Firecrawl credits are finished, there's still ScrapingBee and
  ScrapingDog tokens left" — dead stealth scrapers were being re-tried every
  candidate every tick; (2) "when $5 is all the cash, if it passes on tokens it
  should buy as well — it's not working as intended" — the paper cash rule
  refused every live entry.
- **Fix 1 — 403-rejection benching** (`backend/data_providers/crowd.py`):
  Forensics in `logs/live_cycle.log` showed Firecrawl (402 credits) and ZenRows
  (402 credits) benching correctly, but ScrapingDog's proxy refused by the
  fomo.fun origin (HTTP 403 — can't pass that endpoint's Cloudflare even with
  forwarded headers) on EVERY candidate, and ScrapingBee ReadTimeout-ing; only
  ScrapeOps gets through. Added `_CONSECUTIVE_REJECTIONS` +
  `_rejection_error`/`_rejection_success`: two consecutive 403s bench a provider
  30 min exactly like a 402; own counter because `_transport_success` resets the
  transport streak on any completed response; a 200 resets the rejection streak.
  Wired into `_scrape_get_template`.
- **Fix 2 — micro-bootstrap live cash rule** (`run_live_cycle.py`): the paper
  `cash_available` rule checks cash vs `INTENDED_POSITION_SIZE_USD` ($100, sized
  for the $1,000 paper book); the live book starts from a few USDC (REF-R11) and
  sizes from `MIN_LIVE_TICKET_USD` ($0.50), so the paper threshold refused every
  entry before sizing. `LIVE_ACTIVE_RULES` swaps in `_live_cash_available`
  (checks the live floor); every other rule verbatim; paper `ACTIVE_RULES` +
  `INTENDED_POSITION_SIZE_USD` untouched (calibration-frozen). `run_cycle`
  evaluates `LIVE_ACTIVE_RULES`.
- **Tests**: +11 → **498 combined passing** (backend 385 + live_execution 113).
  4 in `test_crowd.py` (two-403s-bench, single-403-transient, 200-resets-streak,
  transport/rejection counters independent) + 7 in `test_live_cash_rule.py`
  (only-cash-rule-swapped, paper-rule-frozen, live-floor pass/fail,
  gate-outcome-flips, run_cycle-uses-live-rules). `fresh_state` fixture resets
  the new counter.
- **Live verification (ARMED)**: system was cleanly shut down, restarted via
  `./start.sh` (ARMED + live cycle). First cycle: `scrapingdog: 2 consecutive
  origin rejections (403) — benching`, called exactly 2× then skipped, ScrapeOps
  served all 20 candidates; no `cash_available` in any failed-rule list, several
  `gate=PASS`; DeepSeek 200 OK on every think call (no degradation). Current
  refusals are the model returning verdict "pass" not "buy" — the model veto
  working as designed. With `SIZING_MODE=fixed` a $5 book sizes
  `min(5×0.15, 150) = $0.75` ≥ the $0.50 floor, so a model "buy" + gate pass now
  places a micro-order. system-status / live/portfolio / disclosure.json all 200.
- **Docs**: handoff §34 + header test count; memory-bank updated; project report
  updated. Committed + pushed (no contributor trailers).

## Memory-bank update - 2026-08-28 (§32 cash-corruption fix + final omo audit session)

- **Incident**: operator reported an outrageous cash balance on the dashboard
  after closing `neet`. Forensics: a transient bad Jupiter quote priced the
  ~$0.04 token at $119.0648 (~2,960×); the 15s exit scanner ratcheted
  high-water on the poisoned mark and a TP trim credited ≈$94k of phantom
  cash into the paper accumulator. Root-cause class: exit math trusted a
  single unbounded price sample for a money write.
- **Fix**: two hardcoded fail-closed guards in `backend/config.py` —
  `EXIT_PRICE_JUMP_MAX=50` (scan-level: a price 50× the established peak is a
  bad quote → skip the scan, do NOT ratchet high-water; upward-only so a
  genuine collapse still exits) and `MAX_EXIT_PROCEEDS_MULT=200`
  (close/trim proceeds backstop refused BEFORE any state write). Both
  deliberately generous — they only trip on data errors. Book cash repaired
  to the true accumulator value (one-off script). 9 tests
  (`test_exit_price_guards.py`).
- **Live parity**: a live sell can never fabricate money (real swap; cash is
  chain truth) but a phantom-spike early exit is real harm → identical jump
  guard in `run_live_cycle._manage` (skip cycle, high-water untouched).
  3 tests (`test_manage_jump_guard.py`) on the exact incident numbers.
- **Operator question answered**: live cash is accurate BY CONSTRUCTION —
  every cycle reads the wallet's real on-chain USDC balance
  (`getTokenAccountBalance`; missing account = 0.0; unreadable = None →
  cash 0, no entries, executor refuses). It is never accumulated, so the
  paper failure mode cannot occur; A2 reconciles token quantities vs chain.
  Dashboard cash = the PAPER book; live truth = on-chain balance.
- **Final omo audit** (docs/09 §F, full-coverage re-read, repo unchanged at
  48a86f9): (1) `exit.server.ts` EXISTS but is unpublished — proven by their
  own `exit-rules.test.ts` importing it; a fresh clone cannot run their
  tests; the pinned exit contract matches our public engine. (2) calibration
  factor STILL unwired in their sizing (grep: `convictionFactor` only in
  learn.server.ts). (3) wash-trade filter parity confirmed
  (market.server.ts:237). Remaining deltas: narration anti-repetition (queued
  UX item), memo burner key (documented §26 deviation), hosting plumbing.
  Verdict: no trading-critical parity gap remains.
- **Arming**: the operator performed the §27 human-only steps — hand-edited
  `LIVE_TRADING_ENABLED=True` + `REQUIRE_MANUAL_CONFIRMATION=False`. That
  edit is LOCAL operational state and deliberately NOT committed (repo ships
  disarmed); `test_safety_flags_are_hardcoded_safe_defaults` is the canary —
  expected red while armed. **486 tests; 485 pass while armed.** Rollback:
  one line.
- **Docs**: handoff §32; docs/09 §F; project report updated.

## Memory-bank update - 2026-08-28 (§33 armed state committed session)

- **Task**: operator directed "push config as armed, no questions asked just
  push" — committing the operator's own §27 arming edit to
  `live_execution/config.py` (`LIVE_TRADING_ENABLED=True`,
  `REQUIRE_MANUAL_CONFIRMATION=False`). The safety trade-off (a fresh clone
  is armed by default) was raised in plan mode; the operator overrode
  explicitly. Done exactly as directed.
- **Secrets**: diff scanned — no secrets in the committed change; all keys
  remain in the gitignored `.env`; wallet keypair file gitignored.
- **Collateral fixes so the pushed repo is green and truthful**:
  (1) canary test re-purposed to `test_safety_flags_match_the_committed_state`
  (pins committed state; any silent flip either way fails loudly);
  (2) `/api/disclosure.json` `armed` latent bug — it read a nonexistent
  backend-config attribute and ALWAYS said False (would have lied "disarmed"
  while armed); now reads the real live_execution flag via the sanctioned
  function-local optional import (fail-closed False if absent);
  (3) config.py header, README (section renamed "Live trading
  (operator-ARMED)" + clone-warning + arming record), handoff §1/§3/§27/§32
  updated, new handoff §33 record, project report updated.
- **Tests**: 486 passing — suite fully green again (was 485 + 1 expected-red).
- **Unchanged safety layers**: no env bypass; kill switch, daily-loss
  breaker, caps, identity pin, SOL reserve, memo-before-fill all active.
  Rollback: one line (`LIVE_TRADING_ENABLED = False`).
- **§27 is now fully COMPLETE.** Nothing in the handoff remains gated on
  arming.

## Memory-bank update - 2026-08-28 (§27 pre-flight + DEVNET DRILL PASSED session)

- **Refusal first**: the session opened with a request to "move live execution
  into backend/ and wire it all up, make sure live execution is enabled".
  REFUSED per handoff §1 ("if a task ever seems to require real execution
  inside backend/ — stop and flag it"), §27 ("no session may arm, or propose
  arming"), live_execution/config.py header (no env bypass by design), and
  defense-first skill rule 3. Operator chose the safe path: pre-flight +
  devnet drill together, human-only flag flips afterwards.
- **Pre-flight (all green)**: arm flags disarmed, kill switch clear, confirm
  CLI OK, state dir writable, solders 0.29.0, devnet + configured mainnet RPC
  reachable. Throwaway drill keypair generated (solders byte-array JSON at
  ~/.config/solana/drill-keypair.json); `.env` WALLET_KEYPAIR_PATH +
  EXPECTED_WALLET_ADDRESS set (a stale empty duplicate template line removed;
  dotenv last-wins verified). Operator-pinned address mismatch surfaced by the
  identity pin exactly as designed; resolved by re-pinning to the generated
  wallet (operator-approved).
- **Two latent bugs found by the first REAL keypair load** (commit d8e426f):
  (1) wallet.load_keypair passed the file PATH to solders from_json (expects
  JSON CONTENT) — every real load fail-closed with "expected value at line 1
  column 1"; fixed to from_bytes on the already-validated array + exactly-64-
  u8 check. (2) drill.py used an undefined `log` (NameError on step 1) and
  run_live_cycle ran --drill before logging.basicConfig. Both fail-closed and
  invisible to the mocked suite; both would have blocked arming day. +4
  regression tests incl. the previously-missing success path → **474 combined
  passing** (backend 370 + live_execution 104).
- **Drill PASSED 5/5** (devnet, wallet funded via faucet.solana.com after the
  RPC requestAirdrop faucet hit its daily limit — 429 on 4 amounts × 2
  endpoints): wallet/identity pin, balance 1.0 SOL, chain decimals=9, real
  signed dust transfer broadcast + confirmed (slot 489023339), REF-R11
  publish_commit_memo end-to-end (slot 489023363). Exit 0.
- **Docs**: handoff §31 (record) + §27 checklist state (preconditions all
  checked; steps 1–3 done for the throwaway devnet wallet); project report
  §16; memory-bank updated.
- **Still DISARMED**: LIVE_TRADING_ENABLED=False, REQUIRE_MANUAL_CONFIRMATION=
  True untouched. Remaining operator-only steps: mainnet wallet funded (0.03
  SOL + $3–5 USDC), `.env` re-pointed, the two hand-edited flag flips,
  supervised `run_live_cycle.py --once`.

## Memory-bank update - 2026-08-27 (A11 thesis re-authoring session)

- **Task**: the handoff code queue was COMPLETE (§29), so this session re-read
  the reference (`omotrades/omo`, full local clone — commit 48a86f9, unchanged
  since the audit) to find any remaining parity gap. Found ONE: the original
  audit's module list missed `src/lib/thesis-author.server.ts` (thesis
  re-authoring). Also resolved both "not verbatim-verified" audit caveats:
  (1) `placeOrder`'s guard block is now verbatim-readable — our
  `live_execution/executor.py` guards are a strict superset; (2) their
  calibration factor is STILL not wired into their sizing (`computeBudget`
  takes no factor; `ticketUsd(cash, conviction)` uses crowd heat) — our
  REF-R8×REF-R9 wiring remains ahead. Their `exit.server.ts` is still missing
  from the public repo (README mentions it; raw 404) — nothing to port.
- **What shipped**: `backend/thesis_restate.py` — pure selection/validation
  helpers + `restate_theses(conn, positions, price_map)` (never raises). Due =
  open AND (stale >6h OR not model-authored OR unparseable updated_at); ≤2
  rows/pass, oldest first; under-60-word rewrite contract validated fail-
  closed (<20/>1000 chars rejected, old text kept). NARRATIVE ONLY: writes
  theses.thesis/author/updated_at and nothing else; the UPDATE is guarded by
  closed_at IS NULL (retired-mid-pass rows untouched). Reuses the tick's own
  price_map (zero extra network I/O — documented deviation from the
  reference's per-row tape fetch). Main provider via build_main_client
  (json_mode=False, task="thesis_restate"); usage accounted success AND
  degradation; each rewrite journaled as a `did` event; DeepSeek peak-window
  skip; mock mode no-op.
- **Wiring**: `get_open_theses()`/`update_thesis_text()` in BOTH db.py +
  db_pg.py (lockstep); `main.py run_tick` pass after the risk-budget block;
  `run_live_cycle.py` pass after `_manage` (outcome `thesis_restatements`);
  `/api/disclosure.json` `thesis_restatement` block (stale_hours/per_pass/
  scope). Config: `THESIS_RESTATE_STALE_HOURS=6.0`/`THESIS_RESTATE_PER_PASS=2`
  hardcoded (cadence knobs of a narrative-only job).
- **Tests**: +26 → **470 combined passing** (test_thesis_restate.py:
  selection math, validation bounds, hand-computed P&L reuse, DB behaviors
  incl. retired-mid-pass guard, PG surface parity, mocked-HTTP orchestration).
  Isolation grep clean (no new backend→live_execution references).
- **Live smoke**: backend restarted; first tick advanced BOTH stale open
  write-ups (aura +3.5% mark; ANSEM −8.1% with tightened invalidation) via
  model:deepseek:deepseek-v4-flash, two `did` events journaled, retired `neet`
  skipped; system-status/theses/disclosure/proof/verify/binding all 200;
  `armed=false`, `paper_only=true`; 0 tracebacks.
- **Docs**: handoff §30 (implementation record) + §28 queue/header/test
  counts; docs/09 gains row A11 + resolved caveats; project report updated.
- **Still DISARMED**: `LIVE_TRADING_ENABLED=False` untouched. §27 (enable live
  execution) remains the operator's FINAL task — no session may arm.

## Memory-bank update - 2026-08-27 (omo-audit code queue session: A7/A6/A3/A2/A4)

- **Task**: close the five parity gaps surfaced by the 2026-08-27 audit of
  `omotrades/omo` (docs/09_OMO_AUDIT_COMPARISON.md), per operator instruction. Reference files fetched
  raw: market.server.ts (isFakeChart), blocklist.ts, wallet.server.ts
  (readViaRpc/getWalletSnapshot/fetchFillVenue), fomo.server.ts (readOwnBasis),
  execute.server.ts.
- **A7**: `backend/rule_engine/fake_chart.py` — all 13 thresholds ported;
  `Candidate.volume_5m_usd` added (models/dexscreener/discovery); wired into
  `main.run_tick` READ stage before think/gate. Deviation: unknown age/fdv
  skip rather than fail (documented §29).
- **A6**: `blocklist.py` gains `BLOCKED_SYMBOLS` (omo's exact list) +
  `is_blocked_symbol()`; enforced in `filter_candidates()`.
- **A3**: `live_execution/venue.py` (pure parser + fail-soft fetch);
  `decision_commits.venue` (SQLite + PG self-heal + `004_fill_venue.sql`);
  `bind_commit_venue()` both db layers; journaled in `run_live_cycle` after
  fills; `/api/binding.json` shows venue on every pair.
- **A2**: `solana.get_token_balances()` (legacy + token-2022; empty-vs-
  unreadable distinction) + `live_execution/reconcile.py` (pure). Chain =
  authority on quantities; journal = authority on cost; ledger never mutated
  by a chain read; exit sizing clamped to chain; vanished positions excluded
  + flagged; unjournaled holdings flagged, never added. Cycle outcome gains
  `chain_reconciliation`. Deliberate deviation from omo's full re-derivation:
  our §5.1 atomic journal stays the money authority (safer at micro scale).
- **A4**: `crowd.py` refactored — shared cached `_thesis_payload()`;
  `fetch_fomo_theses` contract unchanged; new `read_own_basis()` (raw-board
  match, no substantive filter on our own row, invested floored at 0, cap 10).
  `FOMO_OWN_HANDLE` env (default disabled). `_crosscheck_basis()` in
  run_live_cycle logs mismatches (tolerance max(5%,$0.50)), never applies
  them. Cycle outcome gains `basis_crosscheck`.
- **Tests**: +65 since REF-R11 → **444 combined passing** (test_fake_chart,
  test_reconcile ×10, test_token_balances ×7, test_own_basis ×9, test_venue,
  extended churn guards). Isolation grep clean. Live smoke disarmed:
  verify/binding/disclosure/proof all 200, `venue: null` on unbound pairs,
  `armed=False`, 0 tracebacks.
- **Docs**: handoff §28 code queue marked complete + new §29 implementation
  record; §27 (enable live execution) untouched and STILL the final task.

## Memory-bank update - 2026-08-27 (REF-R11 on-chain precommit memo + micro-bootstrap session)

- **Task**: implement the last implementable reference-parity gap — REF-R11
  on-chain precommit memo (commit–reveal) — operator-approved, using
  `omotrades/omo` (`precommit.server.ts`/`verify.server.ts`) as reference. Also
  folded in the micro-bootstrap accommodations so the live book can start from
  0.03 SOL (fee reserve) + $3–5 USDC (capital) and compound.
- **What shipped**: `live_execution/memo.py` (memo build + fail-closed
  `publish_commit_memo`); `commit_log.py` `sealed→published→bound` +
  `record_memo()`/`fail()`; `executor.py` order = guards→wallet→SOL reserve→
  USDC funding→seal→memo→CONFIRM memo→quote→build→send→confirm→bind (memo
  precedes the quote so the quote→fill window is unchanged); `solana.py`
  `get_usdc_balance()`; `run_live_cycle.py` real-USDC cash + journals seal+memo
  into `decision_commits`. Verifier surface: `memo_signature`/`memo_slot`
  columns (SQLite + PG self-heal + `migrations/supabase/003_commit_memos.sql`),
  `bind_commit_memo()`/`get_commit_id_by_hash()` in db.py+db_pg.py,
  `/api/verify.json` memo checks (hash-on-chain + slot ordering; unknown never
  pass), `/api/disclosure.json` `commit_memo` block. Sizing floor threaded
  through `compute_ticket`/`compute_risk_budget` (paper bit-identical). Devnet
  drill now sends a real memo.
- **Fail-closed guarantee (tested)**: a memo that cannot be confirmed BLOCKS the
  fill — the fill send is never attempted. USDC insufficient/unreadable and SOL
  below reserve all refuse BEFORE any on-chain commitment.
- **Deviations from the reference (documented handoff §26)**: fail-closed
  blocking (reference publishes async), immediate reveal, single signer = the
  trading wallet, de-branded `commit:v1:` prefix.
- **Bug found + fixed**: solders 0.29 requires `Hash.from_string()`; `drill.py`
  had a latent incompatibility (it had never run because solders was absent).
  Installed solders 0.29.0 into `.venv`.
- **Tests: 379 combined passing** (backend 308 + live_execution 71; +41 new,
  all offline/hermetic with hand-computed hash fixtures). Isolation grep clean.
  Live smoke (disarmed): verify/binding/disclosure all 200, `armed=False`,
  `paper_only=True`, 0 tracebacks.
- **Docs**: handoff §26 (implementation) + §27 (FINAL TASK: enable live
  execution — operator-only arming checklist); §22 status flipped; §8 next-steps
  points to §27; file map + test counts updated. memory-bank activeContext /
  progress / decisionLog (#41) / session-log updated.
- **Still DISARMED**: `LIVE_TRADING_ENABLED=False` + `REQUIRE_MANUAL_CONFIRMATION=True`
  untouched. Arming is the operator's final manual task (§27) — no session may
  arm before every other task is done.

## Memory-bank update - 2026-08-27 (fresh scraper keys session)

- **Operator added new keys** to the repo-root `.env` (what `load_dotenv()`
  resolves from `backend/`): Firecrawl, ScrapingBee, ScrapingDog, ScrapeOps.
  ZenRows unchanged (still 402-exhausted). Backend restarted to load keys +
  clear in-memory benches.
- **Code**: `_scrape_scrapingdog` was wired but did NOT forward the Privy
  bearer (prod-api requires it). ScrapingDog docs confirm `custom_headers=true`
  + headers-on-request (same as ScrapeOps keep_headers, no extra cost). Now
  appends `&custom_headers=true` and passes `fwd_headers=dict(headers)`.
- **Live-verified**: Firecrawl (new key) 15× 200 OK = real crowd heat restored;
  ScrapeOps (new key) 1× 200 OK failover (caught a transient firecrawl 500);
  0 tracebacks; no 15-min stalls.
- **Per-provider**: Firecrawl ✅ primary · ScrapeOps ✅ failover · ScrapingBee
  ⚠ ReadTimeout (can't forward bearer) · ScrapingDog ⚠ 403 (plan/Cloudflare;
  backup, fails soft) · ZenRows ❌ 402 exhausted.
- **Tests: 338 combined passing** (backend 290 + live_execution 48; +1
  `test_scrapingdog_forwards_privy_bearer`).

## Memory-bank update - 2026-08-27 (dead-provider fail-fast session)

- **Reference fomo-path audit** (verbatim from its source): primary = Privy
  bearer → direct `fetch` (9s, 2 attempts); fallback = Firecrawl stealth-proxy
  behind their own gateway (`proxy:"stealth"`, `rawHtml`, 25s) — the identical
  payload we already send, same credits. No free scrape mechanism exists.
- **crowd.py**: `_CONSECUTIVE_ERRORS` + `_transport_error()`/
  `_transport_success()` — 2 consecutive transport failures (timeout/connect)
  bench a provider exactly like a 402; any completed response resets the
  streak. `_scrape_firecrawl` wrapped in try/except (was uncaught → would
  crash the chain). `_FIRECRAWL_TIMEOUT(45s)` → `_STEALTH_TIMEOUT(25s)` on
  both stealth paths. `_direct_get` now 2 transport attempts (never retries a
  real HTTP response, even 403).
- **Root cause of ~15-min ticks**: ScrapingBee ReadTimeouts were caught +
  logged but never benched → every candidate re-tried it (~20 × 45s).
- **Tests: 337 combined passing** (backend 289 incl. 6 new in test_crowd.py +
  live_execution 48). Live-verified after restart: ScrapingBee benched after
  exactly 2 timeouts, crowd stage degrades in seconds, 0 tracebacks.
- **Operator action pending**: refill Firecrawl credits to restore REAL crowd
  heat (chain self-heals, no code change). ZenRows renewal optional.

## Memory-bank update - 2026-08-27 (REF-R8 + REF-R9 session)

- **config.py**: SIZING_MODE gains "risk_budget" (default stays "fixed");
  hardcoded PER_ORDER_FRACTION=0.035, DAY_MULTIPLE=4,
  HARD_ORDER_CEILING_USD=3000, HARD_DAILY_CEILING_USD=12000 (never
  env-overridable).
- **paper_trading_engine.py**: RiskBudget + compute_risk_budget() (verbatim
  computeBudget() port, Math.round half-up parity, fail-closed min ticket);
  portfolio_equity_and_unrealized() (at-cost marks when unpriced);
  compute_ticket() risk_budget branch = budget x conviction clamped [25,3000].
- **calibration.py** (NEW): compute_calibration() verbatim computeCalibration()
  port - expectancy, raw scale (+10%->+20% / -10%->-40%), confidence
  min(n/12,1), factor clamp [0.6,1.2], FLAT 1.0 fail-closed.
- **api/db.py + api/db_pg.py**: get_daily_stats() + patch_daily_stats()
  key-merge into daily_stats.stats_json (JSONB || on PG); no migration.
- **main.py**: per-tick budget+calibration compute -> log -> persist; per-
  candidate equity recompute, conviction-scaled ticket, derived daily-ceiling
  refusal (risk_budget mode only; static cap unchanged otherwise).
- **learning_loop.py**: calibration computed + persisted (merge, advisory log).
- **api/routes/disclosure.py**: risk_budget + calibration blocks
  (persisted-first, cost-basis recompute fallback, fail-closed minimums).
- **run_live_cycle.py** (DISARMED): freshest marks captured in _manage;
  risk_budget-mode sizing + derived daily ceiling vs deployed_today_usd().
- **Tests: 331 passing** (backend 283 incl. 42 new hand-computed +
  live_execution 48). Live smoke: disclosure serves both blocks; tick
  persisted real budget (equity $991, $35/$140) + FLAT calibration.

## Memory-bank update - 2026-08-25 (session 3)

- **Rules**: gate now uses EXACTLY the reference bot 9 rules
  (market_regime_ok and security_clear retired from active set;
  regime still computed/logged as observability).
- **web_research.py** (NEW): Firecrawl search evidence for thinker,
  WEB_SEARCH_PER_TICK cap, fail-soft, keyless-off.
- **social.py** (NEW): rigid provider-agnostic social read
  (Groq/Grok/OpenRouter via SOCIAL_LLM_* env).
- **live_execution**: solana.py multi-RPC + confirm; commit_log.py
  CommitLog seal/bind; executor.py place_order buy+sell with the reference statuses;
  wallet verify_expected_address; ledger open_token_amounts/
  deployed_today_usd/reduce_position.
- **run_live_cycle.py** (NEW root runner): autonomous manage->read->think->
  gate->execute cycle; --drill flag for devnet self-transfer drill.
- **Refusals public**: get_refusal_events in db.py/db_pg.py;
  /api/refusals.json; refusals embedded in /api/proof.json.
- **Discovery**: slot-composed board (newborn/movers/rotation slots),
  boost feeds set Candidate.boosted.
- **Candidate breadth fields**: chg5m/6h/24h, fdv, buys/sells/vol 6h,
  pool_count, total_liquidity_usd, top_pool_share, boosted, web/social lines.
- **Tests: 212 passing** (backend 158 + live_execution 54).

- **Sell sealing (paper)**: scan_and_execute_exits writes decision_commits
  row (verdict=sell, payload=trade_id/rule/fraction/price) BEFORE close or
  trim executes. /api/refusals.json + refusals in /api/proof.json.
- **Live commit log**: live_execution/commit_log.py CommitLog - seal intent
  sha256(nonce|payload) before broadcast, bind signature on confirm;
  wired into place_buy and place_sell via _broadcast_and_confirm.
- **Devnet drill**: run_live_cycle.py --drill runs wallet->balance->
  decimals->blockhash->sign->send->confirm dust self-transfer, devnet only.
