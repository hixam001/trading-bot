# Active Context — trading-bot

**As of 2026-08-23 (post Task C completion).** Full detail:
`handoff.md` (root). Repo: `/home/hixam/Downloads/Projects/trading-bot/`.

## Current focus
Tasks A (dual-lens discovery + thesis reuse), B (num_ctx + provider shutdown)
and C (live_execution safety model) are IMPLEMENTED AND VERIFIED:
backend suite 94/94 · live_execution 48/48 · combined root run 142/142 ·
verify_tasks.sh green end-to-end (§6 needs ollama running; skips cleanly).

## Task C — DONE (rebuilt to full spec this session)
The prior partial commit (8d73171) was superseded: live_execution/ now has
the complete seven-file safety model — config (hardcoded LIVE_TRADING_ENABLED
=False / REQUIRE_MANUAL_CONFIRMATION=True), models+ledger (idempotency,
exposure, realized P&L), wallet (lazy solders), kill_switch (manual +
automatic daily-loss breaker at -$75 realized), confirmation_queue
(fail-closed expiry checked at propose/approve/consume), jupiter_executor
(kill→flags→caps→idempotency→decimals→breaker→confirm→quote→sign→send),
scripts/confirm_trade.py operator CLI (list/approve/deny/kill/resume).
Swap endpoint POST lite-api.jup.ag/swap/v1/swap VERIFIED LIVE 2026-08-23
(422 deserialization naming quoteResponse; bogus sibling route 404; GET 405).
Quote URL/mint imported from backend/config.py — single source of truth.
Unit math imported from data_providers.jupiter (identity-tested). The stale
"9-decimals assumption" docstring in data_providers/jupiter.py is FIXED.

## Before ANY mainnet use (hard requirement)
Zero live-network coverage: run the FULL flow on Solana devnet with a
throwaway keypair first (propose → approve → execute → on-chain confirm).
Install solders when going real: .venv/bin/python -m pip install solders.

## Watch-outs
- websockets lib needed by new_listings (ships with uvicorn[standard] ✓)
- New-listing WS may require a Birdeye plan with BDS feeds; free key →
  lens auto-disables, trending-only continues (by design)
- ⚠ TERMINAL: login shell is FISH — wrap multi-command bash in a script file;
  `bash -c '...'` quoting breaks silently. Run scripts, read outputs from files.
- ⚠ pytest from repo ROOT previously failed 19 async tests (asyncio config
  lives in backend/pytest.ini); fixed by adding a root pytest.ini with
  asyncio_mode=auto. Both invocation styles now pass.
- live_execution/state/ is gitignored runtime state (confirmations, ledger,
  kill switch). Never commit it.

