"""
live_execution/config.py — live-execution settings.

Deliberately SEPARATE from backend/config.py: the paper system's
PAPER_TRADING_ONLY=True stays hardcoded True forever and must never gate (or
be gated by) this package. Enabling real execution is an explicit,
independent, operator-only decision made here.
"""
from __future__ import annotations

import os

# Master switch — default OFF. Real money moves only when this is
# explicitly "1" AND a wallet keypair path is configured AND the trade
# passes its own fail-closed preconditions.
EXECUTION_ENABLED: bool = os.getenv("LIVE_EXECUTION_ENABLED", "0") == "1"

# Local JSON keypair file (solders Keypair.from_json). Never commit it.
WALLET_KEYPAIR_PATH: str = os.getenv("WALLET_KEYPAIR_PATH", "")

# RPC for sendTransaction + confirmation polling. Public endpoint works but
# rate-limits hard; set SOLANA_RPC_URL to a paid RPC for real use.
RPC_URL: str = os.getenv(
    "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
)

# Jupiter swap endpoint (lite-api = current free tier; v6 quote-api sunset).
JUPITER_SWAP_URL: str = "https://lite-api.jup.ag/swap/v1/swap"

# Per-trade ceiling, hardcoded like the paper side's SLIPPAGE_PCT/FEE_PCT:
# changing it mid-session would corrupt comparability of the track record.
MAX_TRADE_USD: float = 50.0
SLIPPAGE_BPS: int = 50          # matches the paper-side quote param

# Confirmation polling
CONFIRM_TIMEOUT_SECONDS: float = 60.0
