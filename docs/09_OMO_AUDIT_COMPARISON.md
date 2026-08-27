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

**Operator decision:** implement **2, 3, 4, 6, 7**. (1, 5, 8, 9, 10 left as-is —
see "Deliberately not adopted" below.)

---

## B. Where we were worse at audit time

1. **Model quality:** they run **Claude Opus 5** (reasoning + narration) and
   **Grok 4.1** (realtime social) per-role with honest degraded-routing records.
   We run cheaper models (Groq-hosted; DeepSeek migration queued behind
   shadow-replay proof). Cost-efficient, weaker reasoning horsepower.
2. **Live track record:** they have real mainnet fills, a public wallet with real
   equity, venue-labeled journal, off-book disclosure. We're paper + disarmed —
   *by our own sequencing choice*, but a factual gap today.
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
  just code.
- `placeOrder`'s guard block was middle-truncated in every fetch; the guard list
  is taken from the module's own header documentation plus all visible code —
  accurate but not verbatim-quoted.
- Their calibration wiring claim is flagged "unverified", not "false".

    calibration — they have no paper layer.
