# 06 — Reference Comparison: trading-bot vs the reference site

> **Historical snapshot (2026-08-22).** The rule set and execution model have
> changed since this comparison: `crowd_heat`, `already_held`, and
> `not_on_break` are now active; `market_regime_ok` is retired from the gate;
> the paper engine was deleted (§52) and this repository now executes live
> through `backend/live_execution/`. See `docs/01_ARCHITECTURE.md` §2.3 and
> `docs/02_FEATURE_LIST.md` for the current state.

Checked 2026-08-22 against https://the reference site/, /proof, and its published
`/api/public/verify.json`. Evidence includes independent local recomputation
of two revealed commit hashes (see §2).

## 1. Rule set alignment

the reference bot' gate (from revealed commit payloads) vs ours:

| # | their rule_id | detail example | our rule_id | aligned? |
|---|---|---|---|---|
| 1 | `liquidity_floor` | `pool $19,937` | `liquidity_floor` | YES — same id, same concept; our detail adds the threshold value |
| 2 | `volume_alive` | `1h volume $177,402` | `volume_alive` | YES — identical |
| 3 | `buy_pressure` | `1880 buys vs 6469 sells` | `buy_pressure` | YES — identical (tx counts, not dollars) |
| 4 | `not_newborn_fade` | `age 1.4h, 1h 18.6%` | `not_newborn_fade` | YES — identical joint age+momentum condition |
| 5 | `public_presence` | `twitter` | `public_presence` | YES — any-channel-passes; they name channels, we do too |
| 6 | `crowd_heat` | `fomo 20` | `crowd_heat` | ALIGNED since §44 — fomo.fun heat with a presence-proxy fallback; band-gated |
| 7 | `cash_available` | `cash $55817.50` | `cash_available` | YES — identical |
| 8 | `already_held` | `no size on` | `already_held` | ALIGNED — one position per name, no scale-ins |
| 9 | `not_on_break` | `awake` | `not_on_break` | ALIGNED — operator break/liveness gate, fail-closed |
| 10 | — | — | `market_regime_ok` | OUR ADDITION — retired from gate (observability only) |
| 11 | — | — | `security_clear` | OUR ADDITION — ACTIVE in gate since 2026-08-29 (None-means-unknown semantics) |

Their extra *inputs* we don't consume: `chg6h`, a `researched` deep-dive flag,
a numeric `socials` list, and the `fomo` index itself.

## 2. Commit-reveal proof mechanism — independently verified

From `/proof` we took two fully revealed entries and recomputed their hashes
locally (`backend/verify_commit.py` during the check):

```
sha256("commit-v1|" + <nonce> + "|" + <canonical payload>)
  == 0cd102c7…7b0a67ea   MATCH (byte-for-byte)
  == 21fcce86…e765d8fbb  MATCH (byte-for-byte)
```

Confirmed mechanics, all matching `05_VERIFICATION_APPENDIX.md`:
- canonical payload = sorted object keys, undefined dropped
- memo format `commit:v1:<hash>`, written pre-fill by a **separate burner
  key** that never holds the book
- reveal ~20 minutes later; fill slot must be **greater than** memo slot;
  fill's token balances must contain the committed mint; signer must be the
  published wallet
- `verify.json` exposes wallet, executionPath steps, howToReplicate,
  totals {checked, verified, failed}, rows[]

Conclusion: our appendix documented the real mechanism accurately. The
mechanism is now built for live entries (`live_execution/memo.py` publishes a
confirmed commit memo before the fill), so this is no longer paper-only theory.

## 3. Logic divergences — deliberate or noted

### 3.1 THE BIG ONE: who decides the final action
the reference bot' revealed payloads show `verdict:"pass"` **with two FAILED rules**
(`buy_pressure`, `crowd_heat`), `side:null`, and a note reading *"refused
market buy…"*. I.e., in their architecture an agent layer sits between the
rules and the action: rules are inputs, the model decides whether to trade.

This project deliberately CLOSES that gap: `evaluate_gate()`'s all_passed is
the sole entry decision; exits come only from fixed numeric conditions; the
LLM narrates decisions that were already made (§4.1) and may never flip a
verdict. This is the project's core principle and stays non-negotiable —
the reference bot demonstrates the alternative, not a defect in ours.

### 3.2 `crowd_heat` (fomo index)
They gate on a 0–100 "fomo index" sourced from the fomo app. Since §44 this
project consumes the same fomo.fun board (with a documented presence-proxy
fallback) and gates on the configured act band. `market_regime_ok` remains
universe-level observability, not a gate.

### 3.3 `already_held`
Their gate blocks new buys whenever ANY size is held (`heldUsd > 0` fails).
Ours now does the same: `already_held` is active and there is no scale-in.
Partial *exits* exist (take-profit ladder), but no adds.

### 3.4 `not_on_break`
An operational liveness rule (the agent can be "on break"). Ours implements
the same gate (`rule_engine/liveness.py`), failing closed on corrupt state.

## 4. Everything else checked out

- Rule detail style (short human strings with the real numbers) matches.
- Every rejection logged at full detail matches their philosophy.
- Their "stalking" state ≈ our feed/journal observation layer; no change needed.
- Non-goals have narrowed: the engine now executes real capital through
  `backend/live_execution/`, with the same commit-reveal proof mechanism and a
  signing guard. The comparison above predates that switch; treat §3 as the
  historical rationale, not the current architecture.
