# 05 — Verification Appendix (optional future work, not part of the initial build)

## Purpose of this document

This documents a cryptographic proof-of-decision mechanism observed in a
comparable live public system's own published, machine-readable
specification (`the reference site/api/public/verify.json`). It solves a
specific problem — proving to an anonymous public audience that a
decision's reasoning existed, unmodified, before the resulting trade was
executed — that this project does not currently have, since it runs
locally, privately, and with no real funds. It's documented here for
completeness and as a reference if this project ever moves toward a public
track record with real capital, at which point it becomes a genuinely
useful pattern to adopt. **It is explicitly not part of the 10-day
build/calibration plan in `03_GANTT_CHART.md`.**

## The mechanism, as published — and independently verified 2026-08-22

The mechanism below was re-checked against the live system and **verified
byte-for-byte**: two fully revealed decisions from the reference site/proof were
hashed locally and matched their published on-chain hashes exactly. See
`06_REFERENCE_COMPARISON.md` for the check, its evidence, and a full
rule-by-rule comparison against our implementation.

1. **Before acting on a decision**, the system constructs a canonical
   representation of the full decision payload (the candidate data, every
   rule result, the verdict, and the narration text) and generates a
   random nonce. Canonical = object keys sorted, undefined dropped.
2. It computes a hash: `sha256("commit-v1|" + nonce + "|" +
   canonical(payload))`.
3. It writes **only the hash** — not the payload — on-chain, as a Solana
   memo instruction, in the format `commit:v1:<hash>`. This transaction
   is paid for by a separate, dedicated key used only for posting these
   memos (isolating the proof mechanism's costs and permissions from the
   trading wallet itself).
4. Only later (the published system uses a 20-minute delay) is the
   plaintext payload and nonce revealed publicly.
5. **Anyone can independently verify**: fetch the on-chain memo
   transaction via a standard Solana RPC `getTransaction` call, recompute
   `sha256("commit-v1|" + nonce + "|" + canonical(revealed_payload))`
   locally, and confirm it matches the on-chain hash; additionally check
   that the fill transaction's slot is strictly greater than the memo
   slot, that its pre/post token balances contain the committed mint, and
   that its signer is the published wallet. Because the hash was
   posted on-chain (with a real, externally-verifiable timestamp) before
   the plaintext was ever revealed, this proves the exact reasoning
   existed at that time and was not edited afterward to look better in
   hindsight.
6. The published system also exposes `/api/public/verify.json` with the
   wallet, an execution-path description, replication steps, and per-row
   verification totals {checked, verified, failed}.

## Why this is a genuinely good pattern, and why it doesn't apply yet

This is a well-designed, minimal-trust mechanism: it doesn't require
trusting the operator's server logs, a database that could be edited after
the fact, or any centralized attestation — only a Solana RPC endpoint and
basic hashing, both independently checkable by anyone. It directly solves
the "how do I know you're not just cherry-picking or rewriting your
reasoning after seeing the outcome" problem inherent to any public track
record.

This project doesn't have that problem yet, because:
- There is no real wallet and no real funds — nothing to prove regarding
  actual capital at risk.
- There is no external audience the operator needs to convince — the
  trade log's trustworthiness only needs to satisfy the operator
  themselves during the paper-trading calibration phase, which is better
  served by direct database inspection and the atomicity guarantees in
  `01_ARCHITECTURE.md` §5.1 than by a public cryptographic proof system.
- Building this adds real complexity (a second dedicated on-chain key,
  memo-posting logic, a public verification page, canonical
  payload-serialization discipline) that has no payoff until there's an
  actual audience or actual capital for it to protect.

## If this is ever revisited

Worth doing only after (and as a separate, deliberate decision from) any
future move toward live trading with real capital, at which point:
- The canonical payload should be the full `GateDecision` (all rule
  results, not just the final verdict) plus the LLM narration text, so the
  proof covers the entire decision trail, not just the outcome.
- A dedicated, minimally-funded key for posting memos, separate from any
  trading wallet, mirrors the isolation already observed in the reference
  system and limits what a compromised proof-posting key could ever affect.
- The delay-before-reveal window (the reference system uses 20 minutes) is
  a tunable trust/latency tradeoff — long enough that the hash's on-chain
  timestamp is clearly prior to the reveal, short enough to still be
  useful to an audience following in near-real-time.

This remains documented here, not built, until that day.

## Related future-scaling item: direct RPC/mempool subscriptions

Filed alongside this appendix rather than in the main architecture
document for the same reason: it belongs to the "matters once real capital
and real latency sensitivity are in play" category, not the current
paper-trading, free-tier-API phase. See `01_ARCHITECTURE.md` §5.4 for the
brief note — direct validator RPC/mempool subscriptions replace periodic
REST polling for lower-latency data, which is real infrastructure
sophistication with no payoff until tick-to-tick latency is actually the
binding constraint on results, which it currently is not.
