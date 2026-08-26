# 07 — Project Report

**trading-bot** — a local, AI-assisted paper-trading research system for
Solana memecoins. Report updated 2026-08-26 from the current main branch.
Status: **live** (real market data, simulated funds; optional
Supabase Postgres persistence).
**OMO parity: ALL R1–R7 features implemented.** Tests: **222 passing**.

---

## 1. Executive summary

The system watches Solana memecoin markets on a repeating tick (~60s),
assembles a candidate batch from real market data, computes a market-wide
regime snapshot, and evaluates every candidate against **ten deterministic
rules**. The AND of those rules is the *entire* entry decision. Open
positions are checked against **three fixed numeric exit conditions** every
tick. A DeepSeek-compatible thinker path supplies pre-trade analysis with a
fail-closed template fallback; Groq remains evidence-only for social reads.
The model does exactly two things: narrate
decisions that were already made, and (later phase) add advisory flags that
can never override a verdict. Every decision — pass or fail — is persisted
with its full rule breakdown, narrated thesis, and grounding-check results,
and is visible in a real-time dashboard.

**The one governing idea:** decisions are made by deterministic, tested
code. The LLM never decides, never scores, never overrides. This was
validated against the reference system (omotrades.com — see
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
- **LLM**: DeepSeek V4 Flash via direct API for Thinker/Narrator, Groq API for evidence-only social reads, with comprehensive usage logging (latency, tokens, cost).
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
| fomo.fun board (crowd) | real crowd conviction: heat = clamp(20 + 8×theses) via Privy-authenticated reads | direct reads Cloudflare-challenged → stealth chain firecrawl → scrapingbee(keyless-only) → zenrows(custom_headers+premium_proxy) → scrapeops(keep_headers); the last two forward the Privy bearer through Cloudflare (verified live); quota-exhausted providers benched 30 min |
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

- **Narration** (every decision): verdict pre-decided; prompt contains only
  rule results; qwen3 thinking disabled; output validated for emptiness +
  groundedness (rule-derived vocabulary check, invented-rule check, numeric
  echo check). Flags are recorded on the feed event — never dropped.
- **Reflection** (after close): fire-and-forget async task, stored on the
  trade, shown in the journal.
- **Advisory deep-dive**: later phase by design; may lower confidence,
  never flip a verdict.

## 9. Knowledge base, learning loop, promotion gate

- **KB**: static doctrine file + operator-ingested documents stored whole;
  prompts receive digests within a 5,000-char budget; truncation drops
  whole documents; dynamic win-rate-by-bucket stats from real trades.
- **Learning loop** (daily): win rate, profit factor, max drawdown, total
  P&L, per-rule rejection breakdown; threshold recommendations are logged
  advisory-only.
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

**158 backend tests passing** (<2s, fully hermetic): both branches of all
ten rules; regime incl. empty batch; money math known-correct values +
raise-on-invalid; atomicity (double-open/double-close/crash-replay/
scale-cap/flag assert); API shape+pagination on seeded DBs; end-to-end mock
tick cycle with forced exits and exact cash-conservation arithmetic;
Jupiter decimals regression tests pinning the 1000× fabrication bug.
54 live_execution tests (212 combined). The live execution bridge is wired
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

## 13. OMO-R5 memory and events

Implemented 2026-08-26. Both database backends persist an append-only event
stream with kinds `thought`, `did`, `refused`, `read`, and `trade`, plus
weighted lessons with recall-hit counts. Each tick records its read, think,
outcome, and successful paper-trade stages. Strongest topic-matched lessons
are recalled into the thinker prompt as context only; they cannot change
deterministic rules, sizing, exits, cash, or the paper-only boundary. Recent
events are available through the read-only `/api/events.json` endpoint.
Regression tests cover validation, persistence, hit increments, prompt
injection, and mock-tick stage events.

## 14. OMO-R4 Self-regulating break system

Implemented 2026-08-26. The `not_on_break` rule was updated to use a persistent JSON state file that fail-closes if corrupted. The thinker can pass a `break` block (minutes and reason) in its JSON verdict, which triggers a persistent UTC expiry timestamp. While on break, the gate fails closed on the `not_on_break` rule, preventing new entries but allowing exits to continue uninterrupted.

## 15. OMO-R2 FOMO crowd intel upgrade

Implemented 2026-08-26. Full thesis rows with author P&L are fetched from the fomo.fun feed and injected as evidence lines into the LLM thinker prompt. The prompt is instructed to weigh claims by whether the author is actually up on their position, providing a richer, performance-backed crowd conviction signal.

## 16. Incorporated P0 report

The former standalone `P0_REPORT.md` is incorporated here as the historical
P0 implementation and verification record.

### 14.1 Gate rules

Gate: exactly omotrades 9 entry rules: `liquidity_floor`, `volume_alive`,
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

The omo-parity swap retired `security_clear` from the active nine-rule gate;
authority fields remain logged for post-hoc audit. This is an accepted omo
parity risk and can only be changed by an explicit operator decision.

The verification record contains 222 tests at the time of the current report
(backend 192 plus live_execution 30), including commit-log tamper detection,
authority parsing, social and web evidence contracts, research/discovery
coverage, refusal API shape, sell sealing, ledger reductions, executor guards,
wallet identity checks, a mocked devnet self-transfer drill, and — as of
2026-08-26 — the full OMO-R1/R6/R7 test suites (binding verification, disclosure
no-secrets, reasoning provenance, and retro-match safety properties).

The funded throwaway-keypair devnet drill remains mandatory before any mainnet
consideration; no mainnet execution has occurred.

## 17. OMO parity completion (2026-08-26)

All seven approved OMO parity items are now implemented:

| ID | Feature | Status | Key files |
|---|---|---|---|
| OMO-R1 | Independent verifier + binding report | ✅ | `proof.py` `/api/binding.json`, `solana.py` `get_transaction()` |
| OMO-R2 | FOMO crowd intel with author P&L | ✅ | `crowd.py`, `thinker.py` `crowd_line` |
| OMO-R3 | Durable thesis book | ✅ | `db.py` `upsert_thesis/retire_thesis`, `proof.py` `/api/theses.json` |
| OMO-R4 | Self-regulating break system | ✅ | `liveness.py`, `rules.py` `not_on_break` |
| OMO-R5 | Events + memory system | ✅ | `db.py` `insert_event/recall_memories`, routes `/api/events.json` |
| OMO-R6 | Public disclosure + reasoning feeds | ✅ | `disclosure.py` `/api/disclosure.json` + `/api/reasoning.json` |
| OMO-R7 | Retro audit-log signature matching | ✅ | `retro_matcher.py`, `db.py` `bind_commit_signature` |

### OMO-R4 bug fix (2026-08-26)

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

### schema additions for OMO-R1/R7

Three nullable columns added to `decision_commits` via idempotent ALTER TABLE:
- `signature TEXT` — Solana tx signature bound at exact-fill or retro attribution
- `phase TEXT` — `'filled'` when bound
- `matched_by TEXT` — `'exact'` (CommitLog) or `'retro'` (retro_matcher)

A partial index on `(signature) WHERE signature IS NOT NULL` keeps the binding
lookup O(1). Both SQLite (db.py) and Postgres (db_pg.py) backends apply the
same migrations idempotently on `init_db()`.
