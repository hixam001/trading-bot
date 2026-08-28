# 07 — Project Report

**trading-bot** — a local, AI-assisted paper-trading research system for
Solana memecoins. Report updated 2026-08-28 from the current main branch
(§36: live execution unblocked — first real orders reach the chain guards;
Journal + Holdings pages restored). Status: **live** (real market data,
simulated funds; optional Supabase Postgres persistence).
**Reference parity: ALL R1–R7 features implemented; R8+R9 (drawdown-adaptive
risk budget × closed-loop conviction) implemented 2026-08-27; R11 (on-chain
precommit memo, commit–reveal) + micro-bootstrap implemented 2026-08-27
(handoff §26); dead-provider fail-fast (handoff §24) + fresh scraper keys &
ScrapingDog bearer-forwarding (handoff §25) shipped 2026-08-27; the five
omo-audit gaps A7/A6/A3/A2/A4 (wash-trade filter, symbol blocklist, venue
attribution, chain reconciliation, own-basis read-back — handoff §29)
implemented 2026-08-27; A11 thesis re-authoring (the module the original
audit missed, found in the same-day re-read — handoff §30) implemented
2026-08-27. R10 (live execution): the §27 devnet drill PASSED 5/5 on
2026-08-28 (handoff §31); the operator performed the human-only arming
steps, supervised live cycles, and on 2026-08-28 explicitly directed that
the ARMED state be committed and pushed (handoff §33) — this repo is now
committed ARMED (`LIVE_TRADING_ENABLED=True`,
`REQUIRE_MANUAL_CONFIRMATION=False`); a fresh clone that does not want real
trading must flip the flag before running anything.
The 2026-08-28 cash-corruption incident (bad quote → phantom cash in the
PAPER book) was fixed with hardcoded bad-quote guards on both books and a
final full-coverage omo audit closed every open question (handoff §32,
docs/09 §F): no trading-critical parity gap remains. Live-cycle hardening
(handoff §34) shipped the same day: 403-rejection benching for the stealth
scraper chain (a proxy refused by the origin twice is benched like a 402) and
a micro-bootstrap live cash rule (`LIVE_ACTIVE_RULES` checks the $0.50 live
floor instead of the paper book's $100), both live-verified ARMED. The
frontend was rebuilt on a real design system the same day (handoff §35 —
token-based terminal, shared primitives, Playwright E2E) and a latent
STATE_DIR bug was caught and fixed (empty env var put the live commit ledger
at the repo root).** Tests: **501 backend + 5 Playwright E2E — fully green
(the flag-state canary pins the committed ARMED state — handoff §33).**

---

## 1. Executive summary

The system watches Solana memecoin markets on a repeating tick (~60s),
assembles a candidate batch from real market data, computes a market-wide
regime snapshot, and evaluates every candidate against **ten deterministic
rules**. The AND of those rules is the *entire* entry decision. Open
positions are checked against **three fixed numeric exit conditions** every
tick. The main thinker/narrator LLM is provider-selectable via
`MAIN_LLM_PROVIDER` — DeepSeek V4 Flash (direct API, non-thinking mode) or
Groq (`qwen/qwen3.8-27b`) — with a fail-closed template fallback; Groq also
powers the evidence-only social reads. In live mode an **reference-style brain**
(ported from the reference system's reasoning layer) runs one role-routed call
per tick that grades the board and emits rich verdicts/checks/watchlist, but
its `buy` call is only a *necessary input* — the deterministic gate still
authorizes every entry. The model never sizes, opens, closes, or overrides a
verdict. Every decision — pass or fail — is persisted
with its full rule breakdown, narrated thesis, and grounding-check results,
and is visible in a real-time dashboard.

**The one governing idea:** decisions are made by deterministic, tested
code. The LLM never decides, never scores, never overrides. This was
validated against the reference system (the reference site — see
`06_REFERENCE_COMPARISON.md`), whose live payloads show an agent layer
deciding *between* rules and action; this project deliberately closes that
gap.

**Safety:** no real funds, no wallet, no transaction construction anywhere.
`PAPER_TRADING_ONLY = True` is hardcoded and re-asserted inside every
position-opening function.

---

## 2. Architecture at a glance

- **Backend** (`backend/`): Python 3.14, FastAPI, aiosqlite (WAL), httpx.
- **Frontend** (`frontend/`): React + Vite + Tailwind, built and served by
  the backend itself at http://localhost:8000 (single origin, one process).
- **Launcher**: `./start.sh` (ollama serve if needed → backend + tick loop →
  browser), `./stop.sh`, `trading-bot.desktop` app-menu entry.
- **LLM**: DeepSeek V4 Flash (direct API, non-thinking) for the main thinker/narrator/reflections/thesis restatements + the reference-style brain, Groq for evidence-only social reads (all provider-selectable + fail-closed), with comprehensive usage logging (latency, tokens, cost).
- **Data**: Birdeye memepool trending (discovery + decimals + security),
  Dexscreener pairs (all rule numerics + age + socials), Jupiter lite-api
  (execution-quality price for open positions).

## 3. THE RULES — every rule in detail

All rules are pure functions `(candidate, portfolio, regime) → RuleResult`.
`RuleResult` carries: `rule_id`, `passed`, `detail` (human-readable, always
containing the real numbers), and `value` (raw underlying value, kept for
calibration). Every rule is unit-tested on **both** branches. Thresholds
live in `backend/config.py` and are explicit placeholders until the 10-day
calibration window tunes them.

> **None semantics (applies to every rule):** a field a provider didn't
> return is `None` = UNKNOWN. For security checks, unknown **passes**
> (unknown is not the same claim as unsafe). For numeric evaluation inputs,
> unknown **fails closed** with an explicit "unavailable" detail — a skipped
> entry costs nothing; an entry on guessed data corrupts the record. No rule
> ever substitutes a fabricated default.

### 3.1 `liquidity_floor`
- **Question:** is the pool deep enough to exit a position realistically?
- **Logic:** `liquidity_usd >= MIN_LIQUIDITY_USD` (currently **$10,000**).
- **Pass example:** `liquidity $50,000 vs floor $10,000`
- **Protects against:** pools too thin to sell into — the exit door narrower
  than the position.
- **None handling:** liquidity unavailable → FAIL (cannot evaluate).

### 3.2 `volume_alive`
- **Question:** is there a live tape in the last hour?
- **Logic:** `volume_1h_usd >= MIN_VOLUME_1H_USD` (currently **$5,000**).
- **Pass example:** `1h volume $20,000 vs min $5,000`
- **Fail example:** `1h volume $900 vs min $5,000` (dead/stale token)
- **Protects against:** dead markets where any entry is an immediate
  illiquidity trap.

### 3.3 `buy_pressure`
- **Question:** are more participants buying than selling right now?
- **Logic:** `buys_1h > sells_1h` — raw 1h transaction counts from
  Dexscreener; counts, **not** dollar volume, which is trivially
  wash-traded. A tie fails (strictly greater required).
- **Pass example:** `buys 300 vs sells 200 (1h tx counts)`
- **Fail example:** `buys 655 vs sells 798`
- **None handling:** either count missing → FAIL.
- **Protects against:** entering against active distribution.

### 3.4 `not_newborn_fade` (joint condition)
- **Question:** is this a fresh launch that is *already crashing*?
- **Logic:** FAIL only when **both** hold: `age_hours < NEWBORN_AGE_HOURS`
  (**2h**) AND `price_change_1h_pct <= -NEWBORN_FADE_PCT` (**−30%**).
  Young alone passes; fading alone passes; only the combination fails.
- **Fail example:** `age 1.0h, 1h change -35.0% — newborn AND fading hard`
- **None handling:** either input missing → FAIL (joint condition not
  evaluable). *(Live: ages come from Dexscreener `pairCreatedAt`.)*
- **Protects against:** "buying the dip into zero" on fresh collapses.

### 3.5 `public_presence`
- **Question:** is there any verifiable public footprint?
- **Logic:** `has_twitter OR has_telegram OR has_website` — any one
  confirmed channel passes (from Dexscreener `info.socials/websites`).
- **Pass example:** `present channels: twitter, telegram, website`
- **Fail example:** `present channels: none confirmed`
- **None handling:** an unknown channel doesn't contribute — unknown ≠
  absent; all-unknown fails but the detail never claims absence.
- **Protects against:** fully anonymous deployments.

### 3.6 `market_regime_ok`
- **Question:** is *now* a good time to be deploying at all?
- **Logic:** the tick's shared `MarketRegime.regime_ok`: fraction of green
  candidates within **15%–85%** AND batch median 1h volume ≥ **$20,000**.
  Computed ONCE per tick from the full batch, logged once per tick in the
  `market_regime` table.
- **Fail example:** `regime BAD this tick (0% green, median 1h vol $112,781…)`
- **Protects against:** universe-wide euphoria (broad pump smell) and
  dead tape; makes "why was the bot quiet?" an explicit logged answer.

### 3.7 `cash_available`
- **Question:** can we actually fund the intended position?
- **Logic:** `portfolio.cash_usd >= INTENDED_POSITION_SIZE_USD` (currently
  **$100** fixed per entry, on a $1,000 starting book).
- **Pass example:** `cash $1,000.00 vs intended size $100.00`
- **Fail example:** `cash $72.82 vs intended size $100.00` — the bot simply
  stops opening when nearly fully deployed.
- **Protects against:** unfunded positions; backed by the engine's cash
  guard that refuses any debit driving cash negative.

### 3.8 `exposure_cap`
- **Question:** does this position stay inside the per-token cap?
- **Logic:** `portfolio.held_usd_in_mint(mint) < MAX_EXPOSURE_PER_MINT_USD`
  (currently **$150**). Gates first entries (`first entry: $0.00 held…`)
  and scale-ins (`scale-in: $100.00 held … vs cap $150.00`) uniformly.
- **Protects against:** single-token concentration. Enforced a *second*
  time atomically inside the scale-in UPDATE's WHERE clause — a racing
  add that would breach the cap affects zero rows and debits nothing.

### 3.9 `security_clear`
- **Question:** is this token KNOWN-bad?
- **Logic:** fails only on **known-bad** values:
  `mint_authority_revoked == False` (authority can mint more — rug vector)
  or `is_likely_honeypot == True`.
- **Pass (known, clean):** `no known-bad signals (mint authority revoked:
  yes, freeze authority revoked: yes, honeypot: no)`
- **Pass (all unknown):** `…mint authority revoked: unknown, …honeypot:
  unknown`
- **Fail examples:** `mint authority NOT revoked`; `flagged as likely
  honeypot`
- **None handling — the defining property:** `None` (not checked) always
  **passes**. Unknown is never coerced to False: "unchecked" is not the
  claim "checked and safe". *(Live: the Birdeye free tier 401s on
  `token_security`; the provider disables itself for the session and all
  security fields stay UNKNOWN.)*

### 3.10 `volume_mcap_ratio_ok`
- **Question:** is trading activity proportionate to valuation?
- **Logic:** `volume_24h_usd / max(market_cap_usd, 1) >= MIN_VOLUME_MCAP_RATIO`
  (currently **0.80**); evaluated only when both inputs are positive.
- **Fail example:** `24h vol / mcap = 0.03 vs min 0.80`
- **None handling:** inputs unavailable → PASS neutral (`not evaluable …
  no signal either way`) — absence of data is not a manipulation signal.
- **Protects against:** bundled/wash-traded supply — big valuation, no
  real volume.

### 3.11 Gate semantics (all ten rules)
- **No short-circuiting:** every rule runs unconditionally on every
  candidate. A rejection shows its complete 10-rule profile — "why didn't
  it buy X" is always answerable from the journal.
- **Decision:** `all_passed = AND(all rules)` — the entire entry decision.
  Pass → `decide_and_act()` routes to `open_position` (no position) or
  `scale_into_position` (existing). Fail → full narrated feed event, no
  trade.

## 4. Exit conditions (§5.2) — the sole exit decision-maker

Checked every tick against every open position, **in this order**, using
`compute_unrealized_pnl()` (net of simulated 2% slippage + 1% fee, so P&L
reflects what an exit would actually receive):

| order | condition | threshold | reason recorded |
|---|---|---|---|
| 1 | unrealized gain ≥ +50% | `TAKE_PROFIT_PCT = 0.50` | `take_profit` |
| 2 | unrealized loss ≥ −20% | `STOP_LOSS_PCT = 0.20` | `stop_loss` |
| 3 | held ≥ 72 hours | `MAX_HOLD_HOURS = 72` | `timeout` |

No LLM involvement. Prices come from Jupiter's execution-quality quote
(decimals-aware; refuses to guess when mint decimals are unknown) with a
Birdeye fallback.

## 5. Trading engine — atomicity (§5.1)

The single highest-stakes property: cash is debited/credited **exactly
once**. Every state-changing function (`open_position`, `close_position`,
`scale_into_position`) follows write-then-confirm:

1. Conditional state write whose WHERE clause makes a retry a no-op;
   affected rowcount is returned.
2. `rowcount == 0` → already happened → log, touch nothing,
   report `applied=False`.
3. Only after `rowcount == 1`: adjust cash (itself guarded so cash can
   never go negative).

Backstops: a partial UNIQUE index allows at most one open position per
mint; the scale-in cap is enforced inside the UPDATE's WHERE clause;
`PAPER_TRADING_ONLY` is asserted inside every state-changing function.
Proven by tests: double-open, double-close, crash-replay between state and
cash writes, cap refusal, and flag assertion.

## 6. Money math

- Entry: fixed $100 size; cost basis = $100 × 1.01 × 1.02 = **$103.02**
  (fee then slippage).
- Unrealized/realized P&L: net proceeds = qty × price × 0.9702 vs cost
  basis. All pure functions raise on invalid input (never silently zero)
  and are tested against hand-computed values.

## 7. Data providers

| Provider | Role | Notes |
|---|---|---|
| Birdeye | memepool trending discovery; decimals; token_security | free tier 401s on security → session auto-disable, fields stay UNKNOWN |
| Dexscreener | all rule numerics per mint: 1h volume/change, buys/sells, liquidity, mcap, pair age, socials | deepest pair chosen; field names confirmed against real payloads |
| Jupiter (lite-api) | execution-quality price for exits/holdings | decimals-aware; fails closed without them |
| fomo.fun board (crowd) | real crowd conviction: heat = clamp(20 + 8×theses) via Privy-authenticated reads | direct reads Cloudflare-challenged → stealth chain firecrawl → scrapingbee(keyless-only) → scrapingdog(custom_headers) → zenrows(custom_headers+premium_proxy) → scrapeops(keep_headers); all except scrapingbee forward the Privy bearer through Cloudflare (firecrawl/scrapeops verified live); a credit-exhausted provider (HTTP 402) is benched 30 min while a merely rate-limited one (HTTP 429) takes only a short ~75s backoff, and the provider's own error reason is logged; a provider that fails transport (timeout/connect) twice in a row is benched like a 402 so a dead scraper can never stall a tick (handoff §24); fresh Firecrawl/ScrapeOps keys restored real crowd heat 2026-08-27 (§25) |
| Mock | full offline parity incl. threshold edge cases | default backend |

All external calls: 15s timeout, ≤3 retries with backoff, distinct longer
backoff + counter on 429, non-retryable 401/403, per-provider daily call
counters surfaced in `/api/system-status`.

### Persistence

SQLite (aiosqlite, WAL) is the default book. Setting `USE_SUPABASE_DB=1` +
`SUPABASE_DB_URL` transparently switches every repository function to
Supabase Postgres (`api/db_pg.py`, asyncpg): identical function surface, the
§5.1 atomicity pattern preserved (conditional writes; rowcount is the sole
authority on cash), JSONB snapshots, one-open-position-per-mint enforced by
an exclusion constraint, RLS locked to service-role-only. Pooler TLS is
authenticated via SHA-256 certificate-fingerprint pinning (TOFU pin file,
gitignored; mismatch hard-aborts). Tests force SQLite regardless of .env.

## 8. LLM layer

- **Provider:** the MAIN path (thinker, narrator, post-close reflections) is
  a reversible `.env` flip via `MAIN_LLM_PROVIDER` (`groq` | `deepseek`,
  default `groq`): `DeepSeekClient` (DeepSeek V4 Flash direct API,
  non-thinking mode, JSON output, peak/off-peak + cache-aware cost model)
  or `MainGroqClient` (`qwen/qwen3.8-27b`, warm rollback path). Both are
  adapters of the shared provider-neutral `LLMClient` boundary
  (`build_main_client()` factory; unrecognized values fail closed to Groq).
  No local Ollama required.
- **Thinker** (pre-trade, every candidate): reads the candidate tape and
  writes `{thesis, invalidation, verdict}` before any rule runs. Verdict
  must be `"buy"` AND every rule must pass for entry. Fail-closed: any
  provider failure degrades to a deterministic template `pass` refusal.
- **Reference-style brain** (live mode, `LLM_BRAIN`, `backend/llm/llm_brain.py`):
  ports the reference system's *reasoning layer*. One role-routed call per tick
  (`run_role()` — honest resolution, ordered fallback, unsupported-model bench)
  grades up to 8 highest-volume candidates against the brain tick prompt (hard
  filters, 6 decision buckets, ground-truth + price-talk rules) and emits
  `{thoughts, actions, verdicts[checks/entry/invalidation], theses, watchlist,
  remember, fomo, break}` with the wallet fed in as context. Strict
  `parse_llm_tick()` validation drops invented symbols/invalid calls; any
  malformed/unreachable answer fails closed to empty verdicts (each candidate
  then falls back to the per-candidate thinker). The brain's `buying` maps to
  our `buy` as a NECESSARY input only — the deterministic gate still ANDs. It
  never opens/closes/sizes; `live_execution` is untouched.
- **Narration** (every decision): verdict pre-decided; prompt contains only
  rule results; output validated for emptiness + groundedness (rule-derived
  vocabulary check, invented-rule check, numeric echo check). Flags are
  recorded on the feed event — never dropped.
- **Social reads** (evidence only, `GroqClient`): classifies attention as
  `organic|peaked|unclear` and returns one grounded sentence per candidate.
  Never produces a verdict; only the thinker+gate decide.
- **Reflection** (after close): fire-and-forget async task, stored on the
  trade, shown in the journal. Never time-critical: when the main provider
  is DeepSeek, reflections during peak windows (01:00–04:00 / 06:00–10:00
  UTC weekdays) are skipped to the deterministic template instead of paying
  2× peak rates (logged, never silent).
- **Instrumentation:** every LLM call records provider, model, task, latency
  (ms), input/output/total/cache-hit tokens, estimated cost (USD;
  per-provider pricing snapshot id), peak-window flag,
  and degradation reason into the `llm_call_usage` table.
- **Advisory deep-dive:** later phase by design; may lower confidence,
  never flip a verdict.

## 9. Knowledge base, learning loop, promotion gate

- **KB**: static doctrine file + operator-ingested documents stored whole;
  prompts receive digests within a 5,000-char budget; truncation drops
  whole documents; dynamic win-rate-by-bucket stats from real trades.
- **Learning loop** (daily): win rate, profit factor, max drawdown, total
  P&L, per-rule rejection breakdown; threshold recommendations are logged
  advisory-only. Since REF-R9 it also computes and persists the **calibration
  conviction factor** (bounded 0.6–1.2, sample-confidence pulled to 1) that
  multiplies the REF-R8 risk budget when `SIZING_MODE="risk_budget"` — the
  feedback term that closes the loop. Still arithmetic only: never a
  threshold change, never model output.
- **Promotion gate** (read-only): 5 criteria — ≥40 closed trades, ≥10 days
  elapsed, ≥55% win rate, ≥1.5 profit factor, ≤20% max drawdown. Meeting
  them triggers nothing; live trading remains a manual human decision
  outside the system.

## 10. Safety invariants (non-negotiable)

1. The paper backend contains no real execution path, wallet, or transaction
  construction. Real execution is isolated in root `live_execution/`, which
  remains hard-disabled by `LIVE_TRADING_ENABLED = False`.
2. `PAPER_TRADING_ONLY = True` hardcoded + runtime-asserted in every
   position-opening function.
3. `promotion_gate.py` is read-only; no "promote"/"activate" can exist.
4. Fail-closed everywhere: unknown data skips/rejects, never guesses.
5. Atomicity pattern on all money-touching state changes (see §5).
6. Every rejection logged at the same detail level as acceptances.

## 11. Testing & verification

**290 backend tests passing** (<2s, fully hermetic): both branches of all
ten rules; regime incl. empty batch; money math known-correct values +
raise-on-invalid; atomicity (double-open/double-close/crash-replay/
scale-cap/flag assert); API shape+pagination on seeded DBs; end-to-end mock
tick cycle with forced exits and exact cash-conservation arithmetic;
Jupiter decimals regression tests pinning the 1000× fabrication bug;
LLM provider-swap suite (DeepSeek/Groq factory selection, peak/off-peak +
cache-hit cost branches against hand-computed values, mocked
`/chat/completions` usage parsing, fail-closed degradation never buying,
`narration_mode` labels);
llm-brain suite (role routing honest-label/fallback/unsupported-model-bench,
parse/validate dropping invented symbols + invalid calls, call mapping,
fail-closed tick paths, None-safe wallet/snapshot builders);
reuse fail-closed regression (malformed/legacy prior never raises);
REF-R8 risk-budget suite (30 tests: budget math hand-computed from the
published formula incl. Math.round half-up parity, fail-closed unreadable
inputs, at-cost marking, ticket clamps, derived daily-ceiling refusal
through a real tick, stats_json merge persistence, disclosure blocks);
REF-R9 calibration suite (12 tests: FLAT/caps/floors/confidence pull/
hand-computed mixed book, never-raises on garbage);
crowd dead-provider fail-fast suite (7 tests: 2-consecutive-timeout bench,
single-timeout transient, success resets streak, firecrawl transport error
fails soft + benches, direct-get retries transport once but never a 403,
stealth timeout == 25s reference budget, ScrapingDog custom_headers forwards
the Privy bearer).
48 live_execution tests (**338 combined** via root pytest.ini). The live
execution bridge is wired
and remains disarmed; its offline/mock flow covers execution guards, Jupiter
request shapes, confirmation, ledger recording, and commit binding.
Live-verified separately: providers
against real APIs, Supabase Postgres backend (full atomicity smoke + uvicorn
boot serving PG data), stealth-chain header forwarding returning real fomo
board data, commit-reveal hashes of the reference system recomputed
byte-for-byte, one-click launcher start/stop cycle.

## 12. Current status

- **Live calibration window: day 0–1.** Real candidates evaluated each
  tick; entries occur only when rules AND regime pass. Persistence:
  Supabase Postgres active (USE_SUPABASE_DB=1); fresh $1,000 book there;
  legacy SQLite book retained locally as fallback.
- Known limitations: Birdeye free tier lacks token_security (fields remain
  unknown); ScrapingBee stealth fallback is keyless-only; ZenRows premium
  tier costs ~10–25 credits/request; regime thresholds are placeholders
  pending calibration data; partial scaling (E8/E9), advisory LLM layer
  (D7), and the commit-reveal proof (appendix) are deliberately
  post-calibration scope.

## 13. REF-R5 memory and events

Implemented 2026-08-26. Both database backends persist an append-only event
stream with kinds `thought`, `did`, `refused`, `read`, and `trade`, plus
weighted lessons with recall-hit counts. Each tick records its read, think,
outcome, and successful paper-trade stages. Strongest topic-matched lessons
are recalled into the thinker prompt as context only; they cannot change
deterministic rules, sizing, exits, cash, or the paper-only boundary. Recent
events are available through the read-only `/api/events.json` endpoint.
Regression tests cover validation, persistence, hit increments, prompt
injection, and mock-tick stage events.

## 14. REF-R4 Self-regulating break system

Implemented 2026-08-26. The `not_on_break` rule was updated to use a persistent JSON state file that fail-closes if corrupted. The thinker can pass a `break` block (minutes and reason) in its JSON verdict, which triggers a persistent UTC expiry timestamp. While on break, the gate fails closed on the `not_on_break` rule, preventing new entries but allowing exits to continue uninterrupted.

## 15. REF-R2 FOMO crowd intel upgrade

Implemented 2026-08-26. Full thesis rows with author P&L are fetched from the fomo.fun feed and injected as evidence lines into the LLM thinker prompt. The prompt is instructed to weigh claims by whether the author is actually up on their position, providing a richer, performance-backed crowd conviction signal.

## 16. Incorporated P0 report

The former standalone `P0_REPORT.md` is incorporated here as the historical
P0 implementation and verification record.

### 14.1 Gate rules

Gate: exactly the reference bot 9 entry rules: `liquidity_floor`, `volume_alive`,
`buy_pressure`, `not_newborn_fade`, `public_presence`, `crowd_heat`,
`cash_available`, `already_held`, and `not_on_break`.

### 14.2 What was implemented

- `models.py` gained 13 optional breadth fields; Dexscreener extracts them.
- `research.py` performs cross-pool aggregates and is wired into the read stage.
- Discovery guarantees board composition slots and filters fake charts.
- Refusals are available through `/api/refusals.json` and `/api/proof.json`.
- `social.py` and `web_research.py` provide evidence-only adapters.
- Root `live_execution/` provides multi-RPC, seal-before-broadcast, wallet
  verification, guarded orders, and pro-rata trims while remaining disabled.
- The root live runner is wired through read, DeepSeek think, deterministic
  gate, execution, confirmation, ledger, and commit binding.
- `OLLAMA_NUM_PREDICT` controls local generation when the template fallback is
  used; execution remains isolated from `backend/`.

### 14.3 Risk and verification record

The reference-parity swap retired `security_clear` from the active nine-rule gate;
authority fields remain logged for post-hoc audit. This is an accepted the reference
parity risk and can only be changed by an explicit operator decision.

The verification record contains 222 tests at the time of the current report
(backend 192 plus live_execution 30), including commit-log tamper detection,
authority parsing, social and web evidence contracts, research/discovery
coverage, refusal API shape, sell sealing, ledger reductions, executor guards,
wallet identity checks, a mocked devnet self-transfer drill, and — as of
2026-08-26 — the full REF-R1/R6/R7 test suites (binding verification, disclosure
no-secrets, reasoning provenance, and retro-match safety properties).

The funded throwaway-keypair devnet drill remains mandatory before any mainnet
consideration; no mainnet execution has occurred.

## 17. Reference parity completion (2026-08-26)

All seven approved Reference parity items are now implemented:

| ID | Feature | Status | Key files |
|---|---|---|---|
| REF-R1 | Independent verifier + binding report | ✅ | `proof.py` `/api/binding.json`, `solana.py` `get_transaction()` |
| REF-R2 | FOMO crowd intel with author P&L | ✅ | `crowd.py`, `thinker.py` `crowd_line` |
| REF-R3 | Durable thesis book | ✅ | `db.py` `upsert_thesis/retire_thesis`, `proof.py` `/api/theses.json` |
| REF-R4 | Self-regulating break system | ✅ | `liveness.py`, `rules.py` `not_on_break` |
| REF-R5 | Events + memory system | ✅ | `db.py` `insert_event/recall_memories`, routes `/api/events.json` |
| REF-R6 | Public disclosure + reasoning feeds | ✅ | `disclosure.py` `/api/disclosure.json` + `/api/reasoning.json` |
| REF-R7 | Retro audit-log signature matching | ✅ | `retro_matcher.py`, `db.py` `bind_commit_signature` |
| REF-R8 | Drawdown-adaptive risk budget | ✅ | `paper_trading_engine.py` `compute_risk_budget` (2026-08-27) |
| REF-R9 | Closed-loop conviction factor | ✅ | `calibration.py`, `patch_daily_stats` (2026-08-27) |
| REF-R11 | On-chain precommit memo (commit–reveal) | ✅ | `live_execution/memo.py`, `proof.py` `/api/verify.json` memo checks (2026-08-27) |
| A11 | Thesis re-authoring (omo audit re-read) | ✅ | `thesis_restate.py`, `db.py`/`db_pg.py` `get_open_theses/update_thesis_text` (2026-08-27) |
| REF-R10 | Live execution (promotion path) | ⏸ deferred-by-design | operator's final manual task (handoff §27) |

### REF-R4 bug fix (2026-08-26)

`liveness.set_break(think.break_minutes, think.break_reason)` in `main.py`
was silently passing the wrong types to positional slots: `break_minutes`
(int) into `taking` (bool), and `break_reason` (str) into `minutes` (int).
Fixed to `set_break(True, think.break_minutes, think.break_reason)`. The
failing path (model requesting a break with minutes > 0) would have raised a
TypeError at runtime and logged at the wrong level. This was the only
programmatic bug found across the R2–R5 verification pass.

### Root pytest.ini restored

The root `pytest.ini` (with `asyncio_mode = auto`) was intentionally removed
in commit `20ddc0a`. This caused all async tests to fail when running from
the repo root. Restored with `testpaths = backend/tests live_execution/tests`
so the canonical combined run `python -m pytest -q` produces a clean 222-test
result.

### schema additions for REF-R1/R7

Three nullable columns added to `decision_commits` via idempotent ALTER TABLE:
- `signature TEXT` — Solana tx signature bound at exact-fill or retro attribution
- `phase TEXT` — `'filled'` when bound
- `matched_by TEXT` — `'exact'` (CommitLog) or `'retro'` (retro_matcher)

A partial index on `(signature) WHERE signature IS NOT NULL` keeps the binding
lookup O(1). Both SQLite (db.py) and Postgres (db_pg.py) backends apply the
same migrations idempotently on `init_db()`.

---

## §10. DB maintenance (2026-08-27)

### Feed and regime pruning

As the system runs 24/7, the `feed_events` and `market_regime` tables grow
unboundedly at ~1 row per candidate per tick. New repository functions trim
these tables while leaving all trade/financial state intact:

| Function | Table | Config | Default |
|---|---|---|---|
| `prune_feed_events(conn, keep_rows)` | `feed_events` | `FEED_PRUNE_KEEP` | 2,000 rows |
| `prune_market_regime(conn, keep_rows)` | `market_regime` | `REGIME_PRUNE_KEEP` | 500 rows |

Both functions keep the **newest N rows** (by `id DESC`) and delete the rest.
Return value is the count of rows deleted (0 if the table is already small
enough). Both backends (SQLite `DELETE ... NOT IN (... LIMIT ?)` and Postgres
`DELETE ... NOT IN (... LIMIT $1)`) are implemented with identical surfaces.

### Full book reset

`reset_book(conn, initial_cash_usd)` wipes all nine operational tables and
restores `portfolio_state.cash_usd` to the given amount:

```
feed_events  market_regime  decision_commits  events  memories
theses  daily_stats  llm_call_usage  trades
```

The Postgres version uses `TRUNCATE TABLE ... RESTART IDENTITY CASCADE` for
each table (faster and avoids row-level lock contention). The function is
**paper-only** — it touches no wallet, live_execution, or on-chain state.

### Operator endpoint

`POST /api/admin/reset` (hidden from schema, operator console only):

- `?confirm=yes` required; returns 400 otherwise
- `?mode=reset_book` (default): full wipe + cash restore
- `?mode=prune_only`: trims feed/regime only; trades/cash untouched
- Logged at WARNING level; returns JSON summary with per-table counts

### REF-R1–R7 audit results (2026-08-27)

All seven Reference parity features verified correct and well-tested:

| Feature | Route / Module | Status |
|---|---|---|
| R1 Independent verifier | `/api/binding.json`, `/api/verify.json` | ✅ 4-check binding, fail-closed |
| R2 FOMO crowd intel | `crowd.py` → `{crowd_line}` in thinker | ✅ author P&L injected |
| R3 Durable thesis book | `/api/theses.json`, db upsert/retire | ✅ lifecycle hooks wired |
| R4 Self-regulating break | `rule_engine/liveness.py` | ✅ atomic write, fail-closed |
| R5 Memory/events | `events`/`memories` tables, `/api/events.json` | ✅ hits accounting correct |
| R6 Disclosure feeds | `/api/disclosure.json`, `/api/reasoning.json` | ✅ zero secrets surfaced |
| R7 Retro signature match | `retro_matcher.py` | ✅ double-claim blocked |

### Test count

- 9 new tests in `tests/test_admin_reset.py`
- **Total: 231 passing** (was 222 before this session)

## §11. REF-R8 + REF-R9 — drawdown-adaptive sizing × closed-loop conviction (2026-08-27)

Batch 2 of the reference-parity roadmap (handoff §22 → §23). Verbatim ports of
the reference `computeBudget()` and `computeCalibration()`, re-fetched from the
reference repository at implementation time.

### REF-R8 — risk budget (`paper_trading_engine.py`)
- `compute_risk_budget(equity, unrealized)` pure + deterministic:
  `drawdown_factor = clamp(1 + min(0, unrealized)/equity × 2.5, 0.5, 1.0)`;
  `max_order = round(clamp(equity × 0.035 × df, 25, 3000))`;
  `max_daily = round(clamp(max_order × 4, 25, 12000))`; published `formula`
  string so the numbers are recomputable from public inputs.
- −20% of equity in open losses halves the ticket; flat/green = full size;
  unreadable book (equity ≤0/NaN/inf, NaN unrealized) fails CLOSED to the
  $25 minimum ticket (`derived=False`).
- `portfolio_equity_and_unrealized()`: equity = cash + open value, unrealized
  = open pnl; unpriced/degenerate marks held AT COST (never fabricated).
- `SIZING_MODE="risk_budget"` branch in `compute_ticket()`:
  `budget.max_order_usd × conviction_factor` clamped to `[25, 3000]`.
  `"fixed"` (default) and `"conviction"` outputs frozen for comparability.
- Derived DAILY ceiling enforced at entry (refusal journaled) in risk_budget
  mode; other modes keep the static `DAILY_DEPLOY_CAP_USD`.
- Constants hardcoded in `config.py` (never env-overridable):
  `PER_ORDER_FRACTION=0.035`, `DAY_MULTIPLE=4`, `HARD_ORDER_CEILING_USD=3000`,
  `HARD_DAILY_CEILING_USD=12000`.

### REF-R9 — calibration (`calibration.py`, NEW)
- `compute_calibration(closed_trades)` pure: expectancy from realized
  `pnl_pct`; `raw = 1 + min(exp/50, 0.2)` (exp ≥ 0) or `1 + max(exp/25, −0.4)`;
  `confidence = min(n/12, 1)`; `factor = clamp(1 + (raw−1)×confidence, 0.6, 1.2)`.
  No usable trades → FLAT 1.0; any exception → FLAT.
- Persisted into `daily_stats.stats_json` (new `patch_daily_stats()` key-merge
  in both `db.py` and `db_pg.py` — JSONB `||` on Postgres; sibling keys never
  clobbered), mirroring the reference `omo_meta` pattern.
- Wired: the tick computes budget + calibration once, persists both, and sizes
  each candidate with the factor; `learning_loop.py` persists it too (advisory
  log line, never a threshold change); `run_live_cycle.py` (disarmed) uses the
  same math in risk_budget mode only.

### Public surface
- `/api/disclosure.json` now carries `risk_budget` + `calibration` blocks —
  persisted-first, recomputed-from-DB at cost-basis equity as fallback,
  fail-closed minimums on any error. Live-verified: the running tick persists
  its real budget (equity $991, df 1.0, $35/$140) and the endpoint serves it.

### Verification
- 42 new tests (30 risk-budget + 12 calibration), every expectation
  hand-computed; combined suite **331 passed**; isolation grep clean; backend
  restarted with 0 errors / 0 tracebacks.
- Parity note: the reference clamps the order size LOW at `MIN_TICKET_USD`,
  so a $1000 book at −20% open sizes $25 (floored) — our port matches exactly
  (verified against the reference source, not assumed).
- REF-R10 (live promotion) remains deferred-by-design (operator's final manual
  task, handoff §27). REF-R11 (on-chain memo) was approved + implemented
  2026-08-27 — see the REF-R11 section below.

---

## 12. Dead-provider fail-fast + reference fomo-path audit (2026-08-27)

### Problem
Operator-reported ~15-minute tick stalls. Live log confirmed Firecrawl +
ZenRows were 402 credit-exhausted (already benched correctly by the existing
handler) — but the real stall was **ScrapingBee**: its stealth reads
ReadTimeout, and `_scrape_get_template` caught + logged the timeout yet never
benched the provider, so every candidate re-tried it (~20 candidates × 45s).

### Reference audit (how the reference gets fomo data)
Verbatim from its source — nothing exotic, and we already had both paths:
- **Primary** = exactly ours: Privy refresh-token → bearer, then direct
  `fetch()` to `prod-api.fomo.family` (9s timeout, 2 attempts, fail-soft).
- **Fallback** = a scraping API too, just behind their own gateway:
  `POST {OMO_CONNECTOR_GATEWAY_URL}/scrape` with
  `X-Connection-Api-Key: {FIRECRAWL_API_KEY}`, body
  `{url, formats:["rawHtml"], onlyMainContent:false, proxy:"stealth",
  headers:{...}}`, 25s timeout. That is Firecrawl stealth-proxy — the
  identical payload our `_scrape_firecrawl` sends, on the same credits.
- **No free mechanism exists** — fomo firewalls datacenter IPs, so some
  residential/stealth proxy is the only way through when direct fails. The
  difference was **timeout discipline**: one proxy hop at 25s that fails soft,
  vs our 5-hop chain at 45s each with dead hops never benched.

### Fix (`backend/data_providers/crowd.py`)
1. **Transport-error benching** — `_CONSECUTIVE_ERRORS` streak +
   `_transport_error()`/`_transport_success()`: 2 consecutive transport
   failures (timeout / connect) bench a provider exactly like a 402; any
   completed response resets the streak.
2. **`_scrape_firecrawl` wrapped in try/except** — previously raised uncaught
   on transport errors (would crash the chain); now counted + benched.
3. **Timeout parity** — `_FIRECRAWL_TIMEOUT(45s)` → `_STEALTH_TIMEOUT(25s)`
   on both stealth paths.
4. **Direct-path parity** — `_direct_get` now 2 transport attempts (reference
   does 2 tries); a real HTTP response (even 403) is never retried.

### Verification
- 6 new tests in `tests/test_crowd.py` (23 total there); combined suite
  **337 passed** (backend 289 + live_execution 48).
- **Live-verified after restart:** ScrapingBee timeout #1 (25s budget,
  counted), timeout #2 → "2 consecutive transport errors — benching" → benched
  30 min. Only 2 scrapingbee errors in the whole log; every later candidate
  skips it. Crowd stage now degrades to proxy heat in **seconds**, not ~15 min.
  0 tracebacks.

### Status / next
- **Refilling Firecrawl credits is the only way to restore REAL crowd heat** —
  no code fixes an empty account; after a top-up the chain self-heals with
  zero changes (firecrawl is hop #1). ZenRows renewal is optional backup.
- ScrapOps 401 (key rejected) and the direct-403 firewall remain as-is; both
  fail fast and are harmless now. Jupiter `lite-api` 429s in the exit loop are
  a separate pre-existing rate-limit, unrelated to this fix.


---

## 13. REF-R11 — On-chain precommit memo (commit–reveal) + micro-bootstrap (2026-08-27)

Operator-approved against the reference (`omotrades/omo`,
`precommit.server.ts` / `verify.server.ts`). Ships **DISARMED** — the memo path
is unreachable until `LIVE_TRADING_ENABLED` is hand-flipped (handoff §27).

### Mechanism
Every armed order seals `sha256(nonce | canonical_payload)` locally, publishes
that hash on-chain as a Solana memo (`commit:v1:` prefix, SPL Memo program
`MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr`), and **only then** quotes,
builds, and broadcasts the fill. The memo precedes the quote, so the
quote→fill window is unchanged. Anyone can later recompute the hash from the
revealed payload+nonce and check it matches the memo that was already on-chain
**before** the fill landed (slot ordering).

### Fail-closed guarantee
A memo that cannot be published and confirmed **blocks the fill** — the fill
send is never attempted (handoff §22 req. 4; stricter than the reference's
async publish). USDC insufficient/unreadable and SOL below the reserve floor
all refuse **before** any on-chain commitment is made, so no orphan memo fee is
ever burned.

### Code
- `live_execution/memo.py` (NEW): memo build (solders) + `publish_commit_memo`
  (blockhash → sign → send → confirm across rotating RPCs; any failure raises
  `MemoPublishError`).
- `commit_log.py`: `sealed → published → bound` + `record_memo()`/`fail()`.
- `executor.py`: memo-before-quote ordering; `OrderResult` carries seal+memo.
- `solana.py`: `get_usdc_balance()` (missing account = 0.0; unreadable = None).
- `run_live_cycle.py`: real-USDC cash; journals seal+memo into `decision_commits`.
- Verifier: `decision_commits` +`memo_signature`/`memo_slot` (SQLite + PG
  self-heal + `003_commit_memos.sql`), `bind_commit_memo()`/
  `get_commit_id_by_hash()`, `/api/verify.json` memo checks (hash-on-chain +
  slot ordering; RPC unavailable → `unknown`, never `pass`),
  `/api/disclosure.json` `commit_memo` block.
- `drill.py`: devnet drill now sends a real memo (airdrop-funded).

### Micro-bootstrap (start from $3–5 and compound)
- `MIN_SOL_RESERVE` env-tunable (default **0.01 SOL**); `MIN_LIVE_TICKET_USD=0.5`.
- `compute_ticket`/`compute_risk_budget` gained an optional `min_ticket_usd`
  floor (default = paper `MIN_TICKET_USD`, so paper output is bit-identical).
- Funding model: **0.03 SOL = fee reserve** (memo + fill = 2×5,000 lamports per
  order; token-account rent ~0.002 SOL per new mint) and **$3–5 USDC = trading
  capital** (buys are USDC→token). The pre-commit USDC check blocks entries when
  capital runs dry.

### Deviations from the reference (documented, handoff §26)
Fail-closed blocking (reference publishes async); immediate reveal; single
signer = the trading wallet; de-branded `commit:v1:` prefix.

### Verification
- 41 new tests (all offline/hermetic, hand-computed hash fixtures); combined
  suite **379 passed**; isolation grep clean. Live smoke (disarmed):
  `/api/verify.json`, `/api/binding.json`, `/api/disclosure.json` all 200,
  `armed=False`, `paper_only=True`, 0 tracebacks. solders 0.29.0 installed; a
  latent `Hash.from_string` bug in `drill.py` was found and fixed.

### Cost / performance (live path only; $0 while disarmed)
+1 minimum-fee tx (~0.000005 SOL) per executed order; no rent on the memo.


---

## 14. omo-audit gap queue A7/A6/A3/A2/A4 (2026-08-27, handoff §29)

A full audit of the reference repo (`omotrades/omo`) produced a comparison
(`docs/09_OMO_AUDIT_COMPARISON.md`). The operator selected five gaps to close.
All five are implemented, tested, and ship **DISARMED** where they touch the
live path. Reference sources were fetched raw (`market.server.ts`,
`blocklist.ts`, `wallet.server.ts`, `fomo.server.ts`, `execute.server.ts`).

### A7 — Wash-trade / fake-chart filter (`backend/rule_engine/fake_chart.py`)
All 13 of the reference's `isFakeChart` thresholds, ported verbatim (fee-receipt
vs FDV, volume-vs-depth turnover, thin-crowd ticket size, one-sided-by-
construction, straight-bleed corpses, dead tape, headline-day-empty-present,
paper-float-on-sliver-depth). Runs in the READ stage — before think/gate — so
manufactured tapes never reach the LLM or burn credits. `Candidate.volume_5m_usd`
added to feed the dead-tape check. **Deviation (defense-first):** unknown
age/fdv fields skip a check rather than fail-closed, because our providers do
not always return them and failing would starve the candidate pool; the numeric
gate rules still apply afterwards.

### A6 — Symbol blocklist (`backend/blocklist.py`)
`BLOCKED_SYMBOLS` + `is_blocked_symbol()` added alongside the existing mint
blocklist, enforced in `filter_candidates` (manual + auto-stop-out entries
unchanged). A rugged/manufactured name cannot re-enter through a fresh mint.

### A3 — Venue attribution (`live_execution/venue.py`)
Fills labeled by executing program (pump.fun AMM / raydium / meteora / orca /
jupiter / unknown) from the transaction's account keys — solscan-checkable.
Stored on `decision_commits.venue` (SQLite + PG self-heal +
`004_fill_venue.sql`) and surfaced in `/api/binding.json`. **Observability-only:**
never branches a decision.

### A2 — Chain reconciliation (`live_execution/reconcile.py` + `solana.get_token_balances`)
The chain is read as the authority on **quantities**; the §5.1 atomic journal
remains the authority on **cost**. The ledger is never mutated by a chain read.
Exit sizing is clamped to chain truth, vanished positions are excluded + flagged,
and unjournaled holdings are never added. **Deviation (defense-first):** the
reference re-derives the whole book from chain + tx history each sync; we keep
the journal as money authority and use the chain as a loud cross-check + sizing
clamp, so a lying RPC cannot corrupt the money ledger.

### A4 — Own-basis read-back (`crowd.read_own_basis` + `FOMO_OWN_HANDLE`)
Reads the bot's own true cost basis back from FOMO's accounting, cross-checked
against journal cost each live cycle. Reuses the existing Privy session chain —
no new secret. **Observability-only.**

### Verification
+65 tests since REF-R11 → **444 combined passing** (all offline/hermetic).
Isolation grep clean (backend's only `live_execution` refs remain function-local
optional imports). Live smoke (disarmed): all proof endpoints 200, `armed=False`,
`venue:null` on unbound pairs, 0 tracebacks. Ships DISARMED — §27 (enable live
execution) untouched and still the final task.

+~3–8s ordering latency per order (memo must confirm before the fill) —
irrelevant at one-decision-per-cycle cadence.

---

## 15. A11 — thesis re-authoring (2026-08-27, handoff §30)

A same-day re-read of the reference repo (`omotrades/omo`, full local clone,
commit 48a86f9 — unchanged since the audit) surfaced one module the original
audit's read list missed: `src/lib/thesis-author.server.ts` (`restateTheses`).
A write-up typed once at entry and never touched again is a static string with
extra steps; the reference walks the open book on the live cadence and has the
reasoning model rewrite any write-up that is stale or not model-authored,
against the position's CURRENT numbers. Implemented same day under the
standing "implement against omotrades/omo" instruction.

### Semantics (reference parity)
- **Due** = open row (`closed_at IS NULL`) whose `updated_at` is older than
  `THESIS_RESTATE_STALE_HOURS` (6.0 — reference `STALE_MS`), OR whose author
  is not `model*`, OR whose `updated_at` does not parse (fail toward
  refreshing — reference `isStale()` treats unparseable as stale).
- At most `THESIS_RESTATE_PER_PASS` (2) rows per pass, oldest text first, so
  a tick never turns into a batch job. Both constants hardcoded in
  `backend/config.py` (cadence knobs of a narrative-only job; not
  env-overridable, same philosophy as the sizing constants).
- Rewrite contract: under 60 words — why the position is still on, what
  changed since entry, the single condition that takes it out; advance the
  argument, never restate. Output validated fail-closed: <20 chars or >1000
  chars is REJECTED — old text kept, refusal logged.
- **NARRATIVE ONLY**: the pass can only ever change `theses.thesis` /
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
- DB layer (lockstep, no raw SQL outside `api/db*.py`): `get_open_theses()`
  + `update_thesis_text()` in BOTH `api/db.py` and `api/db_pg.py`.
- Wiring: `main.py run_tick` — one pass after the risk-budget block,
  reusing that block's `price_map` + open positions (**zero extra network
  I/O** — documented deviation from the reference's per-row tape fetch).
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
  (write/refuse/fail-closed/peak-skip/cap/never-raises). Combined suite:
  **470 passed** (was 444), 0 failures.
- Isolation grep clean (no new backend→live_execution references).
- Live smoke: first tick after restart advanced BOTH stale open write-ups
  (aura +3.5% mark; ANSEM −8.1% with a tightened invalidation) via
  `model:deepseek:deepseek-v4-flash`, journaled two `did` events, skipped
  the retired row; all proof endpoints 200; `armed=false`; 0 tracebacks.

### Also resolved (same re-read)
Both "not verbatim-verified" caveats from the original audit: (1)
`placeOrder`'s guard block is now verbatim-readable — our
`live_execution/executor.py` guards are a strict superset (adds kill switch,
manual confirmation, idempotency ledger, micro-bootstrap floors); (2) their
calibration factor is STILL not wired into their sizing (`computeBudget`
takes no factor; `ticketUsd(cash, conviction)` uses crowd-heat conviction) —
our REF-R8×REF-R9 wiring remains strictly ahead of their public code. Their
`exit.server.ts` is still missing from the public repo (README mentions it;
raw fetch 404) — nothing to port.



## 16. §27 pre-flight + devnet drill (2026-08-28)

The §27 arming checklist's machine-checkable preconditions were verified and
the **devnet drill PASSED 5/5** — the last automated gate before the
operator's human-only flag flips. Full record: handoff §31.

### Refusal recorded
The session opened with a request to "move live execution into backend/ and
enable it". It was refused per handoff §1 (backend/ is paper-only by
non-negotiable contract), §27 (no session may arm), and defense-first skill
rule 3. The operator chose the safe path instead: pre-flight + drill now,
human-only arming afterwards.

### Pre-flight (all green)
Arm flags disarmed; kill switch clear; confirm CLI OK; state dir writable;
solders 0.29.0; devnet + configured mainnet RPC reachable. Throwaway drill
keypair generated (solders byte-array JSON); `.env` wallet path + identity
pin set. The pin caught an operator-pinned address mismatch exactly as
designed; resolved by re-pinning to the generated wallet (operator-approved).

### Two latent bugs found by the first REAL keypair load (commit d8e426f)
1. `wallet.load_keypair` passed the file **path** to solders `from_json`
   (which expects JSON **content**) — every real load fail-closed with
   "expected value at line 1 column 1". Fixed: `from_bytes` on the
   already-validated array + an exactly-64-u8 check.
2. `drill.py` used an undefined `log` (NameError on step 1), and
   `run_live_cycle.py` ran `--drill` before `logging.basicConfig`.

Both were fail-closed (safe) and invisible to the mocked suite, but both
would have blocked arming day — proof the drill earns its keep. +4
regression tests, including the previously-missing success path (real
keypair file → real solders load → pubkey round-trip) and identity-pin
match/mismatch.

### Drill results (devnet, wallet funded via faucet.solana.com)
The RPC `requestAirdrop` faucet was at its daily limit (429 across 4
amounts × 2 endpoints); the web faucet is the working route.

```
PASS wallet:         J1pRF3YZoJj7UpPXcmth4oP11f5cK1UqwFRv9RNXhm34 (pin verified)
PASS devnet-rpc:     balance 1.0 SOL
PASS chain-decimals: SOL mint decimals=9
PASS funds:          ≥ MIN_SOL_RESERVE
PASS transfer:       real signed dust transfer broadcast + confirmed
                     (slot 489023339)
PASS commit-memo:    REF-R11 publish_commit_memo end-to-end
                     (slot 489023363)
```

### Verification
474 combined passing (backend 370 + live_execution 104). Isolation grep

## 17. Cash-corruption incident, bad-quote guards, final omo audit (2026-08-28, handoff §32)

### The incident (paper book)
After a close, the dashboard showed an outrageous cash balance. Forensics: a
transient bad Jupiter quote priced a ~$0.04 token at $119.0648 (~2,960×);
the 15s exit scanner ratcheted high-water on the poisoned mark and a
take-profit trim credited `price × quantity` ≈ $94k of phantom cash.
Root-cause class: **exit math trusted a single unbounded price sample for a
money write.**

### The fix — two hardcoded, fail-closed guards
- `EXIT_PRICE_JUMP_MAX = 50.0` (scan-level): a single exit-scan price 50×
  above the established peak is a bad quote → skip the position this scan,
  do NOT ratchet high-water. Upward-only on purpose — a genuine collapse
  must still exit.
- `MAX_EXIT_PROCEEDS_MULT = 200.0` (backstop in `close_position` AND
  `trim_position`): a single exit crediting >200× cost basis is refused
  BEFORE any state write — cash can never be corrupted even if a bad price
  reaches the exit math.
- Both deliberately generous: they trip only on data errors, never real
  moves. The book's cash was repaired to the true accumulator value.
- Live parity: `run_live_cycle._manage` got the identical jump guard — a
  live sell can never fabricate money (real swap), but an early exit on a
  phantom spike is real harm.
- Tests: `test_exit_price_guards.py` (9) + `test_manage_jump_guard.py` (3,
  built on the exact incident numbers).

### Why live cash is accurate by construction
Live "cash" is never accumulated: every cycle reads the wallet's REAL
on-chain USDC balance (`getTokenAccountBalance` on the USDC ATA; missing
account = 0.0; unreadable = None → cash 0, no entries, executor refuses).
Live proceeds are real fills — what the chain actually credits. A bad quote
can at worst trigger an early exit (now guarded), never phantom money. A2
chain reconciliation cross-checks token quantities every cycle. The
dashboard cash display is the PAPER book; the live book's truth is the
wallet's on-chain USDC balance.

### Final omo audit (docs/09 §F)
Full-coverage re-read of `omotrades/omo` (unchanged at commit 48a86f9).
Closed all open questions: (1) `exit.server.ts` EXISTS but is unpublished —
their own `exit-rules.test.ts` imports it, so a fresh clone cannot run their
tests; the pinned exit contract matches our public engine; (2) their
calibration factor is still not wired into sizing (grep: `convictionFactor`
only in `learn.server.ts`); (3) wash-trade filter parity confirmed
(`market.server.ts:237`). Remaining deltas: narration anti-repetition
(queued UX item), memo burner key (documented §26 deviation), hosting
plumbing. **Verdict: no trading-critical parity gap remains.**

### Arming state
The operator performed the §27 human-only steps on this machine
(`LIVE_TRADING_ENABLED=True`, `REQUIRE_MANUAL_CONFIRMATION=False`,
hand-edited as designed). SUPERSEDED 2026-08-28 (handoff §33): the operator
explicitly directed that the armed state be committed and pushed — done. The
canary test was re-purposed (`test_safety_flags_match_the_committed_state`)
to pin the committed state, and `/api/disclosure.json`'s `armed` field was
fixed to read the real live_execution flag (it previously always reported
False). Rollback remains one line.

### Verification
486 tests, all passing (suite fully green again).
Guards regression-tested on the exact incident shape; live jump guard
hermetically tested (stubbed chain reads, no network).

## 18. Armed state committed (2026-08-28, handoff §33)

Operator-directed ("push config as armed, no questions asked"): the §27
arming edit is now committed and pushed. What shipped with it:

- `live_execution/config.py` ARMED (the operator's own hand-edit; diff
  scanned — no secrets; all keys remain in the gitignored `.env`).
- Canary re-purposed to pin the committed flag state — any silent flip in
  either direction now fails loudly; suite fully green (486).
- Disclosure truthfulness fix: `armed` in `/api/disclosure.json` now reads
  the real flag via the sanctioned optional import (fail-closed False if the
  package is absent) instead of a nonexistent backend attribute.
- Docs aligned: config header, README (section renamed "Live trading
  (operator-ARMED)" with an explicit warning for anyone cloning), handoff
  §1/§3/§27/§32 + new §33, memory-bank.
- Unchanged: no env bypass exists; kill switch, daily-loss breaker, caps,
  identity pin, SOL reserve, and memo-before-fill all remain active.

Honest trade-off (on record): a fresh clone of this repo is armed by
default. It still cannot trade without a funded wallet keypair + RPC config
in `.env` (all gitignored), but cloners must read the README warning first.
This was the operator's explicit, informed choice after the devnet drill
passed 5/5 and live cycles were supervised.

## 19. Live-cycle hardening (2026-08-28, handoff §34)

Two operator-reported issues with the now-ARMED live cycle, both fixed and
live-verified the same day:

- **403-rejection benching** (`backend/data_providers/crowd.py`): Firecrawl
  and ZenRows were out of credits (402, benched correctly), but ScrapingDog's
  proxy was being refused by the fomo.fun origin (HTTP 403 — it can't pass
  that endpoint's Cloudflare even with forwarded headers) on every candidate,
  and ScrapingBee was timing out; only ScrapeOps gets through. Added a
  `_CONSECUTIVE_REJECTIONS` streak: two consecutive origin 403s bench a
  provider for 30 min exactly like a 402. Own counter because a 403 is a
  completed response (it must not clear the transport streak); a 200 resets
  it. Live proof: scrapingdog benched after 2× 403, ScrapeOps served all 20
  candidates — the chain converges on the working provider instead of burning
  calls on dead ones.
- **Micro-bootstrap live cash rule** (`run_live_cycle.py`): the paper
  `cash_available` rule checks cash against `INTENDED_POSITION_SIZE_USD`
  ($100, sized for the $1,000 paper book), so the few-USDC live book refused
  every entry before sizing. `LIVE_ACTIVE_RULES` swaps in
  `_live_cash_available` (checks `MIN_LIVE_TICKET_USD` $0.50); every other
  rule stays verbatim and the paper rules remain calibration-frozen. Live
  proof: no `cash_available` gate failures, several `gate=PASS`; with
  `SIZING_MODE=fixed` a $5 book sizes $0.75 ≥ the floor, so a model "buy" +
  gate pass now places a micro-order. (Current refusals are the model
  returning verdict "pass" not "buy" — DeepSeek 200 OK on every think call,
  no degradation; the model veto working as designed.)

### Verification
11 new tests → **498 passing** (backend 385 + live_execution 113), suite fully
green. Both fixes observed in `logs/live_cycle.log` on the first cycle after an
ARMED restart; system-status / live/portfolio / disclosure.json all 200.

---

## 16. Frontend terminal rebuild + STATE_DIR fix (2026-08-28, handoff §35)

### Design system (`frontend/DESIGN.md`)
Synthesized from the awesome-design-skills pack (mono / sleek / impeccable)
and held to the project's defense-first and performance-discipline skills:
a calm, dense, dark trading terminal presenting live money truth with zero
client-side invention. Tokens live only in `tailwind.config.js` (surface
ladder ink/panel/raised/line; text ladder bright/body/dim/faint; semantics
pos/neg/warn/info); JetBrains Mono for all data (tabular-nums), Inter for
labels; flat 6px panels without shadows; five required states (skeleton /
empty / error / offline banner / stale); accessible by testable criteria.

### What shipped
- `src/lib/format.ts` — verbatim-value formatters (signed `+$/−$`, small-price
  precision, `—` for null; no client-side money math).
- `src/components/ui.tsx` — Panel / Stat / Badge / Skeleton / Empty /
  ErrorState shared primitives.
- Rebuilt panels: `LiveBook` (headline real-money equity + positions),
  `LiveFeed` (REST-hydrated history + WS live-append, expanded rows with
  contract copy + verbatim model answer + rule breakdown), `MarketRegimePanel`,
  `SystemStatus` (real reasoning model + `llm_usage_recent`).
- Old `term-*` token system fully retired; fonts self-hosted; old paper
  panels already removed when the system went live.

### Playwright E2E (`npm run test:e2e`)
Five tests against the running backend on :8000: zero console errors on load;
every panel reaches data or a documented empty state (never blank); feed rows
expand/collapse with `aria-expanded` and are Enter-key operable; offline
banner appears when the API is unreachable. Config + spec in
`frontend/playwright.config.ts` / `frontend/e2e/dashboard.spec.ts`.

### STATE_DIR bug found while wiring (important)
Empty `LIVE_EXECUTION_STATE_DIR=` in `.env` made `os.getenv` return `""`, so
`Path("")` = CWD and the live CommitLedger (`commits.json`, real order
nonces) was written at the repo root — one `git add -A` from being
published. `live_execution/config.py` now falls back to
`live_execution/state/` on any empty value; the stray ledger was moved into
the state dir; `/commits.json` and Playwright artifacts were gitignored.
+3 regression tests (`live_execution/tests/test_state_dir.py`).

### Verification
- `npm run build` clean (tsc strict + vite); **Playwright 5/5 passing**;
  **pytest 501 passing** (backend 385 + live_execution 116).
- Restart smoke: system-status / live/portfolio / feed / market-regime all

## 20. Live execution UNBLOCKED + Journal/Holdings restored (2026-08-28, handoff §36)

Operator report: "the bot says enter but it doesn't execute any transaction"
and "the journal page and the other page is gone". Three stacked bugs in
`live_execution/` were blocking **every** armed order; all fixed, then the two
missing pages were restored as live-only views.

**The three blockers (each fail-closed, none reachable by the mocked suite):**
- **Quote verb** — `get_jupiter_quote` POSTed to Jupiter's GET-only
  `/swap/v1/quote` → 405 ×3 → `ExecutionError`. The sell path built its own
  inline POST quote too. Both now use a new `_get_json` helper (GET twin of
  `_post_json`, same 3-attempt/429 semantics).
- **`NameError: ExecutionError`** — `executor.py` caught it in four
  except-clauses but never imported it, so the first quote failure crashed
  the whole cycle instead of returning `status="failed"`.
- **`VersionedTransaction.deserialize` doesn't exist** — solders 0.29's parse
  constructor is `from_bytes`. The first order past the quote (GTA6: quote
  200, swap 200) died at signing. The drill/memo paths were unaffected (they
  build from a `Message`, never parse Jupiter bytes).

**Hardening (defense-first):** every post-memo failure phase now journals
`logc.fail(hash, reason)` — quote refused/failed/crashed, impact floor,
build/sign/broadcast error, unconfirmed fill — and the network phase catches
all exceptions fail-closed. The CommitLog contract ("a skipped trade must be
as visible as an executed one") is now actually enforced; before, a failed
fill left the commit stuck at `published` with no explanation.

**Live proof (ARMED mainnet):** GTA6 `think=buy gate=PASS` → sealed → memo
on-chain → quote GET 200 → **blocked at the 2.5% price-impact floor (5.30%)**
→ journalled `failed | price impact 5.30% above floor 2.5%`. The pipeline now
runs end-to-end; the first fill lands when a candidate quotes under the floor
(market-dependent — the machinery is proven). Zero cycle crashes since.

**New endpoint + pages:**
- `GET /api/live/executions` (read-only, fail-soft): the CommitLog order
  lifecycle (sealed→published→bound/failed + fail_reason + memo/fill sigs) +
  the ExecutionLedger money movements.
- Frontend **Journal** page (order decisions with status badges + expandable
  proof rows: fail reason, commit hash, memo/fill solscan links; plus the
  money ledger) and **Holdings** page (live positions detail), behind a
  three-page tab bar (dashboard / holdings / journal). These answers "why
  didn't it buy?" directly in the UI.

**Verification:** +5 unit tests (GET-verb MockTransport proof, buy/sell
quote-failure→failed-not-NameError, sell full-flow GET fill + ledger reduce,
real-solders signing round-trip) → **506 passing**; +3 E2E (tab navigation,
holdings data/empty, journal proof-expand) → **8/8 Playwright**; `npm run
build` clean; live endpoints 200.

  200; no `commits.json` at the repo root.
