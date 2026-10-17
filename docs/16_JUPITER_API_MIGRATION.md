# 16. Jupiter API Migration & V2 Evaluation

> **Spike Result — No Production Switch.**
> Status: Evaluated, not adopted. Bot remains on `lite-api.jup.ag/swap/v1` with fail-closed safeguards.

---

## 1. Endpoints in Use

The bot interacts with Jupiter across the following files and lines:

| Endpoint | Method | Code Location | Purpose |
| :--- | :--- | :--- | :--- |
| `/swap/v1/quote` | GET | `backend/config.py:277`<br>`live_execution/jupiter_executor.py:238`<br>`data_providers/jupiter.py:88`<br>`scripts/dry_run_guard.py:91` | Fetches swap quotes with ExactIn and fixed slippage; price marks for open positions |
| `/swap/v1/swap` | POST | `backend/live_execution/config.py:75`<br>`live_execution/jupiter_executor.py:307`<br>`scripts/dry_run_guard.py:97` | Requests serialized unsigned transaction with `asLegacyTransaction=true` |
| `/swap/v1/program-id-to-label` | GET | `backend/config.py:278`<br>`scripts/refresh_dex_labels.py:62` | Fetches DEX program ID to label mapping to populate allowlist filter |

All endpoints are centralized through `backend/live_execution/jupiter_client.py` (§81 Phase G).

---

## 2. Deprecation Status & Facts (developers.jup.ag)

* **Fetch Date**: 2026-10-17
* **Primary Source**: `https://developers.jup.ag`
* **Official Status**:
  > *"Metis (Jupiter's low-level routing engine) and its legacy swap/v1 APIs have been superseded by Swap V2 (api.jup.ag/swap/v2)."*
  
  Jupiter identifies two successor architectures:
  1. **Meta-Aggregator (`/order` + `/execute`)**: Managed routing and execution; RFQ market maker competition; automated priority fee tuning and gasless execution.
  2. **Router (`/build`)**: Instruction-level composability; custom instructions, CPI, route filtering, and custom RPC transaction signing.

### Key/Limit Facts
* **Authentication Header**: `x-api-key: <KEY>` (verified from Jupiter Developer Portal documentation).
* **Keyless Limits**: Requests to `api.jup.ag` without an API key are rate-limited to 0.5 RPS (intended only for prototyping/testing).
* **Production Access**: Production traffic on `api.jup.ag` requires an API key provisioned through the Jupiter Developer Portal.
* **Legacy Host**: `lite-api.jup.ag` remains active and keyless for `swap/v1`, but is provided as an unmaintained public utility subject to sudden deprecation or IP throttling.

---

## 3. Guard Assumptions and Failure Modes Under V2

The trading bot's two-layer defense (`inspect_swap_transaction` static inspection + `simulate_and_assert` pre-sign simulation) makes strict structural assumptions about Jupiter quotes and transactions. Under a changed API shape, the guard fails closed as follows:

| Field / Parameter | Guard Requirement | If Missing in V2 | If Differently Shaped in V2 | Guard Action |
| :--- | :--- | :--- | :--- | :--- |
| `inAmount` | Must match local `intent.in_amount_raw` exactly | Fails (`inAmount mismatch`) | String/int conversion error or mismatch | Refuses trade before signing (`quote_mismatch`) |
| `outAmount` | Must be a positive integer | Fails (`outAmount must be positive`) | Non-integer or $\le 0$ | Refuses trade before signing (`quote_mismatch`) |
| `otherAmountThreshold` | Must satisfy $other \ge \lfloor out \times \frac{10000 - slippage}{10000} \rfloor - 1$ and exceed independent price floor | Fails (`otherAmountThreshold inconsistent`) | Missing floor or threshold below formula | Refuses trade before signing (`quote_mismatch`) |
| `slippageBps` | Must match local `intent.slippage_bps` exactly | Fails (`slippageBps mismatch`) | Value drift or unit change | Refuses trade before signing (`quote_mismatch`) |
| `swapMode` | Must be explicitly `"ExactIn"` | Fails (`swapMode invalid or missing`) | Any value other than `"ExactIn"` | Refuses trade before signing (`quote_mismatch`) |
| `platformFee` | Must be `null` or `amount == 0` | Passes (no fee) | Non-zero fee dict | Refuses trade before signing (`quote contains platformFee skim`) |
| `routePlan` | Used for label matching; programs verified on-chain | Allowed if empty; checked in tx | Different format or missing labels | DEX filter falls back or transaction check catches it |
| `asLegacyTransaction` | Must produce legacy wire transaction without Address Lookup Tables | Transaction contains `address_table_lookups` | Fails static bytecode inspection | Refuses trade before signing (`address lookup tables unverified`) |
| `prioritizationFeeLamports` | ComputeBudget instructions parsed in tx | Fee omitted or default scaled | Exceeds `PRIORITY_FEE_HARD_CAP_LAMPORTS` (1,000,000 lamports) | Refuses trade before signing (`priority fee exceeds hard cap`) |

---

## 4. Contract Test Verification

The fail-closed contract is enforced by `backend/tests/test_audit_81_g.py::test_quote_contract_fails_closed_on_missing_fields`. Removing or corrupting any required quote parameter causes `validate_quote_against_intent` to raise `QuoteMismatch` and abort execution before any transaction is built, signed, or broadcast.

---

## 5. Recommendation & Pre-Switch Verification Checklist

### Recommendation
**Stay on `lite-api.jup.ag/swap/v1`. Do NOT switch to Swap V2 at this time.**

Rationale:
1. Swap V2 defaults to versioned transactions with Address Lookup Tables (ALTs). The bot's Layer 1 signing guard strictly forbids ALTs because program IDs hidden inside lookup tables cannot be verified statically before signing.
2. The `/order` + `/execute` Meta-Aggregator delegates transaction construction and broadcast to Jupiter's backend, violating our non-negotiable security invariant that all transactions must be simulated and asserted locally against an independent multi-RPC quorum.
3. If `lite-api.jup.ag` is discontinued, the operator should provision a Jupiter API key, set `JUPITER_API_KEY` and `JUPITER_BASE_URL=https://api.jup.ag/swap/v1`, keeping the audited V1 wire format.

### Verification Checklist Before Any Future Migration
Before adopting any new Jupiter API endpoint:
- [ ] Run `python scripts/dry_run_guard.py --side buy --mint <MINT> --usd 10` on mainnet with no signing keys.
- [ ] Confirm `scripts/dry_run_guard.py` exits 0 with all assertions passing (`PASSED`).
- [ ] Confirm no Address Lookup Tables are present in the serialized transaction payload.
- [ ] Confirm ComputeBudget priority fees remain within `PRIORITY_FEE_HARD_CAP_LAMPORTS`.
- [ ] Prove simulated balance deltas land strictly in wallet-owned accounts.
- [ ] Execute devnet drill (`python backend/run_live_cycle.py --drill`) with a throwaway keypair.

