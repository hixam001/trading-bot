# 06 — Reference Comparison: trading-bot vs the reference site

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
| 6 | `crowd_heat` | `fomo 20` | *(none)* | DIVERGENT — see §3.2 |
| 7 | `cash_available` | `cash $55817.50` | `cash_available` | YES — identical |
| 8 | `already_held` | `no size on` | `exposure_cap` + routing | PARTIAL — see §3.3 |
| 9 | `not_on_break` | `awake` | *(none)* | DIVERGENT — see §3.4 |
| 10 | — | — | `market_regime_ok` | OUR ADDITION (informed by their regime concept) |
| 11 | — | — | `security_clear` | OUR ADDITION (None-means-unknown semantics) |

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

Conclusion: our appendix documented the real mechanism accurately, and its
status (documented, NOT built — paper trading needs no public proof) stands.

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
They gate on a 0–100 "fomo index" sourced from the fomo app (failed at
index 20–22 in observed samples; bounds unknown from public data). We have
no equivalent data source. Our `market_regime_ok` plays the same
*"is now a good time to be deploying at all"* role at universe level.
If a fomo-index source ever becomes available, a `crowd_heat` band rule is
a natural addition — post-calibration, tested like every other rule.

### 3.3 `already_held` vs `exposure_cap`
Their gate blocks new buys whenever ANY size is held (`heldUsd > 0` fails).
Ours routes held mints to `scale_into_position()` gated by
`MAX_EXPOSURE_PER_MINT_USD` (§5 unified entry/scale-in). Intentional: the
architecture spec explicitly wants add-to-winner support. Their live stream
shows heavy position MANAGEMENT (BLOSSOM +301%, SEAL +513% "stalking",
partial-language throughout), reinforcing §5.3's point that partial scaling
(E8/E9) is where mature behavior lives — still post-calibration scope.

### 3.4 `not_on_break`
An operational liveness rule (the agent can be "on break"). Ours runs
continuously; the fail-closed data path already stops trading when inputs
disappear (empty batch → regime BAD → zero entries). A formal
data-freshness/liveness rule is optional future polish, not correctness debt.

## 4. Everything else checked out

- Rule detail style (short human strings with the real numbers) matches.
- Every rejection logged at full detail matches their philosophy.
- Their "stalking" state ≈ our feed/journal observation layer; no change needed.
- Non-goals hold: they run REAL capital through Solana programs; we simulate
  only. The proof mechanism exists there because there is something to prove;
  per appendix, we adopt it only if a public track record with real capital
  ever happens.
