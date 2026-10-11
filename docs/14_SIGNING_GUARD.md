# 14 — SIGNING GUARD ARCHITECTURE & RESIDUAL RISK

## Overview

The signing guard implemented in [`live_execution/jupiter_executor.py`](file:///home/hixam/Downloads/Projects/trading-bot/backend/live_execution/jupiter_executor.py) (`inspect_swap_transaction`) protects the bot's wallet against malicious payloads, compromised swap quotes, and blind-signing drain attacks before the transaction is signed with the wallet's private key.

---

## 1. What the Guard CAN Inspect

The guard performs static inspection on the deserialized Solana transaction (`VersionedTransaction` / legacy message format) immediately before signing:

1. **Fee Payer Verification:**
   - The fee payer account (account 0) must strictly match the loaded keypair address (`payer_pubkey`). Transactions attempting to use the wallet as fee payer for unauthorized third parties fail closed.

2. **Program Allowlist Enforcement:**
   - Every program invoked at the top level must belong to [`APPROVED_SWAP_PROGRAM_IDS`](file:///home/hixam/Downloads/Projects/trading-bot/backend/live_execution/config.py).
   - Unknown or unapproved third-party programs immediately trigger an [`ExecutionError`](file:///home/hixam/Downloads/Projects/trading-bot/backend/live_execution/jupiter_executor.py).

3. **Instruction-Level Deny-by-Default (Per-Program Discriminants):**
   - **System Program (`11111111111111111111111111111111`):**
     - `CreateAccount` (tag 0): Allowed ONLY if the new account's owner program is in `APPROVED_SWAP_PROGRAM_IDS` (e.g. SPL Token for temporary wSOL wrapping).
     - `Transfer` (tag 2) & `TransferWithSeed` (tag 11): Allowed ONLY if the destination is the wallet itself, an Associated Token Account (ATA) owned by the wallet (e.g. wSOL ATA), or if `lamports == 0`. Foreign transfers are unconditionally rejected.
     - `Assign` (tag 1), `Allocate` (tag 4), `AllocateWithSeed` (tag 8), `AssignWithSeed` (tag 9): Rejected unconditionally.
     - Any unknown System Program discriminant: Rejected.
   - **SPL Token & Token-2022 (`Tokenkeg...`, `Tokenz...`):**
     - `Transfer` (tag 3), `TransferChecked` (tag 12): Rejected unconditionally at top level. Legitimate swaps route through the DEX program via CPI. Top-level token transfers in a swap transaction indicate wallet exfiltration.
     - `Approve` (tag 4), `ApproveChecked` (tag 13): Rejected unconditionally (prevents token approvals to drainer contracts).
     - `SetAuthority` (tag 6): Rejected unconditionally (prevents transferring ownership of token accounts).
     - `MintTo` (tag 7), `Burn` (tag 8), `BurnChecked` (tag 15): Rejected.
     - `CloseAccount` (tag 9): Allowed ONLY if destination account is strictly our wallet pubkey.
     - `InitializeAccount*` (tags 1, 16, 18), `SyncNative` (tag 17): Allowed.
     - Any unknown Token instruction tag: Rejected.
   - **Associated Token Account Program (`AToken...`):**
     - `Create` (tag 0) / `CreateIdempotent` (tag 1): Allowed ONLY if the wallet owner account matches our payer pubkey.
   - **Compute Budget & Memo Programs:** Allowed at top level.
   - **Approved DEX Aggregators & AMMs:** Allowed at top level.

4. **Address Lookup Tables (ALTs) Guard:**
   - Swaps are requested as legacy transactions (`asLegacyTransaction: True`). Any transaction containing `address_table_lookups` is rejected because dynamic lookup regions prevent static verification of program IDs.

---

## 2. What the Guard CANNOT Inspect (Residual Risk)

Despite comprehensive top-level static inspection, Solana's runtime architecture imposes intrinsic boundaries:

### Cross-Program Invocations (CPI)
When a transaction invokes a top-level DEX aggregator (e.g. Jupiter v6 `JUP6Lkb...`), the DEX program internally calls liquidity pool contracts (Raydium, Orca, Meteora, Whirlpool) via CPI.
- **Top-level visibility only:** A client-side static transaction inspector can only see the outer instructions specified in `message.instructions`. Inner instructions executed dynamically during smart contract execution are not stored in the unsigned transaction message.
- **Opaque execution blobs:** The byte payload passed to Jupiter is an opaque route execution plan. The exact CPI calls that the Jupiter program will execute on-chain cannot be decoded through client-side static analysis alone.

---

## 3. Defense-in-Depth: Simulation and `skipPreflight=False`

Because CPI calls cannot be statically inspected prior to execution, the system employs a two-tier defense-in-depth model:

```
[ Unsigned Tx ]
      │
      ▼
┌───────────────────────────────────────────────┐
│ Tier 1: Static Instruction Guard              │
│ - Verified program IDs                        │
│ - Instruction discriminants & destination check│
│ - No top-level foreign transfers / approves   │
└───────────────────────────────────────────────┘
      │
      ▼ (Sign Transaction)
┌───────────────────────────────────────────────┐
│ Tier 2: Preflight Simulation (RPC Node)       │
│ - Validates entire CPI execution graph        │
│ - Verifies balances, slippage, and assertions │
│ - Aborts BEFORE network broadcast if failing  │
└───────────────────────────────────────────────┘
      │
      ▼
[ Broadcast on-chain ]
```

### Why `skipPreflight=True` is NEVER Allowed
In [`live_execution/solana.py`](file:///home/hixam/Downloads/Projects/trading-bot/backend/live_execution/solana.py), `send_raw_transaction` enforces:
```json
{
  "encoding": "base64",
  "skipPreflight": false,
  "maxRetries": 3,
  "preflightCommitment": "confirmed"
}
```

1. **Preflight Simulation as CPI Gatekeeper:**
   When `skipPreflight` is `false`, the Solana RPC node executes the transaction in a simulated bank environment against current chain state *before* broadcasting to validators.
   - If an internal CPI call fails slippage limits, encounters a bad state, or violates expected balance changes, the simulation errors and the RPC node refuses to broadcast the transaction.
   - A rejected preflight simulation costs zero SOL and incurs no on-chain losses.

2. **Dangers of `skipPreflight=True`:**
   Setting `skipPreflight=true` bypasses node simulation and directly pushes the raw transaction to validator leaders. If a compromised or hostile route causes unanticipated balance drains or unhedged trades, the transaction lands on-chain without any pre-broadcast safety check.

**Rule:** `skipPreflight` must remain `False` across all broadcast paths in the codebase.
