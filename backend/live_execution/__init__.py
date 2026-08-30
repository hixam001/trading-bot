"""
live_execution — REAL-MONEY execution package. Strictly separated from the
paper-trading pipeline (backend/).

Isolation contract (enforced by verify_tasks.sh grep):
  - Nothing under backend/ may import or mention this package.
  - This package never auto-triggers from signals/tick loop/API; it is
    operator-invoked only, via jupiter_executor's CLI and
    scripts/confirm_trade.py.
  - It runs on its OWN hardcoded enable-flag (LIVE_TRADING_ENABLED=False,
    edited only by a human in config.py), independent of the paper system's
    PAPER_TRADING_ONLY (which stays hardcoded True).

Safety model, all fail-closed:
  hard per-trade/exposure/position-count caps · mandatory manual confirmation
  with fail-closed expiry · persistent kill switch (+ operator CLI kill /
  resume) · automatic daily-loss circuit breaker · idempotency ledger for
  execution attempts · decimals-aware unit math imported from
  backend.data_providers.jupiter (unknown decimals refuse before ANY call).

⚠ COVERAGE: zero live-network tests exist. Solana RPC and Jupiter swap
endpoints are not reachable/reliable from sandboxed dev environments, so
signing, sendTransaction, and confirmation are untested against real infra.
BEFORE ANY MAINNET USE: run the complete flow on Solana devnet with a
throwaway keypair first. Hard requirement, not a suggestion.
"""
