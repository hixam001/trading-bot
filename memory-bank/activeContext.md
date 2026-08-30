# Active Context — trading-bot

**As of 2026-08-31 (§49 ANTI-CHURN V2, ONE MEMORY, BOTH BOOKS — the live book had NO loss memory at all: a coin stopped out LIVE could be re-bought the very next tick, and the auto-block only existed in the paper engine, counting exit_stop_loss rule IDs only. `blocklist.py` is now the single source of truth: `record_close_outcome()` (PnL-based — any close at a loss, any exit rule) feeds `maybe_autoblock()` (2 consecutive loss closes → block until a human clears it) and a 24h re-entry cooldown enforced in `filter_candidates` (read stage, both books, self-expiring, zero quota). Both books wired (paper close path + live `_manage`); loss closes journal soft memories (reference layer 5) so the thinker sees the lesson; disclosure serves `anti_churn` truths. The §49 tests also surfaced and fixed REAL test-isolation bugs: conftest now isolates BLOCKLIST_STATE_FILE (tick-closing tests had been polluting the operator's real sidecar with mock mints and auto-blocking them — the polluted file was cleaned 2026-08-31, backup at /tmp/blocklist_state.json.bak-20260831) and `_load()` fails OPEN on unreadable paths. 10 new tests; suite 647 passing (495 backend + 152 live); live cycle restarted 01:15 and verified on first ticks. NOTE: Firecrawl /v1/search currently returns 402 (credits exhausted) — the fail-soft path holds (cached miss, never raises), but paid search evidence is empty until credits are refilled.)**
Repo: `/home/hixam/Downloads/Projects/trading-bot/`.

## DONE
### §49 Anti-churn v2 — PnL-based auto-block + re-entry cooldown on BOTH books (2026-08-30)
Operator audit ask: compare our anti-churn with the reference. The gap
found: the live book had NO loss memory (auto-block only in the paper
engine, and it counted exit_stop_loss rule IDs — a trailing-stop or
manual loss exit re-entered freely the next tick). Shipped:
`blocklist.py` single source of truth — `record_close_outcome()` appends
`{rule, ts, pnl, loss, book}` to the mint's newest-first closes history
(capped 10); `maybe_autoblock()` blocks at `AUTO_BLOCK_CONSECUTIVE_LOSSES`
= 2 consecutive LOSS closes (any rule — PnL-based, the operator's
semantic); 24h re-entry cooldown in `filter_candidates` for a recent loss
close (self-expiring, zero quota, both books). Both books wired: paper
`scan_and_execute_exits` + live `_manage` do record → maybe_autoblock →
(§49 soft memory) `upsert_memory` + loss_close event (weight 2.0) so the
thinker sees the lesson. Block/unblock PRESERVE history — unlifting the
verdict never erases evidence; the preserved history keeps driving the
cooldown. Disclosure gains `anti_churn` truths (thresholds, basis,
cooldown, state semantics). Hardening the audit surfaced:
(1) `backend/conftest.py` `_isolate_live_state` now isolates
BLOCKLIST_STATE_FILE — tick-closing tests had been writing mock mints
into the OPERATOR's real sidecar and auto-blocking them (the next suite
run then opened 0 positions; operator file polluted with
T/DOWN/HEALTH/BADCOIN test artifacts, cleaned 2026-08-31, backup
/tmp/blocklist_state.json.bak-20260831). (2) `_load()` fails OPEN on
unreadable paths (OSError) like corrupt state — a tick must never die
because the sidecar can't be read. 10 new tests (9 §49 contracts in
`test_churn_guards.py` + 1 disclosure truth). **647 passing** (495
backend + 152 live). Live cycle restarted 2026-08-31 01:15; first ticks
clean (scrapling 200s, §48 skip lines, 0 tracebacks, sidecar intact).
Firecrawl /v1/search currently 402 (credits out) — fail-soft path
handled it (cached miss, never raises); refill to restore live search
evidence.

### §48 Web-search spend discipline: staged into the gate + two-tier cache (2026-08-30)
Operator directive: Firecrawl's remaining burn is the web-search evidence
stage; wanted minimal/no cost with no evidence loss. Approved scope: A
(staging) + B (cache) — the free-transport chain (Brave/DDG) is designed
but DEFERRED. Shipped: search left the read stage and became STAGE 4 of
`gate_candidate_staged` (only `all_passed` candidates — the ones the
thinker actually evaluates — get a search; `web search skipped for SYM:
failed … (no quota spent)` log lines); two-TIER mint-keyed cache — hits
7200s, misses only 1800s so "nothing found" stays current, mint-keyed so
same-ticker different-mint candidates never share evidence; knobs
`WEB_SEARCH_CACHE_TTL`/`WEB_SEARCH_CACHE_MISS_TTL`. `search_web` now
never-raises (broad catch — the new tests caught a real escape path).
`enrich_web` retained as cache-aware legacy convenience, no longer called
by the pipeline. 8 new tests (`test_web_staging.py`) + the decision-
pipeline test now pins the read-stage search as must-NOT-run. **637
passing.** LIVE PROOF (23:34 ticks): skip lines firing for rule-failed
candidates, staged searches only for the passers (3 in ~2 min vs ~4–6
per tick before — >95% quota cut), misses cached 1800s. Documented
accepted downside of B: cached hits can be up to TTL old on RECURRING
names (fresh tickers are always misses, so stay current).

### §47 Scrapling local stealth transport — IMPLEMENTED & LIVE (2026-08-30)
Operator: implement Scrapling NOW on the local machine (not deferred to
Oracle after all). Phase-0 drill (`scripts/scrapling_spike.py`, 6/hop):
httpx 0/6 (always 430) · **curl-cffi impersonated 6/6 p50 0.42s** ·
**stealth browser 6/6 p50 1.00s**. Key discovery: **fomo's CF block is
TLS-fingerprint (JA3), not IP** — Chrome-TLS impersonation passes 100%,
no browser, no proxy, from this machine. Shipped: `stealth_browser.py`
(curl hop `curl_get` + warm-browser hop `browser_fetch_json` with
`solve_cloudflare=True`, idle auto-close 600s, hard-recreate after 3
failures, UA never forwarded); `crowd.py` `_direct_get` curl-first +
`_scrape_scrapling()` FIRST in the paid chain, same bench machinery
(§34 discipline); config knobs `SCRAPLING_ENABLED/TIMEOUT_MS/
IDLE_CLOSE_SECONDS/PROXY` (`=0` reverts, one .env line); requirements
pinned `scrapling[fetchers]>=0.4.15,<0.5` (all cp314 x86_64 wheels, no
compiles); Dockerfile browser layer as root pre-`USER bot` (+~1.5 GB,
arm64 fallback = official playwright chromium); 9 new tests
(`test_scrapling_transport.py`) + legacy crowd tests forced off-state —
**629 passing**, mock/hermetic runs byte-identical. README: GitHub-only
"Open-source credits" (Scrapling BSD-3, Patchright Apache-2.0, Playwright,
curl-cffi) — website stays credit-free. LIVE PROOF (21:43 restart): every
fomo read `scrapling: Fetched (200)`, ZERO paid /v1/scrape, ZERO 430s —
Firecrawl credits now burn only on the web-search stage. Lesson for the
wrapper: Scrapling `.body` is BYTES — decode, don't str-check.

### §46 Oracle Always Free deploy decision + Scrapling deferred (2026-08-30)
Operator: deploy the engine to Oracle Always Free NOW; Scrapling (local
stealth fetcher replacing the paid fomo chain + Groq social removal +
candidate reduction 20→12) DEFERRED to implement after deployment; fresh
Firecrawl credits added so the paid chain stays primary meanwhile. Oracle
sizing corrected from Oracle's official docs: Always Free = **2 OCPU +
12 GB** always-on (1,500 OCPU-h + 9,000 GB-h/month) — NOT the 4/24
previously documented; oversized A1 instances are disabled-then-deleted 30
days after trial end on free tenancies; 200 GB storage, 10 TB/month
egress. Bot footprint ~0.8 GB RAM peak (engine + one headless Chromium
later), ~4 GB disk ⇒ comfortable. Frontend: baked into the image
(single-origin, engine serves it on :8000) or Vercel Hobby with
`VITE_API_BASE_URL` + `FRONTEND_ORIGIN` set.

### §45 equity-proportional live minimum ticket (2026-08-30)
Incident: operator reported a token showing ENTER on the dashboard with no
execution. Forensics from `logs/live_cycle.log`: TREE hit
`think=buy gate=PASS` every cycle (feed row journalled ENTER), then sizing
refused — `ticket $0.4962 below live floor $0.50` (cash $3.3078 × 0.15 vs the
hardcoded `MIN_LIVE_TICKET_USD=0.50`). Two defects: (1) a FIXED floor cannot
adapt to book size — in `SIZING_MODE="fixed"` any book with cash under ~$3.33
is structurally frozen out of every entry; (2) the below-floor refusal
`continue`d WITHOUT an `outcome["entries"]` record (the daily-budget refusal
records one), violating "a skipped trade must be as visible as an executed
one".

Fix (operator decision: 10% of equity, most conservative of 3.5%/5%/10%):
`live_execution/config.py::min_live_ticket_usd(equity) =
max(MIN_LIVE_TICKET_ABS_FLOOR_USD=$0.10, equity ×
MIN_LIVE_TICKET_EQUITY_FRACTION=0.10)`. At-cost equity = cash + Σ
position_size_usd (never price-dependent, never raises; equity ≤ 0 /
unreadable fails closed to the dust floor). ONE function now drives: the
`_live_cash_available` gate rule (detail shows the computed floor + equity),
the sizing refusal, and the `compute_ticket`/`compute_risk_budget` floor
threading — the gate-vs-sizing threshold disagreement WAS the root cause.
Paper $25 floor untouched (calibration-frozen); helper lives in
`live_execution/` (isolation intact); hardcoded never-env-settable. The
legacy `MIN_LIVE_TICKET_USD=0.50` stays as a documented historical constant —
nothing in the trade path reads it. Sizing log line now 4dp
(`ticket $0.4962 below live floor $0.4586 (equity $4.59)`) so sub-cent margins
are never misread as equality.

Behavior: floor grows with the book ($100 → $10), so positions stay
meaningful relative to the book; once > ~⅓ deployed, new entries pause until
cash recovers. 620 passing (backend 468 + live 152): §45 formula suite
(scaling, dust clamp, fail-closed on None/NaN/inf/neg/non-numeric,
incident regression equity $4.5864 → floor $0.4586 < ticket $0.4962),
cash-rule rewrite (passes at $5 and at the incident book; FAILS when
over-deployed: $0.05 cash + $3.00 deployed → floor $0.305 > cash), historical
$0.50-threading cases kept (they test mode-independent floor threading).
Docs: handoff §45, report §29 + header/totals. **Operator action: restart the
live cycle (`./stop.sh && ./start.sh`) — the running process holds the old
constants in memory.**

## DONE (previous)
### §44 staged gate: rules → scrape → crowd rules → LLM (2026-08-30)
Operator directive (cost, not cosmetics): the fomo scrape must happen only
after the normal rules pass, then the crowd rules, then the LLM; if the first
set fails, no scrape.

`decision_pipeline.gate_candidate_staged(candidate, portfolio, regime, rules,
crowd_fetch=None)` is the new layer:
1. every cheap (non-crowd) rule, unconditionally — no short-circuiting among
   them, so a reject still shows real results for all of them;
2. the fomo.fun scrape ONLY if all of those passed (else
   `crowd_lookup_deferred=True` and the log records
   `crowd scrape skipped for SYM: failed liquidity_floor (no quota spent)`);
3. the crowd rule(s), against whatever the scrape returned.

`cheap_rules()` / `crowd_rules()` partition the injected list on `__name__`
(`CROWD_RULE_IDS`), so `ACTIVE_RULES` and `LIVE_ACTIVE_RULES` share one
implementation; the assembled `GateDecision.rules` is re-ordered back into the
DECLARED order, so journal/API/UI see no reordering. `gate.py` gained
`decision_from_results()` — one definition of `all_passed` /
`failed_rule_ids` / `not_evaluated_rule_ids`, shared by the plain and staged
paths; `evaluate_gate` still does no I/O.

LLM placement (deliberate deviation from the reference's think-first order):
the once-per-tick brain is UNCHANGED (whole board, same prompt, owns
watchlist/break/remember, cost already paid); the per-candidate thinker runs
only when `gate.all_passed` — and now sees REAL crowd theses because the
scrape already ran; a rule-refused candidate gets `template_think()` with
verdict forced to `pass` and `source="template:rules-refused"`, so its journal
row stays populated without paying for a call that cannot change the outcome.

Cost per tick: 1 brain call + N scrapes + N thinker calls, N = candidates that
passed all nine cheap rules (was `MAX_CANDIDATES_PER_TICK` of each). Because
`cash_available`/`already_held` are evaluated at gate time, the saving
compounds within a tick — once cash or a slot is gone, later candidates fail a
cheap rule and skip their scrape too.

Preserved: fail-closed gate, skips tracked apart from real rejections
(learning loop + `llm/reuse` stay honest), narrator never cites a skipped
rule, declared rule order, mock hermeticity (no scrape off live ⇒ unchanged
proxy behavior), fail-soft on a dead feed. 615 passing (backend 464 + live
151). Docs: handoff §44, report §3.11/§11/§28, architecture §2.2,
FOMO_INTEGRATION.

## DONE (previous)
### §43 crowd-feed quota: shortlist-only crowd lookups (2026-08-30)
Operator named the burn: `crowd_heat` ran for every candidate every tick
before any other rule could weigh in (deliberate — no short-circuiting for
audit transparency), and its only real input is the metered fomo.fun board
read, so quota went to names about to fail liquidity or volume anyway.

Shipped: crowd enrichment removed from `decision_pipeline.enrich_candidates`
(research/social/web still unconditional) and moved to new
`enrich_crowd_for_shortlist(candidates, portfolio, regime, rules)`, which runs
the SAME `evaluate_gate` on the SAME injected rule list minus the crowd rule
(`cheap_rules()`, matched on `__name__` so it works for `ACTIVE_RULES` and
`LIVE_ACTIVE_RULES` alike — no duplicated rule logic), fetches only for the
clearers, marks the rest `Candidate.crowd_lookup_deferred=True`, and logs the
saving per tick. `rules.crowd_heat` returns `evaluated=False, passed=False,
value=None` + "not evaluated — crowd feed reserved for candidates that cleared
every other rule" for deferred candidates; a real `fomo_heat` always wins.

The trade-off is explicit and scoped to ONE field: a reject's journal row shows
"not evaluated" for `crowd_heat`. Preserved — the rule stays in every
breakdown (says why it is blank), the other nine rules still always run,
`all_passed` now requires `passed AND evaluated` (fail closed), skips go to
`not_evaluated_rule_ids` not `failed_rule_ids` (learning-loop rejection
breakdown + `llm/reuse` signature stay honest), narrator never cites a skipped
rule, mock mode is a no-op (hermetic), dead feed still degrades to proxy heat.
Both books wired (`main.run_tick` with `ACTIVE_RULES`, `run_live_cycle` with
`LIVE_ACTIVE_RULES`); live log distinguishes `FAIL:` from `SKIP:`; feed rows
carry `evaluated`; dashboard shows a neutral `SKIP` badge (missing field on
pre-§43 rows = evaluated). Docs: handoff §43, report §3.11/§10/§11/§27,
`docs/01_ARCHITECTURE.md` §2.1/§2.2, `docs/FOMO_INTEGRATION.md`.
610 passing (+13); frontend build clean. Zero new dependencies.

## DONE (previous)
### §42 deployable restructure: single-module backend + Docker + env wallet secrets (2026-08-30)
Operator approved a deployment plan, then directed execution. Layout:
`live_execution/` → `backend/live_execution/`, `run_live_cycle.py` →
`backend/run_live_cycle.py` (git mv, history preserved). Isolation contract
re-stated: the paper pipeline never imports live_execution (test-pinned) and
stays `PAPER_TRADING_ONLY=True` hardcoded; all cross-package sys.path
juggling removed (jupiter_executor, solana, executor.raw_units,
repair_vanished, run_live_cycle, api/main.py); disclosure.py kill-switch
path → `BASE_DIR/live_execution/state/...`; conftest.py rewritten
(deterministic backend/ on sys.path); pytest.ini testpaths updated. Artifacts:
root Dockerfile (SPA baked in for single-origin), backend/docker-entrypoint.sh
(API always; live cycle ONLY if armed AND wallet configured — same double-gate
as start.sh), docker-compose.yml (persistent volumes for live state + book),
.dockerignore (image contains zero secrets), docs/11_DEPLOYMENT.md runbook.
Secrets: `WALLET_KEYPAIR_JSON` env channel (resolution: path file preferred →
env JSON in-memory → fail-closed), keypair log redactor (`_KeypairRedactor`),
identity pin enforced on both channels; arming stays human-edit-only — never
env. CORS `FRONTEND_ORIGIN` comma-list; `solders` pinned in requirements
(was unlisted — deploys would have crashed); frontend `VITE_API_BASE_URL` +
`src/lib/api.ts` + vite-env.d.ts + frontend/.env.example. POST-MOVE SAFETY
CATCH (§42b): `rule_engine/liveness.py` (break state) and
`api/routes/disclosure.py` (kill switch) composed their own paths off
`BASE_DIR.parent`, so the live cycle RECREATED the old state dir — break
state and kill switch in two places (a tripped kill switch nothing reads).
Root-cause fix: `config` is the single source of truth
(`LIVE_STATE_DIR` / `BREAK_STATE_FILE` / `KILL_SWITCH_FILE`, env-overridable)
and both readers resolve it per call (the `blocklist._path()` pattern). That
exposed a second bug: the SUITE was reading the operator's REAL break state,
so `not_on_break` + every tick/gate/churn/risk-budget test behind it went
red whenever the live bot took a break (6 failures) — `conftest.py` now
retargets all three live-state paths to a tmp dir (session autouse), making
the suite hermetic. Pinned by `backend/tests/test_state_path_colocation.py`.
ENTRYPOINT BUGS found by writing its tests: missing shebang (image would not
exec at all), unchecked `cd "$APP_DIR"` (would start uvicorn from `/` with
the wrong config/state), `wait "$API_PID"` only (a crashed live cycle left
an ARMED container up + healthy but not trading — now `wait -n` on both,
non-zero exit so the platform restarts), HEALTHCHECK hardcoded to 8000 while
the app honours `$PORT`; `.dockerignore` extended for runtime state.
Engine restarted and verified: old dir gone, state in
backend/live_execution/state/, endpoints 200, disclosure armed=true with a
clear kill switch. 597 passing (448 backend + 149 live: +11 wallet-secrets,
+6 state-path co-location, +12 docker-entrypoint).

## NEXT (operator-side, no code)
Deployment only — the code queue is empty. See handoff "Next steps":
provision an always-on host with a persistent volume for
`backend/live_execution/state`, build the image, pass secrets at runtime
(mounted keypair or `WALLET_KEYPAIR_JSON`), set `EXPECTED_WALLET_ADDRESS` +
`ADMIN_TOKEN`, optionally deploy the SPA to Vercel/CF Pages with
`VITE_API_BASE_URL`, then confirm `/api/disclosure.json` shows `armed: true`
with a clear kill switch and stop the local cycle so two armed instances
never share one wallet/ledger. `docker build` is unverified in this
environment (no Docker daemon) — watch the first build/boot.

## DONE (previous)
### §41 out-of-band sell repair: close_out_of_band + repair_vanished CLI (2026-08-29)
### §41 out-of-band sell repair: close_out_of_band + repair_vanished CLI (2026-08-29)
Operator manually sold GE8q5h6e…pump during the broken-RPC window (4015.38
tokens, $0.537 cost, 1.11715 USDC proceeds, operator-confirmed). Chain went
0; ledger buy stayed OPEN; reconcile flagged "operator review needed"
every cycle (by design — never mutates the money ledger) but nothing could
COMPLETE the review, so Holdings showed the phantom forever. Shipped:
(1) `ExecutionLedger.close_out_of_band` — closes all open buys of a mint,
`outofband` idempotency key + note forensics, HONEST P&L (known proceeds
realize vs summed cost; unknown = pnl None, never fabricated, skipped by
the daily-loss breaker); (2) `repair_vanished.py` operator CLI — `list`
(open positions + live chain balance), `close` with two safety gates
(open-position check + chain-VERIFIABLY-0, unreadable RPC refused
fail-closed), journals did event + retires thesis; bookkeeping only.
Repair executed live (cycle briefly stopped to avoid the executions.json
write race): GE8q5h6e gone from Holdings, 0 reconcile warnings in the new
log, realized +$0.5801, and a fresh genuine fill minutes later (TIT
$0.53 → 1306.93 tokens) proving the book healthy. +6 tests → 568 passing.
Handoff §41, decisionLog #54.

## DONE (previous)
### §40 security_clear unblinded: dead RPC fallback fixed + Birdeye fast-fail (2026-08-29)
Operator asked "how does omo get token security via Dexscreener" (Birdeye
erroring constantly). Analysis verdict: omo reads NO token security
anywhere (fake-chart filter + liq floors only), and Dexscreener's API has
no authority/honeypot fields — nothing to port; security is our advantage,
but ours was blind. Two bugs found + fixed, both proven live:
(1) `onchain_security.get_authority_flags` never worked — it POSTed without
the `jsonrpc/id` envelope; mainnet-beta answers that with 200 + EMPTY body
(rate-limit masquerade), publicnode with 400 Parse error; `resp.json()`
threw every time. Fixed: full envelope + empty-200 = rotate-to-next-RPC +
non-mint parsed accounts rejected. Live proof: MEW → both authorities
revoked=True from the real chain.
(2) Birdeye key quota-exhausted ("Compute units usage limit exceeded" on
trending AND token_security, 3 retries each → 1,371 log lines). Fixed:
`ProviderQuotaError` in base.py (phrase-sniffed 400 quota body raises
immediately, zero retries, like 401/403) + both Birdeye surfaces
session-disable on it (operator approved trending fast-fail too). Birdeye
now answers 401 (tier denial) — same one-line session-disable path.
Live result: every `security_clear` detail reads `mint authority revoked:
yes, freeze authority revoked: yes`; 0 tracebacks; 562 passing (+12 in new
`test_onchain_security.py`: envelope regression, empty-200 rotation,
non-mint rejection, authority parsing, quota sniff, both disable paths).
Handoff §40.

## DONE (previous)
### §39 roadmap items #1, #3, #6 (2026-08-29)
**#1 security_clear re-activated** in `ACTIVE_RULES` (between `already_held`
and `not_on_break`) — one edit, both books (LIVE_ACTIVE_RULES derives from
ACTIVE_RULES). KNOWN-bad-only semantics: fails on live mint authority or a
honeypot flag; None/unknown always passes (B10) — missing data can never
block a trade. Reference parity knowingly broken by this single deliberate
addition (documented in rules.py + docs/06/07/09). Live-verified:
`/api/feed` breakdowns now carry 10 rules with `security_clear` present.

**#3 crowd author-P&L attribution** (omo exit-liquidity-dump parity):
`crowd.py::_is_dumped` — a thesis whose author closed at a realized profit
(`closed AND realized_usd > 0`) is marketing, not conviction.
`fetch_fomo_theses` returns `dumped_count` + `effective_total`;
`enrich_crowd_heat` now feeds `heat_from_count(effective_total)` — so a
board whose visible authors all took profits stops counting as live heat.
Knob: `FOMO_DUMPED_THESIS_WEIGHT` (env, default 0.0; 1.0 = old behavior).
`total` stays raw (thinker/UI/tests unchanged). Unknown authorTrade keeps
full credit (fail-soft). +6 tests in `test_crowd.py`.

**#6 pipelines unified** on new `backend/decision_pipeline.py`
(read_candidates / enrich_candidates / think_candidate / apply_break /
gate_candidate / entry_decision). Paper `main.run_tick` and live
`run_live_cycle.run_cycle` are thin over the shared core; sizing, seals,
ledgers, exits stay per-book. The extraction surfaced and fixed THREE
live-side drifts: (a) live was missing the A7 fake-chart filter entirely;
(b) a thinker exception killed the whole live cycle (now the paper's
template fallback degrades that one candidate); (c) the live break handler
called `set_break(minutes, reason)` — missing the leading `taking`
positional, a latent TypeError (now `apply_break`, correct in both books).
Isolation contract intact (module imports only backend/, pinned by test).
+13 backend `test_decision_pipeline.py` + 4 live
`live_execution/tests/test_pipeline_parity.py` (paper-vs-live decisions
differ in EXACTLY the cash rule).

Verification: **550 passing** (backend 418 + live 132; +23 over the 527
baseline), Playwright 8/8, live cycle restarted and verified (clean cycle,
gate decisions flowing, `security_clear` in every breakdown, 0
tracebacks). Handoff §39.

## DONE (previous)
### §38 security audit + hardening (2026-08-28)
Audited the whole codebase against the operator's 20-rule checklist. Verified
CLEAN: zero secrets in full git history; all keys in untracked `.env`;
Supabase RLS ON (13 tables, zero permissive policies, service-role server-side
only); both DB layers fully parameterized; npm audit 0 vulns; pip audit 0 known
vulns; React auto-escaping (no dangerouslySetInnerHTML). FIXED: **F1/F2**
`.env` + drill keypair 644→600; **F3** new `api/auth.py::require_admin_token`
gates `POST /api/admin/reset` + `/api/knowledge-base/ingest` via
`X-Admin-Token` (constant-time compare, FAIL CLOSED — unset token disables the
endpoint; 32-char token generated into `.env`); **F4** `MAX_INGEST_CHARS`
200k cap in `loader.ingest_file`; **F5** security-headers middleware
(nosniff/DENY/no-referrer + no-store on /api/*); **F6** CORS `*`→GET/POST +
Content-Type. Accepted risk: HTTP on loopback only (never leaves machine; all
external calls TLS). Operator item F7: GitHub repo is public — consider
private. E2E made deterministic (poll-based settle, feed row-or-empty-state).
+11 tests → 527; Playwright 8/8; live-verified 403/200 token behavior.
Handoff §38.

### §37 stale-holdings dust fix + first real fill + items 1 & 3 (2026-08-28)
ONE ledger bug caused three symptoms: a reconcile-clamped FULL exit produced a
sell fraction just under the 0.999 close threshold, so `reduce_position`
booked a trim and left a dust row OPEN — it showed in Holdings, counted
against `MAX_OPEN_POSITIONS=3` (blocking every ENTER with "would hold 4
mints"), and disagreed with the journal. Fix: threaded `full_close` through
`models.reduce_position` → `executor.place_sell` → `place_order` →
`run_live_cycle._manage` (`decision.action == "close_full"`); full_close
closes outright + realizes PnL on full cost. One-time repair flipped the stuck
`2NffKvfZ…` row closed (backup kept). Live proof: PINK `think=buy gate=PASS`
→ memo → quote GET 200 → **FILLED $0.66 → 491.15 tokens** (first real fill).
Item 3: `CommitLog.reconcile_orphaned(600s)` marks old memo-only `published`
commits failed/no-fill (reuses `failed` status), wired at `run_cycle` start —
healed 7 orphans, commits.json now `{failed:8, bound:5}`, 0 ambiguous. Item 1:
narrator rotates style angles (LLM prompt) + template openers — style-only,
grounding intact. +10 tests → 516. Handoff §37.

### §36 live execution unblocked + Journal/Holdings pages (2026-08-28)
Three stacked bugs blocked every armed order: (1) quote POSTed to a GET
endpoint (405) — buy + sell paths now use the new `_get_json`; (2)
`ExecutionError` caught but never imported in `executor.py` → NameError
crashed the cycle on the first quote failure; (3) solders 0.29 has no
`VersionedTransaction.deserialize` → `from_bytes`. Hardening: every
post-memo failure now journals `logc.fail(hash, reason)` (commit_log
contract: a skipped trade as visible as an executed one) and the
build/sign/broadcast phase catches all exceptions fail-closed. Live proof:
GTA6 buy sealed → memo on-chain → quote 200 → blocked at 2.5% impact floor
(5.30%) → journalled with reason; no crashes since. New read-only
`/api/live/executions` (commits lifecycle + money ledger); frontend Journal
+ Holdings pages restored as live-only views with a three-page tab bar;
+3 E2E tests (8/8), +5 unit tests (506). Handoff §36.

### §35 frontend rebuild + STATE_DIR fix (2026-08-28)
Operator-directed rebuild using the awesome-design-skills pack + Playwright.
`frontend/DESIGN.md` is now the UI source of truth (token-only colors in
`tailwind.config.js`, JetBrains Mono data / Inter labels, five required
states, a11y gates). All four live panels rebuilt on shared primitives
(`lib/format.ts`, `components/ui.tsx`); feed hydrates 50 rows from
`/api/feed` then live-appends over WS (deduped) — reloads never blank;
`term-*` tokens fully retired; fonts self-hosted. Playwright `npm run
test:e2e` (5 passing: console-clean load, panels reach data/empty never
blank, aria-expanded expand/collapse, Enter operable, offline banner).
FOUND + FIXED while wiring: empty `LIVE_EXECUTION_STATE_DIR=` in `.env` →
`Path("")` = CWD → the live CommitLedger (`commits.json`, real order
nonces) wrote to the REPO ROOT; now `or`-falls back to `live_execution/state/`,
stray ledger moved, `/commits.json` + Playwright artifacts gitignored,
+3 tests. The "Qwen3.8 text-only" error was the operator's own chat tool —
verified NOT the bot (Groq 8/8 200s, DeepSeek OK, 0 errored usage rows).

## DONE
### §34 live-cycle hardening (2026-08-28)
Two operator-reported issues with the ARMED live cycle, both fixed + verified:
(1) **403-rejection benching** (`backend/data_providers/crowd.py`): a stealth
provider whose proxy keeps getting refused by the ORIGIN (HTTP 403 — can't pass
the endpoint's Cloudflare) was re-tried on every candidate every tick. Added
`_CONSECUTIVE_REJECTIONS` — two consecutive 403s bench it 30 min like a 402;
own counter because `_transport_success` resets the transport streak on any
completed response; a 200 resets it. Live proof: scrapingdog benched after 2×
403, ScrapeOps served all 20 candidates. (2) **Micro-bootstrap live cash rule**
(`run_live_cycle.py`): the paper `cash_available` rule checks cash vs
`INTENDED_POSITION_SIZE_USD` ($100, sized for the $1,000 paper book), so a $5
live book refused every entry before sizing. `LIVE_ACTIVE_RULES` swaps in
`_live_cash_available` (checks `MIN_LIVE_TICKET_USD` $0.50); every other rule
verbatim, paper rules frozen. Live proof: no `cash_available` failures, several
`gate=PASS`; $5 book sizes $0.75 ≥ floor so a model "buy" now places a
micro-order. Current refusals are the model returning verdict "pass" (DeepSeek
200 OK, no degradation) — the model veto working as designed. 11 new tests →
**498 combined passing**.

## DONE
### §33 armed state committed (operator-directed) (2026-08-28)
Operator: "push config as armed, no questions asked". Done: committed
`live_execution/config.py` with `LIVE_TRADING_ENABLED=True` +
`REQUIRE_MANUAL_CONFIRMATION=False` (the operator's own hand-edit; diff
scanned — no secrets; keys stay in gitignored `.env`). Canary re-purposed:
`test_safety_flags_match_the_committed_state` pins the committed state (any
silent flip either way fails loudly). Disclosure truthfulness fix:
`/api/disclosure.json` `armed` previously read a nonexistent backend attr
(always False — would have lied while armed); now reads the real
live_execution flag via sanctioned optional import (fail-closed False if
absent). Docs aligned (config header, README warning for cloners, handoff
§1/§3/§27/§32/§33, project report). Suite **486 passing**. Unchanged: no env
bypass; kill switch / daily-loss breaker / caps / identity pin / SOL reserve
/ memo-before-fill all active; rollback one line. Honest trade-off recorded:
a fresh clone is armed by default but cannot trade without a funded wallet +
RPC in `.env` (gitignored) — operator's explicit informed choice.

## DONE
### §32 cash-corruption incident + bad-quote guards + final omo audit (2026-08-28)
A transient bad Jupiter quote (~$0.04 token at $119.0648, ~2,960×) poisoned
the paper exit scanner's high-water ratchet; a TP trim credited ≈$94k phantom
cash. Fixed with two hardcoded fail-closed guards — `EXIT_PRICE_JUMP_MAX=50`
(scan-level: skip + no ratchet on upward jumps; collapse still exits) and
`MAX_EXIT_PROCEEDS_MULT=200` (close/trim proceeds backstop before any state
write) — plus a one-off cash repair. Live parity: identical jump guard in
`run_live_cycle._manage` (a live sell can't fabricate money — real swap +
chain-truth cash — but a phantom-spike early exit is real harm). Live cash is
accurate by construction: re-read from the wallet's on-chain USDC balance
every cycle (`getTokenAccountBalance`), never accumulated; fail-closed to 0
when unreadable. Final omo audit (docs/09 §F): `exit.server.ts` exists but is
unpublished (their own test imports it — published repo can't run its own
tests; contract matches our public engine); their calibration factor still
unwired (final grep proof); wash-trade filter parity confirmed. Remaining
deltas: narration dedupe (queued UX item), memo burner key (documented
deviation), hosting plumbing. 12 new tests → **486 total; 485 pass while
armed** — the 1 red test is `test_safety_flags_are_hardcoded_safe_defaults`,
the ships-disarmed canary, expected red while the operator's machine is
armed. Operator flipped `LIVE_TRADING_ENABLED=True` +
`REQUIRE_MANUAL_CONFIRMATION=False` by hand (§27 human-only step); that edit
stays LOCAL — deliberately not committed; rollback is one line.

## DONE
### §27 pre-flight + devnet drill (handoff §31) (2026-08-28)
Operator began the promotion path; a session request to "move live execution
into backend/ and enable it" was REFUSED per handoff §1/§27 + defense-first
skill rule 3 (stop and flag) — the safe path was chosen instead. Pre-flight
verified: arm flags disarmed, kill switch clear, confirm CLI OK, devnet +
mainnet RPCs reachable, throwaway drill keypair generated, `.env` identity
pin set (stale duplicate line removed). First REAL keypair load found two
latent fail-closed bugs (commit d8e426f): (1) wallet.load_keypair passed the
file PATH to solders from_json (expects JSON content) → now from_bytes on the
validated array + exactly-64-u8 check; (2) drill.py log undefined +
run_live_cycle ran --drill before logging.basicConfig. +4 regression tests
(real keypair round-trip, pin match/mismatch, wrong length) → **474 combined
passing**. Drill then PASSED 5/5 on devnet (faucet-funded): wallet/identity
pin, balance read, chain decimals, real signed dust transfer broadcast +
confirmed (slot 489023339), REF-R11 publish_commit_memo end-to-end (slot
489023363). RPC requestAirdrop was daily-limited (429 on all amounts);
faucet.solana.com web faucet worked. Remaining operator-only: mainnet wallet
funded (0.03 SOL + $3–5 USDC), `.env` re-pointed, hand-edited
LIVE_TRADING_ENABLED=True (+ optionally REQUIRE_MANUAL_CONFIRMATION=False),
supervised --once. No session may perform those.

## DONE
### A11 — thesis re-authoring (handoff §30) (2026-08-27)
Same-day re-read of `omotrades/omo` (full local clone, commit 48a86f9 —
unchanged since the audit) found `thesis-author.server.ts`, which the original
audit's module list missed. Ported as `backend/thesis_restate.py`: once per
tick/cycle, rewrite open write-ups that are stale (>6h) or not model-authored
against the position's CURRENT numbers — ≤2 rows/pass, oldest first, under-60-
word contract validated fail-closed (<20/>1000 chars rejected, old text kept).
NARRATIVE ONLY: can only ever change thesis text/author/updated_at — never
trades, cash, sizing, exits, verdicts; DB write guarded by `closed_at IS NULL`
(retired-mid-pass rows untouched). Reuses the tick's own price_map (zero extra
network I/O — documented deviation from the reference's per-row tape fetch).
Wired into `main.py run_tick` + `run_live_cycle.py` (outcome key
`thesis_restatements`); `get_open_theses()`/`update_thesis_text()` in both DB
layers; `/api/disclosure.json` `thesis_restatement` block; usage accounted as
task `thesis_restate`; each rewrite journaled as a `did` event. DeepSeek peak-
window skip; mock mode no-op; never raises. Also resolved both audit caveats
verbatim (placeOrder guards — our executor is a strict superset; their
calibration factor still unwired in their public code). 26 new tests → **470
combined passing**; isolation grep clean; live-verified: first tick advanced
both stale open write-ups (aura, ANSEM) via deepseek, retired row skipped,
0 tracebacks.

## DONE
### omo-audit code queue — A7/A6/A3/A2/A4 (handoff §29) (2026-08-27)
The 2026-08-27 audit of `omotrades/omo` surfaced five parity gaps; all five
implemented, tested, shipped DISARMED (444 passing, was 379):
- **A7 wash-trade filter**: `backend/rule_engine/fake_chart.py` — verbatim port
  of omo's `isFakeChart` (all 13 thresholds: fees-vs-fdv, vol-vs-depth 20×/150×,
  thin-crowd, fat-ticket, one-sided, straight-bleed, dead-tape, headline-day-
  empty-present, paper-float). `Candidate.volume_5m_usd` added; applied in the
  READ stage before think/gate (filtered rows burn no credits). Unknown
  age/fdv fields SKIP (not fail) — documented deviation.
- **A6 symbol blocklist**: `BLOCKED_SYMBOLS` (omo's exact 14 names) + `^404`
  prefix + `is_blocked_symbol()` in `blocklist.py`, enforced in
  `filter_candidates()` before think/enrichment.
- **A3 venue attribution**: `live_execution/venue.py` labels the executing
  program off the confirmed fill tx (pump.fun/jupiter/raydium/orca/meteora;
  unknown = `program XXXX…YYYY`, never guessed). Observability only.
  `decision_commits.venue` column (both db layers + `004_fill_venue.sql`),
  journaled by `run_live_cycle`, surfaced in `/api/binding.json`.
- **A2 chain reconciliation**: `solana.get_token_balances()` (both token
  programs; `{}`=empty vs `None`=unreadable) + `live_execution/reconcile.py`.
  Chain is the authority on HOW MANY tokens we hold; journal stays the
  authority on cost. chain<journal → exit sizing clamped; chain=0 → position
  excluded + flagged; unjournaled mints flagged, never added. Ledger NEVER
  mutated by a chain read. Reported in cycle outcome `chain_reconciliation`.
- **A4 own-basis read-back**: `FOMO_OWN_HANDLE` env (default "" = disabled) +
  `crowd.read_own_basis()` (finds our own `authorTrade` on the raw board,
  invested = max(0, value−unrealized), cap 10 mints). `_crosscheck_basis()`
  compares vs journal cost each live cycle (tolerance max(5%,$0.50));
  mismatches logged, never applied. Reported as `basis_crosscheck`.
Verification: 444 passing; isolation grep clean; live smoke disarmed —
verify/binding/disclosure/proof all 200, binding pairs carry `venue: null`,
`armed=False`, 0 tracebacks.

## DONE
### REF-R11 — on-chain precommit memo (commit–reveal) + micro-bootstrap (handoff §26) (2026-08-27)
Operator-approved (the "implement against omotrades/omo" instruction is the
§13 sign-off). Every armed order now seals `sha256(nonce|canonical_payload)`,
publishes that hash on-chain as a Solana memo (`commit:v1:` prefix, SPL Memo
program), and ONLY THEN quotes/builds/broadcasts the fill. Fail-closed: an
unconfirmed memo BLOCKS the fill (handoff §22 req. 4 — stricter than the
reference's async publish). New `live_execution/memo.py` (solders build +
`publish_commit_memo`); `commit_log.py` gains `sealed→published→bound` +
`record_memo()`/`fail()`; `executor.py` order = guards→wallet→SOL reserve→
USDC funding→seal→memo→confirm→quote→build→send→confirm→bind, `OrderResult`
carries seal+memo; `solana.py` gains `get_usdc_balance()`; `run_live_cycle.py`
uses REAL USDC balance as cash + journals seal+memo into `decision_commits`.
Verifier surface: `decision_commits` +`memo_signature`/`memo_slot` (SQLite +
PG self-heal + `003_commit_memos.sql`), `bind_commit_memo()`/
`get_commit_id_by_hash()` in db.py+db_pg.py, `/api/verify.json` memo checks
(hash-on-chain + slot ordering; unknown never pass), `/api/disclosure.json`
`commit_memo` block. Micro-bootstrap: `MIN_SOL_RESERVE` env-tunable (default
0.01 SOL), `MIN_LIVE_TICKET_USD=0.5`, `compute_ticket`/`compute_risk_budget`
optional `min_ticket_usd` floor (paper bit-identical). Devnet drill now sends
a real memo. **41 new tests (379 combined passing)**; isolation grep clean;
live smoke disarmed: verify/binding/disclosure all 200, `armed=False`.
Deviations documented: fail-closed blocking, immediate reveal, single signer
(trading wallet), de-branded prefix. solders 0.29.0 installed; fixed a latent
`Hash.from_string` bug in drill.py. REF-R10 stays deferred — arming is §27.

## DONE
### Dead-provider fail-fast + reference fomo-path audit (handoff §24) (2026-08-27)
Operator-reported ~15-min tick stalls root-caused: Firecrawl + ZenRows were
402 credit-exhausted (already benched correctly) but **ScrapingBee
ReadTimeouts were never benched** — every candidate re-tried it, ~20 × 45s.
Reference audit (verbatim from its source): primary path = exactly ours
(Privy bearer → direct `fetch`, 9s, 2 attempts); fallback = Firecrawl
stealth-proxy behind their own gateway (`proxy:"stealth"`, `rawHtml`, 25s) —
the identical payload we already send, same credits. No free mechanism
exists; the difference was timeout discipline. Fix in `crowd.py`:
`_CONSECUTIVE_ERRORS` + `_transport_error()`/`_transport_success()` — 2
consecutive transport failures bench a provider like a 402, any response
resets the streak; `_scrape_firecrawl` wrapped in try/except (was uncaught);
`_FIRECRAWL_TIMEOUT(45s)` → `_STEALTH_TIMEOUT(25s)`; `_direct_get` now 2
transport attempts (never retries a real HTTP response). 6 new tests (23 in
test_crowd.py); **337 combined passing**; live-verified: ScrapingBee benched
after exactly 2 timeouts, crowd stage now degrades in seconds. Refilling
Firecrawl credits is the only way back to REAL crowd heat (chain self-heals).

### REF-R8 + REF-R9 — risk budget × conviction factor (handoff §22 → §23) (2026-08-27)
Verbatim ports of the reference `computeBudget()` / `computeCalibration()`
(re-fetched from the reference repo at implementation time). NEW
`backend/calibration.py` (pure, fail-closed FLAT=1.0, factor clamped
[0.6, 1.2], confidence `min(n/12,1)`); `paper_trading_engine.py` gains
`RiskBudget` + `compute_risk_budget()` (df = clamp(1 + min(0,unrl)/eq×2.5,
0.5, 1); order = round(clamp(eq×0.035×df, 25, 3000)); daily = ×4 clamped
12000; Math.round half-up parity) + `portfolio_equity_and_unrealized()`
(unpriced marks at cost, never fabricated) + `compute_ticket()`
`risk_budget` branch (budget × conviction, clamped; fixed/conviction
frozen). Config: `SIZING_MODE` gains `"risk_budget"` (default stays
`"fixed"` — opt-in) + hardcoded `PER_ORDER_FRACTION=0.035`, `DAY_MULTIPLE=4`,
`HARD_ORDER_CEILING_USD=3000`, `HARD_DAILY_CEILING_USD=12000`. Persistence:
`patch_daily_stats()`/`get_daily_stats()` in db.py + db_pg.py (JSONB `||`
merge; no schema migration; mirrors reference `omo_meta`). Tick computes +
persists budget + calibration once, sizes per candidate, enforces the
DERIVED daily ceiling in risk_budget mode (journaled refusal);
`learning_loop.py` persists calibration (advisory); `run_live_cycle.py`
(disarmed) wired for risk_budget mode only; `/api/disclosure.json` surfaces
both blocks (persisted-first, cost-basis recompute fallback, fail-closed).
Parity detail confirmed against source: clamp low bound IS `MIN_TICKET_USD`
($1000 book at −20% sizes $25, as the reference does). 42 new tests
hand-computed; **331 combined passing**; isolation grep clean; live smoke:
tick persisted real budget (equity $991, $35/$140) + FLAT calibration,
endpoint serves both blocks, 0 tracebacks. REF-R10/R11 remain gated.

## DONE
### De-brand + rename brain module to `llm_brain.py` (2026-08-27)
Operator directive: rename the brain module to `llmbrain.py` and remove any
references to the upstream project's name from the whole repo. Renamed the brain
module to `backend/llm/llm_brain.py` and de-branded every identifier (brain class
→ `LLMBrain`, verdict type → `LLMVerdict`, parser → `parse_llm_tick`, system
prompt → `LLM_SYSTEM`, config → `LLM_BRAIN*`), the test files (→ `test_llm_brain`
/ `test_ref_r*`), and the requirement ids (→ `REF-R#`). Repo-wide replaced the
upstream project's branding with neutral "the reference"/"reference" prose across
54 files (pure find/replace, 407/407 balanced). Removed the scratch
`backend/scripts/verify_reference_commit.py` (only verified the reference's
external on-chain preimages; not imported anywhere). KEPT `fomo`/`FOMO` (the
crowd-board product), `promotion`/`promotion_gate`, and token tickers
(BLOSSOM/SEAL) — all unrelated to the brand. The reference's `commit-v1` memo
prefix was never our commit scheme (we hash `sha256(nonce|canonical)` with no
prefix), so it only lived in the removed verifier + two reference docs. All
**289 tests pass**; backend restarted clean on the renamed module (0 import
errors, 0 tracebacks).

## DONE
### Reference-style brain — ported the reference repository's LLM reasoning layer (handoff §21) (2026-08-27)
New `backend/llm/llm_brain.py`: `run_role()` role-based router (the reference
`models.server.ts` — honest resolution, ordered fallback chain, unsupported-model
benches the provider for the process); `LLM_SYSTEM`+`LLM_OUTPUT_CONTRACT` (the reference
tick prompt: hard filters, 6 decision buckets, ground-truth + price-talk rules,
minified-JSON contract — persona lore dropped); wallet mimicry (`build_wallet_block`)
+ None-safe snapshot builder; `parse_llm_tick()` strict validation (invented symbols
/ invalid calls dropped, malformed → None); `LLMBrain.tick()` grades up to 8
highest-volume candidates, fail-closed to empty verdicts on any error. Config:
`LLM_BRAIN` (on), `LLM_BRAIN_MAX_TOKENS=4000`, `LLM_BRAIN_TIMEOUT_SECONDS=60`.
`main.py` runs the brain in live mode; each candidate uses the brain's verdict if
valid else falls back to the per-candidate thinker; `_think_from_llm()` maps the reference
`buying`→our `buy` (NECESSARY only — the deterministic gate still ANDs). Fixed a
pre-existing tick-crashing bug: `reused_if_stable()` required `prior["stats"]` but
the thesis writer never stored it → `KeyError` killed the tick; writer now stores
stats AND reuse fails closed on malformed prior. Live-verified: brain ping graded
8/10 candidates (`DELTA=buying`, 6 checks + invalidation, 2268 tokens, no
truncation), fail-closed proven on a truncated response, backend clean. 27 new
tests → **289 passing**. The LLM stays a veto/input only; `live_execution` untouched.

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
### DB maintenance: prune + reset + the reference audit (2026-08-27)
Added `prune_feed_events(conn, keep_rows)`, `prune_market_regime(conn, keep_rows)`,
and `reset_book(conn, initial_cash_usd)` to both `api/db.py` (SQLite DELETE NOT IN)
and `api/db_pg.py` (TRUNCATE RESTART IDENTITY CASCADE). New operator-only endpoint
`POST /api/admin/reset` in `api/routes/admin.py` (requires `?confirm=yes`,
`mode=reset_book` or `mode=prune_only`; logged at WARNING; never touches wallet or
live_execution). Config knobs `FEED_PRUNE_KEEP=2000` and `REGIME_PRUNE_KEEP=500`
added to `config.py`. 9 new tests in `tests/test_admin_reset.py`. Full suite now
**231 passing**. All REF-R1–R7 routes audited and confirmed correct.

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
### REF-R1 Independent verifier + binding report (2026-08-26)
`/api/binding.json` runs four binding checks per sealed decision commit: tx_confirmed (meta.err == null), time_ordering (commit_at < blockTime), fee_payer (account key 0 == wallet), mint_present (mint in pre/postTokenBalances). Each check that cannot run reports `unknown`, never `pass` (fail-closed). New `signature`, `phase`, `matched_by` nullable columns added to `decision_commits` via idempotent ALTER TABLE migrations in both SQLite and Postgres backends. `live_execution/solana.py` gains `get_transaction()` helper. `/api/verify.json` extended with `created_at` and `signature` fields.

## DONE
### REF-R6 Public disclosure + reasoning feeds (2026-08-26)
`/api/disclosure.json`: machine state — armed/disarmed, kill-switch state, break state, config truths (caps/floors/thresholds). Zero secrets. `/api/reasoning.json`: per-decision provenance — model source (from payload), inputs snapshot hash (sha256 of canonical payload_json), linked commit hash. Both registered in `api/main.py` via the same optional try/except pattern.

## DONE
### REF-R7 Retro audit-log signature matching (2026-08-26)
`backend/retro_matcher.py`: reference-exact algorithm — same symbol (case-insensitive, $-stripped) + side + fill_at >= decision_at + 12h window; earliest fill wins; `taken` set prevents double-claim. Runs post-cycle in both `main.py` and `run_live_cycle.py`. Exact-bind rows (signature IS NOT NULL) protected by WHERE clause in `bind_commit_signature`. Three new DB functions in both backends: `get_pending_unsigned_commits`, `get_recent_fills_for_retro`, `bind_commit_signature`.

## DONE
### REF-R4 Bug fix (2026-08-26)
`liveness.set_break(think.break_minutes, think.break_reason)` was silently passing int/str to the wrong positional slots (`taking=break_minutes`, `minutes=break_reason`). Fixed to `set_break(True, think.break_minutes, think.break_reason)`.

## DONE
### Root pytest.ini restored (2026-08-26)
Root `pytest.ini` with `asyncio_mode = auto` and `testpaths = backend/tests live_execution/tests` restored (was removed in commit 20ddc0a). Full suite: **222 tests passing**.

## DONE
### REF-R2 FOMO crowd intel upgrade (2026-08-26)
Full thesis rows with author P&L are now fetched from fomo.fun and injected into the LLM thinker prompt. The prompt instructs the model to weigh each claim by whether its author is actually up on their position.

## DONE
### REF-R4 Self-regulating break system (2026-08-26)
File-backed state (`break_state.json` inside `live_execution/state`) implements the `not_on_break` rule parity. The thinker can pass a `"break": {"taking": true, "minutes": 15, "reason": "..."}` block in its JSON verdict, which sets a persistent UTC expiry timestamp. While on break, the gate fails closed loudly on the `not_on_break` rule, blocking entries while exits continue functioning normally. Fail-safe semantics apply on state file corruption. LLM API migration for thinker (Groq) and social (Groq) is verified.

## DONE
### REF-R5 memory/events system (2026-08-26)
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
the reference bot' logic including the think→gate intersection (qwen3 verdict = a
necessary veto layer). Source-level study: the reference's fomo.server.ts,
fomo-auth.server.ts, pipeline.server.ts, audit.server.ts, market.server.ts,
execute.server.ts, blocklist.ts, PROCESS.md.

## Completed phase: LLM API migration continuation (2026-08-26)

See `docs/08_LLM_API_MIGRATION_AND_FEEDBACK_PLAN.md` and handoff section 14.
Groq direct API is configured for the thinker in strict non-thinking JSON mode; Groq is also configured for evidence-only Twitter/social reads. (Superseded 2026-08-27: main path now DeepSeek V4 Flash via MAIN_LLM_PROVIDER — see top DONE block; social reads remain Groq.)
Instrumentation of tokens, cache hits, cost, latency, model/prompt versions, and delayed outcomes have been implemented. Provider failure must return thinker `pass` for entry; a template may explain but cannot approve an entry.

Current learning is measurement-only: daily aggregates, rejection breakdowns,
and post-close reflections. Reviewed the reference evidence shows adaptive context and
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
the reference's exit set ported: stop −20%, trail 50%-activation/40pp give-back vs
persisted HWM, liquidity break <$8k, invalidation −25%&1.4×sells, stale
14d, TP ladder +100/300/900 trims 33/33/50%. Sell risk gate ($25 clip,
30-min/mint cooldown, ≤8 exits/24h; risk-off BYPASSES gate — documented
deviation). E8/E9 partial closes via trim_position (atomic).
main(): dedicated 15s price-only exit scan loop alongside tick. DB
migration: trades.high_water_usd + trades.tranches_taken.

### Old logic purged + entry gate = the reference verbatim (ebdd1a1)
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
- decision_commits table (the reference 'seal' parity): every decision sealed with
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
- Suite now: backend 308, live_execution 71, combined root 379 (root pytest.ini asyncio_mode)
- isolation grep (backend must not mention live_execution) must stay clean —
  the only backend references are function-local optional imports (REF-R1/R11 pattern)
- solders 0.29.0 is a live_execution dependency (memo/drill tx build); installed in .venv
- .env.example documents ALL env fields incl. crowd-feed keys; user has
  FOMO_PRIVY_REFRESH_TOKEN + FIRECRAWL_API_KEY filled
- ⚠ CROWD HEAT: fresh keys added 2026-08-27 (handoff §25) — Firecrawl (primary)
  + ScrapeOps (failover) now serve REAL crowd heat. ZenRows still 402-exhausted
  (optional renewal). ScrapingBee ReadTimeouts (can't forward bearer) and
  ScrapingDog 403 (plan/Cloudflare) are harmless backups. Dead providers bench
  after 2 consecutive transport errors (handoff §24), so none can stall a tick.


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

