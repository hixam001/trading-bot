# 14 — SIGNING GUARD ARCHITECTURE & RESIDUAL RISK

## Overview

The signing guard implemented in [`backend/live_execution/jupiter_executor.py`](backend/live_execution/jupiter_executor.py) (`inspect_swap_transaction`) and [`backend/live_execution/sim_guard.py`](backend/live_execution/sim_guard.py) (`simulate_and_assert`) protects the bot's wallet against malicious payloads, compromised swap quotes, and blind-signing drain attacks before any transaction is signed with the wallet's private key.

---

## 1. What the Static Guard CAN Inspect (Layer 1)

The static guard performs strict structural analysis on the deserialized Solana transaction (`VersionedTransaction` / legacy message format) immediately before signing:

1. **Fee Payer Verification:**
   - The fee payer account (account 0) must strictly match the loaded keypair address (`payer_pubkey`). Transactions attempting to use the wallet as fee payer for unauthorized third parties fail closed.

2. **Program Allowlist Enforcement:**
   - Every program invoked at the top level must belong to [`APPROVED_SWAP_PROGRAM_IDS`](backend/live_execution/config.py).
   - Unknown or unapproved third-party programs immediately trigger an `ExecutionError`.

3. **Instruction-Level Deny-by-Default (Per-Program Discriminants):**
   - **System Program (`11111111111111111111111111111111`):**
     - `CreateAccount` (tag 0): Allowed ONLY if the new account's owner program is strictly SPL Token or Token-2022 (not DEXes). Account `space` must be $\le 165$ bytes. Lamports transferred must be $\le$ `MAX_CREATE_ACCOUNT_LAMPORTS` (2,500,000 lamports = 0.0025 SOL).
     - `Transfer` (tag 2) & `TransferWithSeed` (tag 11): Allowed ONLY if the destination is proven wallet-owned (the payer, an ATA owned by payer, or an account created in this transaction and initialized to the payer). Foreign transfers are unconditionally rejected.
     - **Top-Level SOL Outflow Cap:** The total lamports leaving the payer across all top-level `System.Transfer`, `TransferWithSeed`, and `CreateAccount` instructions must not exceed `MAX_SYSTEM_OUTFLOW_LAMPORTS` (10,000,000 lamports = 0.01 SOL).
     - `Assign` (tag 1), `Allocate` (tag 4), `AllocateWithSeed` (tag 8), `AssignWithSeed` (tag 9): Rejected unconditionally.
     - Any unknown System Program discriminant: Rejected.
   - **SPL Token & Token-2022 (`Tokenkeg...`, `Tokenz...`):**
     - `InitializeAccount` (tag 1): Owner account (`accounts[2]`) must strictly match `payer_pubkey`.
     - `InitializeAccount2` (tag 16) & `InitializeAccount3` (tag 18): Owner pubkey in instruction data (`data[1:33]`) must strictly match `payer_pubkey`.
     - `Transfer` (tag 3), `TransferChecked` (tag 12): Rejected unconditionally at top level. Legitimate swaps route through the DEX program via CPI. Top-level token transfers in a swap transaction indicate wallet exfiltration.
     - `Approve` (tag 4), `ApproveChecked` (tag 13): Rejected unconditionally (prevents token approvals to drainer contracts).
     - `SetAuthority` (tag 6): Rejected unconditionally (prevents transferring ownership of token accounts).
     - `MintTo` (tag 7), `Burn` (tag 8), `BurnChecked` (tag 15): Rejected.
     - `CloseAccount` (tag 9): Allowed ONLY if destination account is strictly our wallet pubkey.
     - `SyncNative` (tag 17): Allowed.
     - Any unknown Token instruction tag or truncated instruction data: Rejected.
   - **Associated Token Account Program (`AToken...`):**
     - `Create` (tag 0) / `CreateIdempotent` (tag 1): Allowed ONLY if the wallet owner account matches our payer pubkey.
   - **Compute Budget & Memo Programs:** Allowed at top level.
   - **Approved DEX Aggregators & AMMs:** Allowed at top level.

4. **Address Lookup Tables (ALTs) Guard:**
   - Swaps are requested as legacy transactions (`asLegacyTransaction: True`). Any transaction containing `address_table_lookups` is rejected because dynamic lookup regions prevent static verification of program IDs.

5. **Proven Wallet Ownership:**
   - Accounts are not inferred to be wallet-owned merely because the payer funded them. An account created via `CreateAccount` is only recognized as wallet-owned once initialized with owner equal to the payer. Any transfer before initialization is refused.

---

## 2. What Static Analysis Cannot See (The Need for Simulation)

Despite comprehensive top-level static inspection, Solana's runtime architecture imposes intrinsic boundaries:

### Cross-Program Invocations (CPI)
When a transaction invokes a top-level DEX aggregator (e.g. Jupiter v6 `JUP6Lkb...`), the DEX program internally calls liquidity pool contracts (Raydium, Orca, Meteora, Whirlpool) via CPI.
- **Top-level visibility only:** A client-side static transaction inspector can only see the outer instructions specified in `message.instructions`. Inner instructions executed dynamically during smart contract execution are not stored in the unsigned transaction message.
- **Opaque execution blobs:** The byte payload passed to Jupiter is an opaque route execution plan. The exact CPI calls that the Jupiter program will execute on-chain cannot be decoded through client-side static analysis alone.

---

## 3. Defense-in-Depth: Three-Layer Execution Protection

To protect against malicious routes, compromised quote APIs, and hidden CPI drains, the system enforces a strict three-layer defense model:

```
[ Unsigned Tx ]
      │
      ▼
┌────────────────────────────────────────────────────────┐
│ Layer 1: Static Instruction Guard                      │
│ (inspect_swap_transaction in jupiter_executor.py)     │
│ - Verified program IDs                                 │
│ - Strict owner verification on token account inits     │
│ - Proven wallet-ownership tracking                     │
│ - CreateAccount size & lamport limits                  │
│ - Outflow cap (MAX_SYSTEM_OUTFLOW_LAMPORTS)            │
└────────────────────────────────────────────────────────┘
      │
      ▼
┌────────────────────────────────────────────────────────┐
│ Layer 2: Pre-Sign Simulation & Balance Assertions      │
│ (simulate_and_assert in live_execution/sim_guard.py)   │
│ - Dual-RPC simulateTransaction against live state      │
│ - Verifies balances: SOL decrease <= 0.01 SOL          │
│ - Input token decrease <= intended + 1% tolerance      │
│ - Output token increase >= otherAmountThreshold        │
│ - Zero balance change on other wallet token accounts   │
│ - No unauthorized account owner changes or closures    │
│ - Traps value redirection to third-party accounts      │
└────────────────────────────────────────────────────────┘
      │
      ▼ (Sign Transaction with Wallet Keypair)
┌────────────────────────────────────────────────────────┐
│ Layer 3: RPC Preflight on Broadcast                    │
│ (solana.send_raw_transaction with skipPreflight: False)│
│ - Solana node verifies program execution status        │
│ - Rejects expired blockhashes and landing drift        │
│ - Fails closed before network propagation              │
└────────────────────────────────────────────────────────┘
      │
      ▼
[ Broadcast on-chain ]
```

### Layer 1: Static Inspection
Enforced synchronously before any signing operation. Evaluates all outer instructions, verifies every program ID against `APPROVED_SWAP_PROGRAM_IDS`, enforces owner equality on all token account initializations, restricts `CreateAccount` size and lamports, and caps total top-level SOL outflow.

### Layer 2: Pre-Sign Simulation with Balance-Delta Assertions
Implemented in [`backend/live_execution/sim_guard.py`](backend/live_execution/sim_guard.py).
Before signing, the transaction is simulated against independent Solana RPC nodes using `simulateTransaction` with `sigVerify: false`, `replaceRecentBlockhash: true`, and account post-state inspection:
- Pre-state is fetched for the wallet, its USDC ATA, the output mint ATA, and every writable token account in the transaction owned by the wallet via `getMultipleAccounts`.
- Simulation results across RPC endpoints are validated for consensus (SEC-01 dual-RPC consensus).
- Invariants asserted:
  1. `err is None` (simulation succeeded without runtime error).
  2. Wallet SOL decrease $\le$ `SIM_MAX_SOL_DECREASE_LAMPORTS` (10,000,000 lamports = 0.01 SOL).
  3. Input token decrease $\le$ quote input amount $\times (1 + \text{SIM\_INPUT\_TOLERANCE})$.
  4. Output token increase $\ge$ quote's `otherAmountThreshold` (verifies funds arrive in *our* ATA).
  5. Every other wallet-owned token account unchanged.
  6. No wallet-owned account has its owner modified or is closed unexpectedly.
  7. Value redirection check: any third-party account gaining balance while wallet balance decreases is refused.
- Hardcoded `SIMULATION_REQUIRED = True` ensures transactions cannot bypass simulation.

### Layer 3: RPC Preflight on Broadcast
In [`backend/live_execution/solana.py`](backend/live_execution/solana.py), `send_raw_transaction` enforces:
```json
{
  "encoding": "base64",
  "skipPreflight": false,
  "maxRetries": 3,
  "preflightCommitment": "confirmed"
}
```
**Why `skipPreflight=False`:**
When `skipPreflight` is `false`, the Solana RPC node runs a final check against the validator's local bank immediately before broadcast. This catches last-second blockhash expirations, nonces, or conflicting transactions.
**Clarification:** RPC preflight checks only whether the transaction execution returned an error code; it does *not* verify balance deltas or enforce wallet-level invariants. Layer 2 (`sim_guard.py`) is the layer that verifies balances and asserts economic correctness.

---

## 4. Residual Risks

Even with all three defensive layers active, specific residual risks inherent to smart contract systems remain:

1. **Malicious Upgrade of Allowlisted Programs:**
   - DEX aggregator programs (e.g. Jupiter v6) and AMM routing contracts are mutable and upgradeable by their governing multisigs or deployer keys. If an allowlisted program is upgraded with malicious logic, static checks and simulated execution will see an approved program ID. Layer 2 bounds maximum monetary loss per transaction to the quoted input amount plus rent caps.
2. **Simulation vs. Landing-Time State Drift:**
   - On-chain state can change between the moment of simulation and the moment the transaction lands in a validator's block (e.g. extreme slippage, sandwich attacks, or liquidity pool depletion). Quoted slippage limits (`otherAmountThreshold`) bound this loss, and Layer 3 preflight catches errors if pool state changes drastically.
3. **Compromised RPC Endpoints:**
   - A malicious or compromised RPC node could falsify simulation results. This is mitigated by dual-RPC consensus verification in [`backend/live_execution/solana.py`](backend/live_execution/solana.py), but remains a residual risk if both configured RPC endpoints share an upstream provider or infrastructure.

---

## 5. Offline Inspection & Verification Tooling

Operators can inspect real Jupiter quotes and swap transactions without loading private keys:

```bash
# Test quote generation, static inspection, and pre-sign simulation offline:
python scripts/inspect_live_quote.py --wallet <PUBLIC_KEY> --input-usdc 1.0 --mint <TARGET_MINT>
```

This tool:
1. Requests real quotes and swap transaction payloads from Jupiter v6 API (`asLegacyTransaction=true`).
2. Runs Layer 1 static inspection (`inspect_swap_transaction`).
3. Runs Layer 2 pre-sign balance simulation (`simulate_and_assert`) against live Solana RPC.
4. Outputs a decoded instruction table and PASS/REFUSED verdict without ever loading private keys or signing.
