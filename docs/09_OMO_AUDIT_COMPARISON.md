# 09 — omo Audit & Comparison

**Date:** 2026-08-27 · **Reference:** [`omotrades/omo`](https://github.com/omotrades/omo)
**Method:** full file-tree fetch + raw read of every core module (`risk`,
`execute`, `pipeline`, `audit`, `learn`, `market`, `fomo`, `wallet`, `theses`,
`models`, `ai-gateway`, `keys`, `signer.interface`, `offbook`, `blocklist`,
`web-research`, `cabin-ritual`, `background`, `precommit`/`verify`), plus
README, PROCESS.md, docs/VERIFICATION.md, their test dir, and git-history checks.

**What omo is:** TypeScript/Bun + TanStack SSR + Supabase + Vercel AI SDK (via a
Lovable AI gateway), live-trading mainnet with a published wallet,
scheduler-triggered cycles (`POST /api/public/cycle` with a secret header), one
target candidate per cycle. Flow: `manage → read → think → gate → seal(memo) →
execute → journal → reveal(20-min delayed)`.

> Status of the five gaps the operator selected (A2/A3/A4/A6/A7): **implemented
> 2026-08-27** — see handoff §29 and project report §14. This document records
> the audit findings that drove that decision.
>
> **2026-08-27 re-read (full local clone, commit 48a86f9):** surfaced one module
> the original audit missed — `thesis-author.server.ts` (thesis re-authoring),
> recorded below as **A11** and implemented same day (handoff §30). The two
> "not verbatim-verified" caveats at the bottom are now resolved.

---

## A. What they have that we hadn't implemented

| # | Feature | Theirs | Ours (at audit time) |
|---|---|---|---|
| 1 | Separate burner memo key (`OMO_COMMIT_KEY`) | Memos signed by a key holding **no funds** — a memo-key leak can't move money | Memo signed by the trading wallet (documented §26 deviation) |
| 2 | Chain-derived book re-derivation (`wallet.server.ts`) | Every sync re-reads on-chain token accounts + Jupiter prices; book can't drift from wallet | Live book = local `ExecutionLedger` journal |
| 3 | Venue attribution (`fetchFillVenue`) | Fills labeled by executing program (pump.fun AMM / raydium / meteora / orca / jupiter) from tx account keys | Not implemented |
| 4 | Direct fomo prod-api integration (`fomo-auth` mints Privy sessions) | Structured theses **with each author's live P&L**, plus reads its *own* true cost basis back from FOMO's accounting (`authorTrade`) | Scraped board HTML via 4-provider proxy chain — shallower |
| 5 | Off-book multi-chain positions (`offbook.server.ts`) | BNB/Robinhood positions rolled into disclosed equity — but **hardcoded typed-in marks** + live DexScreener repricing (crude, manual) | Deferred by design |
| 6 | Symbol blocklist (`blocklist.ts`) | Hardcoded ban list (rugged/manufactured/closed names) enforced at scanner + grading + shortlist | None explicit — numeric floors + churn guard partially cover |
| 7 | Wash-trade "fake chart" filter (`isFakeChart`) | Volume-vs-depth + round-trip-shape heuristic kills manufactured tapes *before* reasoning | Liquidity/volume floors + churn guard, no explicit wash-trade shape heuristic |
| 8 | Delayed reveal (20 min) | Plaintext opened 20 min after sealing | Immediate reveal (documented deviation) |
| 9 | Signer abstraction (`signer.interface.ts`) | Clean contract: local keypair / remote service / HSM / hardware all satisfy it | Concrete file keypair (+ identity pin) |
| 10 | Personality layer (`cabin-ritual.server.ts`, 122KB) | Daily lore/flavor notes to the public terminal | Nothing comparable (pure UX narrative — not a trading feature) |
| 11 | Thesis re-authoring (`thesis-author.server.ts`) | Live-cadence job rewrites any open write-up that is stale (>6h) or not model-authored against the position's current numbers — ≤2 rows/pass, oldest first, never touches size/PnL/retirement | **Missed by the original audit** (module absent from the read list); thesis book wrote at open and retired at close only. **Implemented 2026-08-27** (handoff §30): `backend/thesis_restate.py`, wired into the paper tick and the live cycle, narrative-only, fail-closed validation (reject <20/>1000 chars), reuses the tick's own price marks (no extra network I/O) |

**Operator decision:** implement **2, 3, 4, 6, 7**. (1, 5, 8, 9, 10 left as-is —
see "Deliberately not adopted" below.) **11** discovered in the 2026-08-27
re-read and implemented same day under the standing "implement against
omotrades/omo" instruction.

---

## B. Where we were worse at audit time

1. **Model quality:** they run **Claude Opus 5** (reasoning + narration) and
   **Grok 4.1** (realtime social) per-role with honest degraded-routing records.
   We run cheaper models (Groq-hosted; DeepSeek migration queued behind
   shadow-replay proof). Cost-efficient, weaker reasoning horsepower.
2. **Live track record:** they have real mainnet fills, a public wallet with real
   equity, venue-labeled journal, off-book disclosure. We're paper + disarmed —
   *by our own sequencing choice*, but a factual gap today.
   **[SUPERSEDED 2026-08-31: we have been live-armed with 26 real mainnet
   closes since 2026-08-28 — 24 bot-driven + 2 operator out-of-band repairs;
   the measured track record now lives in `scripts/perf_report.py` and §F
   below. Their live disclosure (§F) shows THEM paused/unarmed as of
   2026-08-31.]**
3. **Fomo data depth (A4):** per-author P&L weighting and own-basis read-back are
   richer than our scraped heat. *(Now closed.)*
4. **Public presentation:** their proof terminal (105KB SSR page, clock panel,
   venue display, ASCII aesthetic) is a more polished auditability showcase.
5. **Slippage tolerance is a tradeoff, not a win:** they allow 150bps, we allow
   50bps. Ours is stricter (better price protection) but fails more quotes on
   thin pairs — worth knowing when armed.


---

## C. Where we are better

1. **Testing — not close:** **444 offline hermetic tests** (hand-computed money
   math, §5.1 atomicity proofs, fail-closed path coverage) vs their **5 test
   files** (`audit-rules`, `exit-rules`, `models`, `precommit`, `solana`).
2. **Their exit engine isn't even in the repo.** `src/lib/exit.server.ts` has
   **zero commits ever** (verified via GitHub API), yet `pipeline.server.ts`
   type-imports it, `exits[.]json.ts` runtime-imports `EXIT_LIMITS`/
   `exitRuleLabel` from it, `exit-rules.test.ts` tests it, and PROCESS.md
   documents it. The public repo cannot serve `/api/public/exits.json` as
   committed — their deployed system clearly has it, but **the published code is
   a subset of the running machine**. Ours: `rule_engine/exits.py` fully
   implemented + tested (stop, trail give-back, liquidity break, thesis
   invalidation, stale, TP ladder) plus a sell risk gate with a documented
   risk-off bypass (stops never blocked by cooldowns).
3. **Fail-closed memo semantics:** ours blocks the fill synchronously if the memo
   can't be confirmed (handoff §22 req 4). Theirs publishes asynchronously with
   pending-retry. Ours is the stricter guarantee, chosen deliberately.
4. **Defense layering:** beyond their guards (mint-match, memo-on-chain, impact
   floor, one-order-per-commit, per-order/day caps, SOL reserve), we add: **kill
   switch (manual + automatic −$75 realized daily-loss breaker),
   manual-confirmation queue with fail-closed expiry, idempotency ledger,
   DB-enforced one-open-position-per-mint, pre-commit USDC funding check,
   expected-wallet identity pin, decimals-from-chain refusal**.
5. **Arming model:** theirs arms by env presence (`isArmed() = OMO_TRADING_KEY
   set`) — a leaked env var yields an armed loop. Ours requires hand-editing two
   hardcoded constants (no env path exists) — slower to arm, but env theft can't
   arm us. Both publish armed state honestly.
6. **LLM cost/observability accounting:** our `llm_call_usage` (REF-R2) tracks
   tokens/cost/latency/cache/model per call, plus `model_version`/
   `prompt_version` on every feed event and commit. Nothing comparable visible
   in their repo.
7. **Sizing integration:** our risk_budget mode wires drawdown budget ×
   calibration conviction directly into the ticket. Their pipeline sizes by
   crowd-heat conviction capped at static $3,000 — and their calibration's
   claimed "factor multiplies the risk budget" is **not verifiable in their
   public code** (`computeBudget` takes no factor; `ticketUsd` doesn't use it).
8. **Market-level guards:** regime filter + churn guard (market-wide conditions)
   — their 9 rules are candidate-level only.
9. **Structured model output:** our Thinker returns structured `wants_entry` +
   grounding flags; their pipeline decides entry with a **regex on the thesis
   text** (`/\bbuy\b/ && !/\bpass\b/`) — brittle.
10. **Data-provider resilience:** 4-provider scraper chain with auto-benching/
    recovery vs their single Firecrawl gateway (+direct API).
11. **Dual-backend DB** (SQLite + Supabase, identical surface, self-healing
    schema, cert pinning) vs Supabase-only.
12. **Paper/live comparability:** identical rules/exits/sizing on both paths for

---

## D. Parity (same, by port)

- Commit–reveal architecture (seal → memo → execute → journal → verify) — REF-R1/R11
- `computeBudget` formula — verbatim, identity-tested (REF-R8): same
  0.035/×4/$25/$3k/$12k constants
- Calibration formula — verbatim (REF-R9)
- **The exact same 9 gate rules with the same IDs** (`liquidity_floor …
  not_on_break`) — our `ACTIVE_RULES` comment says it outright
- Memo program ID, memo-log parsing, the 4-check verification model (hash match /
  memo-before-fill / mint match / signer match)
- Jupiter routing + local signing + rotating public RPCs (same two public
  endpoints), 2.5% impact floor
- Thesis book with authorship (REF-R3), events/memories with identical event
  kinds (REF-R5), disclosure/proof/verify/reasoning endpoints (REF-R6), retro
  fill-binding (REF-R7 ≈ their `linkAuditToFills` 12h matcher),
  crowd-heat-from-thesis-counts, Firecrawl web research (fail-soft), and the
  "open brain, locked hand" philosophy

---

## E. Deliberately not adopted (and why)

- **A1 burner memo key:** at this book scale a single trading-wallet signer is
  sufficient; a second key adds operational surface for no current threat. Split
  later if the wallet grows.
- **A5 off-book multi-chain:** theirs is hardcoded typed-in marks — manual
  constants, not real tracking. Deferred until the Solana side has a live record.
- **A8 delayed reveal:** our payload+nonce are already public in `/api/proof.json`;
  the ordering proof is the on-chain hash timestamp, so a 20-min delay adds
  nothing.
- **A9 signer abstraction:** a clean interface is nice, but we have exactly one
  signer backend (file keypair). Abstracting for a hypothetical HSM is premature.
- **A10 personality layer:** pure UX flavor, not a trading feature.

---

## Honest audit caveats

- Their repo is **partially broken as published** (missing exit module) — so
  comparisons against their *deployed* behavior rely on their docs/endpoints, not
  just code. **Re-verified 2026-08-27** against a full local clone (commit
  48a86f9): `src/lib/exit.server.ts` is still absent (README mentions it; raw
  fetch 404), so there is still nothing to port for their exit rule set.
- ~~`placeOrder`'s guard block was middle-truncated in every fetch; the guard
  list is taken from the module's own header documentation plus all visible
  code — accurate but not verbatim-quoted.~~ **Resolved 2026-08-27**: the full
  clone made the guard block verbatim-readable — mint-matches-sealed-commit,
  memo-already-on-chain, one-order-per-commit-row, 2.5% price-impact floor,
  $3,000/$12,000 per-order/per-day ceilings, 0.05 SOL reserve, $25 min ticket,
  decimals-from-chain refusal, zero-round sell refusal. Our
  `live_execution/executor.py` guards are a strict superset (adds kill switch,
  manual confirmation, idempotency ledger, micro-bootstrap floors).
- Their calibration wiring claim is flagged "unverified", not "false".
  **Re-checked 2026-08-27**: still unverified — `computeBudget` takes no
  factor and the pipeline's `ticketUsd(cash, conviction)` uses crowd-heat
  conviction only; our REF-R8×REF-R9 wiring (factor multiplies the budget)
  remains strictly ahead of their public code.
- **Added 2026-08-27:** the original audit's module list omitted
  `thesis-author.server.ts`; the re-read caught it (A11). Lesson recorded:
  audit from the file tree, not from a hand-maintained module list.

---

## F. Final re-audit (2026-08-28) — full module coverage, open questions closed

Repo state: **unchanged** (still commit 48a86f9, the same HEAD audited on
2026-08-27). This pass read every remaining module the earlier audits had not
covered in depth (`omo-brain`, `learn`, `audit`, `keys`, `signer.interface`,
`web-research`, `ai-gateway`, `models`, `offbook`, error infra, and the
`__tests__/` directory) and closed the three standing open questions.

### Open questions — closed

1. **Does `exit.server.ts` exist at all?** YES — proven. Their
   `src/lib/__tests__/exit-rules.test.ts` imports `../exit.server`
   (`EXIT_LIMITS, evaluateExitRules, evaluateSellGate, ExitInputs`). The
   module exists in their working tree but is **not published** — a fresh
   clone cannot even run their own test suite. Hard evidence for the
   long-standing caveat: *their published code is a subset of the running
   machine*. The test also pins their exit contract verbatim: hard stop
   (full), trailing stop armed only after a real run, liquidity break even
   in profit, price+flow invalidation, TP tranches (0.33 first, never
   retaken), risk-break prefers full exit over trim, stale close, and a sell
   gate (`gate_min_clip` / `gate_cooldown` / `gate_daily_exits`). **Our
   `rule_engine/exits.py` implements the same model** (rebuilt on it
   2026-08-20, decisionLog #20) — and ours is fully published and tested,
   plus the fast 15s scanner and the §32 bad-quote guards, which they do not
   publish.
2. **Is their calibration factor wired into sizing?** NO — final proof.
   `convictionFactor` appears nowhere outside `learn.server.ts` (grep across
   the whole tree). `computeCalibration` feeds only the public surface;
   `computeBudget` takes no factor. Our REF-R8×REF-R9 wiring (factor
   multiplies the risk budget) remains strictly ahead.
3. **Do they apply the wash-trade filter?** YES — `isFakeChart` is applied
   in `market.server.ts:237` (fake-chart pairs return null), the same
   READ-stage placement as our A7 port. Parity.

### What they have that we don't — and the verdict on each

| # | Their feature (module) | What it is | Verdict |
|---|---|---|---|
| 1 | Off-book positions (`offbook.server.ts`) | Hardcoded manual bookkeeping for value held OUTSIDE the tracked wallet (a BNB token through the FOMO app; the closed $BASECAT +$3,490.14 banked as a constant) so their terminal shows the whole account | **Deliberately not adopted.** Manual accounting for another chain/app; folding off-wallet value into the book would break our "journal + chain are the sole money authorities" invariant. If the operator ever holds positions outside the trading wallet, the right move is a separate disclosure line, never journal entries |
| 2 | Narration anti-repetition (`omo-brain.server.ts`) | Heavy dedupe machinery on the public thought stream: thought fingerprints, shingles, bigrams, Jaccard similarity, opening-trigram checks, verdict rotation/merge/reconcile-with-book | **Genuine gap — queued as a future UX item.** Our feed narration can repeat similar phrasing across ticks (A11's "advance, don't restate" contract covers write-ups, not feed thoughts). Not trading-critical, no safety value — pure reader experience |
| 3 | Three-model role routing (`models.server.ts`) | reasoning → claude opus 5, realtime → grok, narration → opus; honest resolution (`resolveRole` returns the id actually used + a degraded flag) | **Documented non-gap.** We run a two-provider split (main reasoning provider + Groq social) with the same honest-degradation contract (`LLMResult.degradation_reason`, source tagging). Finer per-role routing is a scale luxury, not a correctness gap |
| 4 | Separate memo-only burner key (`keys.server.ts` `OMO_COMMIT_KEY`) | A second key that can only write memos, so a leaked commit key can never touch funds | **Documented deviation (§26):** single signer = the trading wallet at this book's scale ($3–5 bootstrap). Their rationale is real defense-in-depth at their scale ($10k+ book); revisit if the book grows |
| 5 | AI-gateway abstraction + h3/preview error plumbing (`ai-gateway.server.ts`, `error-capture.ts`, `error-reporting.ts`) | Lovable-gateway headers; h3 stack recovery; preview-host error hooks | **Not applicable** — platform plumbing for their hosting stack. Our FastAPI + local-process logs already carry full tracebacks |

### Where they are genuinely better

- Per-role model selection (an instruction-adherence model for reasoning, a
  timeline-native model for social reads) — a real quality lever we don't use.
- The anti-repetition machinery on the public thought stream (item 2 above).
- Memo-signing custody separation (item 4) at their book scale.
- Their **deployed** machine clearly runs more than they publish (the exit
  module at minimum) — the deployed system is ahead of the public repo.

### Where we are genuinely better (with evidence)

- **Published and runnable.** A fresh clone passes the full suite (486
  tests). Their published repo cannot even run its own tests —
  `exit-rules.test.ts` imports the unpublished `../exit.server`.
- **Calibration is actually wired.** REF-R8×REF-R9: the conviction factor
  multiplies the risk budget in sizing. Final grep proof this pass:
  `convictionFactor` appears nowhere outside `learn.server.ts`; their
  `computeBudget` never receives it.
- **A paper layer at all.** They have none. Ours is a full atomic journal
  with a 15s exit scanner and, since §32, bad-quote guards
  (`EXIT_PRICE_JUMP_MAX`, `MAX_EXIT_PROCEEDS_MULT`) + proceeds-bound
  backstops on every close/trim.
- **Deeper live safety model.** Kill switch + daily-loss breaker + manual
  confirmation with fail-closed expiry + idempotency ledger + identity pin
  + caps + a repeatable devnet drill regime (§27/§31).
- **The exit engine is public and tested.** Same contract as their
  unpublished module (hard stop; trail armed only after a real run;
  liquidity break even in profit; price+flow invalidation; TP tranches —
  0.33 first, never retaken; risk-break prefers full exit; stale close;
  sell gate) — ours published since 2026-08-20 (decisionLog #20), plus the
  fast scanner and quote guards they don't publish at all.
- **Audit items closed:** A2 chain reconciliation, A3 venue attribution, A4
  own-basis cross-check, A6 blocklist, A7 wash-trade filter (parity —
  theirs applied at `market.server.ts:237`, ours at the READ stage), A11
  thesis restatement (parity).

### Final verdict (2026-08-28)

No trading-critical parity gap remains. Everything left on their side is UX
polish (narration dedupe), scale-dependent custody (memo burner key), or
hosting plumbing. The balance has flipped: on everything comparable from
public code, this machine is now ahead of their public repo — and unlike
theirs, all of it is verifiable from a fresh clone.

---

## Full logic-and-rules comparison — deep re-read, later 2026-08-28

Same upstream commit (`48a86f9`, no drift). This section is the consolidated
side-by-side the operator asked for: how each bot works, rule by rule, and
the four lists — what they have that we don't, what we have that they don't,
where we are better, where we are worse. Every number below was read out of
the two codebases, not remembered.

### 1. The loops

| Stage | omo (`pipeline.server.ts`) | ours (`run_live_cycle.py` / `backend/main.py`) |
|---|---|---|
| cadence | 45s tick (`TICK_MS=45_000`), wallet snapshot 60s, equity read 8s | 60s live cycle (paper tick was 60s; exit scan 15s) |
| manage | `runExitCycle()` first — risk off before risk on | `_manage()` first — same ordering |
| read | DexScreener rotated queries + boosts/profiles, fomo board, web search | Birdeye memepool trending + Dexscreener enrichment, fomo board (Privy), web + social enrichment |
| candidates | **one per cycle** (`candidates[0]`, top of screener) | up to `MAX_CANDIDATES_PER_TICK` per tick |
| think | reasoning role writes <60 words ending "buy"/"pass"; `wantsBuy = /\bbuy\b/ && !/\bpass\b/` regex | DeepSeek structured JSON `{thesis, invalidation, verdict}`; entry needs `verdict=="buy"` AND all rules |
| gate | `evaluateRules()` — the same 9 rules the audit log publishes | `evaluate_gate()` — 11 rules, no short-circuit, every refusal journalled with full breakdown |
| seal | sha256(decision+rules+inputs+nonce) → Solana memo BEFORE the order | identical seal → memo BEFORE the fill, fail-closed (no memo = no order) |
| execute | Jupiter quote → local sign → mainnet RPC; "unarmed" without key | same path through `live_execution.executor` with a deeper guard block |
| journal | supabase audit row; signature bound when the fill shows up | `decision_commits` + retro matcher + binding report |
| reveal | **20 minutes later** the plaintext is opened | **immediate** — `/api/verify.json` recomputes the hash and checks the memo on chain |

### 2. Entry rules, side by side

Their `evaluateRules()` (audit.server.ts) is the single source of truth for
both the live gate and the audit log — the same design principle as our
`ACTIVE_RULES`. Theirs has **9 rules; ours has 10** (the same 9, plus one).

| # | rule | omo threshold | ours | verdict |
|---|---|---|---|---|
| 1 | liquidity_floor | ≥ $15,000 | ≥ $15,000 | parity |
| 2 | volume_alive | 1h ≥ $8,000 | 1h ≥ $8,000 | parity |
| 3 | buy_pressure | buys > sells (1h) | buys > sells (1h) | parity |
| 4 | not_newborn_fade | age 0–24h AND 1h < −15% fails | age < 24h AND 1h ≤ −15% fails | parity |
| 5 | public_presence | socials OR site | twitter/telegram/website, any known-present | parity (ours: unknown ≠ absent) |
| 6 | crowd_heat | heat 25–90; heat = 20 + 8×written-theses | heat 36–100; real board heat, else 20 + 8×signals | **different band** (see §6) |
| 7 | cash_available | ≥ $25 | ≥ ticket (live: real USDC balance) | ours is ticket-aware |
| 8 | already_held | no size in name | no size in mint | parity |
| 9 | not_on_break | loop awake | liveness break state | parity |
| 10 | — | — | **security_clear** (mint authority / honeypot known-bad) | **ours only — ACTIVE since 2026-08-29** |
| 11 | — | — | market_regime_ok (market-wide euphoria filter) | ours only — retired from gate, observability |

Discovery filters before the gate: their `isFakeChart()` (13 checks) +
`newbornFaded()` at `market.server.ts`; our `fake_chart.py` (all 13
thresholds, A7) at the READ stage + symbol blocklist (A6). Parity, applied
at equivalent points.

### 3. Exit rules, side by side

Their `exit.server.ts` is **still unpublished** — but their
`__tests__/exit-rules.test.ts` (102 lines) pins the exact contract, and a
fresh clone of their repo fails those tests because the module is missing.
Ours (`rule_engine/exits.py`) has been public and tested since 2026-08-20.

| rule | omo (from their test pins) | ours (config values) | verdict |
|---|---|---|---|
| hard stop | full close at `stopLossPct` | full close at −20% | same model |
| trailing | arms only after a real run (peak 40% does NOT arm; peak 120% → give-back to 70% fires ⇒ ~50pp give-back, activation between 40–120%) | arms at HWM +50%, fires on 40pp give-back | same model, ours published |
| liquidity break | full exit even at +50% (liq $4k breaks, $120k holds) | full exit below $8k even in profit | same model |
| invalidation | price AND flow both break (chg6h −40, sells 3× buys) | chg6h ≤ −25% AND sells > 1.4× buys (6h) | ours triggers earlier |
| profit tranches | 150% ⇒ 0.33 trim (tranche 0), never retaken; risk-break prefers full exit over trim | TP ladder +100/+300/+900 trims, never retaken; risk-off rules take priority | same model |
| stale close | `staleDays`+1, flat pnl, dead volume | 14 days, \|pnl\| ≤ 10%, vol6h < $5k | same model |
| sell gate | min clip ($5 dust blocked), cooldown (2 min blocked), daily exit ceiling | $25 min clip, 30-min per-mint cooldown, 8 exits/24h | same model |
| risk-off bypass | not visible (module unpublished) | **documented**: stops/liquidity-break bypass cooldown+ceiling — blocking a stop in a cascade is how books bleed | ours explicit |
| cadence | once per 45s cycle | **dedicated 15s exit scan** | ours faster |
| bad-quote protection | nothing published | `EXIT_PRICE_JUMP_MAX` 50% + `MAX_EXIT_PROCEEDS_MULT` 200× + proceeds-bound backstops on every close/trim (§32) | **ours only** |

### 4. Sizing and risk

- **Their pipeline** sizes with `ticketUsd(cash, conviction) =
  min(cash × 0.15, $3,000) × conviction`, where conviction is crowd heat
  (0..1). That is the whole story in `runDecisionCycle`.
- **Their `risk.server.ts::computeBudget`** exists and is **byte-identical to
  our REF-R8 port**: equity × 0.035 × drawdown factor (clamp(1 + dd×2.5,
  0.5, 1)), clamped [$25, $3,000]; daily = ×4 capped $12,000. **But it is not
  wired into `ticketUsd`** — verified again this re-read. Their calibration
  (`learn.server.ts::computeCalibration`: expectancy → factor, confidence
  pulled to 1 below 12 samples, clamped [0.6, 1.2]) is likewise computed and
  published but never multiplies a ticket.
- **Ours**: `SIZING_MODE=risk_budget` runs exactly their computeBudget, then
  **multiplies by their calibration factor** (REF-R9, same formula, same
  [0.6, 1.2] clamp) — the wiring their public code never does. Live floor
  `MIN_LIVE_TICKET_USD=$0.5` for the micro-bootstrap; hard ceilings $3k/$12k;
  daily ceiling enforced + journalled.

### 5. Model usage

| | omo | ours |
|---|---|---|
| roles | ai-gateway roles: "realtime" (grok) social read <40 words; "reasoning" thesis <60 words | DeepSeek V4 Flash main (thinking mode disabled); Groq for social evidence |
| output | free text, regex-parsed for buy/pass | structured JSON `{thesis, invalidation, verdict}` — no regex guessing |
| invalidation | "what would make you wrong" inside the prose | dedicated `invalidation` sentence stored with the trade |
| degradation | `.catch(() => null)` → "model gave nothing usable" | fail-closed deterministic template tagged `degraded:*`; provider swap; peak-window skip (2× pricing hours) |
| accounting | none published | `llm_call_usage` per call: tokens, latency, cost, cache, model, degradation reason |
| restatement | `thesis-author.server.ts` rewrites stale write-ups | A11 parity (`thesis_restate.py`) |

### 6. The one real rule difference: crowd_heat band

Their act band is **[25, 90]**; ours is **[36, 100]**. Consequences:
- We demand more written conviction before acting (36 vs 25) — stricter entry.
- They refuse heat > 90 (hype peak). Our proxy heat is capped at 100 by
  construction, so on the proxy path our upper bound can never fire; it only
  fires when real board heat arrives > 100. **Their peak-refusal is stricter
  in practice.** If we want it, lowering `CROWD_HEAT_MAX` to 90 is a one-line
  config change — recorded here as the only candidate follow-up this audit
  found, and deliberately NOT applied without an operator decision.



---

## F. 2026-08-31 live-disclosure ground truth (the re-audit that matters)

The operator asked: *"audit omotrades from start, clean any cache/docs, and
figure out how they are so successful."* The source re-audit found `main`
unchanged since our 2026-08-27 clone (commit `48a86f9`, 9 commits) — **the new
evidence is their LIVE public disclosure**, which went up after our audit was
written. Read from `omotrades.com/api/public/disclosure.json` +
`/api/public/exits.json`, 2026-08-31:

| their number | value |
|---|---|
| equity | $220,674 |
| closed samples | 32 |
| **hit rate** | **9.4%** |
| **avg win** | **+59.71%** |
| **avg loss** | **−7.4%** |
| expectancy | −1.11% |
| conviction factor | 0.956 (expectancy-scaled, clamped 0.6–1.2) |
| order intents vs declines | 175 vs 504 (**74% decline rate**) |
| per-order limit | equity×0.035×drawdown×conviction = $7,384, clamped [25, equity×0.08 backstop] |
| per-day limit | per-order × 4 |
| exit thresholds | stop −35%, trail arm +60%/give-back 40pp, TP +100/300/900 (33/33/50%), stale 14d, min clip $25, cooldown 30m, 8/day |
| armed | **false** (paused; their marks 5.4h stale at read time) |

**The honest reading — "how they are so successful":** they are NOT
per-trade profitable either (−1.11% expectancy). What they have is a
**structure that survives and catches tails**: (1) losses are CUT early by a
manage loop that re-prices every position every cycle and an exit-notes LLM
in the loop — the −35% stop realizes at **−7.4% avg** because something
almost always acts before the stop; (2) the model declines **74%** of
candidates (a bouncer, not a rubber stamp); (3) winners run to a +60% trail
arm (avg win +59.71% — the tail pays for the book); (4) tickets scale with
the machine's own expectancy × drawdown.

**Our measured baseline on the same format** (`scripts/perf_report.py`,
§50 Phase 0, 2026-08-31, 24 closed live trades): hit rate 12.5% (*higher than
theirs*), avg win **+57.88%** (≈ theirs), avg loss **−26.05%** (vs −7.4%),
expectancy −15.56%, think-pass ≈ everything, feed fail-share 95.2% (gate
refusals — the LLM almost never refuses what the gate lets through).

**Conclusion recorded:** our gap is NOT the win side (already at parity) and
NOT the hit rate (already higher). It is (a) the **loss shape** — nothing
between entry and the −20% stop ever acts on the live book — and (b) the
**missing refusal layer** (the LLM passes everything). §50 targets exactly
these two; the TP ladder (+100/300/900) and trail (+50/40pp) stay verbatim —
re-raking them down (an earlier plan idea) would have killed the tail that
pays for the book, exactly the edge omo's numbers prove.

**Cache cleanup verified (2026-08-31):** no local omo clone exists
(`~/Downloads/Projects` holds only `trading-bot` + `community-edify`); no omo
code vendored anywhere (parity ports only, with attribution); no omo-related
`/tmp` artifacts; the fomo/scrapling caches are OURS and stay. This section
supersedes any earlier characterization of their performance in this file
and in docs/10.


