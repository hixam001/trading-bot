"""
live_execution/config.py — live-execution settings.

Deliberately SEPARATE from backend/config.py: the paper system's
PAPER_TRADING_ONLY=True stays hardcoded True forever and must never gate (or
be gated by) this package.

THE TWO LOAD-BEARING SAFETY DEFAULTS ARE HARDCODED — never env-settable,
never changed by code. Edited only by a human, manually, in this file:

  LIVE_TRADING_ENABLED       = False   (master arm switch)
  REQUIRE_MANUAL_CONFIRMATION = True   (every trade needs human approval)

Everything downstream fails closed when these are not exactly right. There is
deliberately NO environment-variable bypass: one stray .env line must never
be able to arm real-money execution.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# SAFETY FLAGS — HARDCODED. NEVER READ FROM ENV. NEVER CHANGED BY CODE.
# ---------------------------------------------------------------------------
LIVE_TRADING_ENABLED: bool = False
REQUIRE_MANUAL_CONFIRMATION: bool = True

# ---------------------------------------------------------------------------
# Operator infrastructure (NOT safety flags — arming still requires the
# hardcoded master switch above plus manual confirmation per trade).
# ---------------------------------------------------------------------------
# Local JSON keypair file (solders Keypair.from_json). Never commit it.
WALLET_KEYPAIR_PATH: str = os.getenv("WALLET_KEYPAIR_PATH", "")

# RPC for sendTransaction + confirmation polling. Public endpoint works but
# rate-limits hard; set SOLANA_RPC_URL to a paid RPC for real use.
RPC_URL: str = os.getenv(
    "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
)

# Mutable state (confirmations, idempotency ledger, kill switch). Gitignored;
# override the directory for isolated drills/backups if needed.
STATE_DIR: Path = Path(
    os.getenv(
        "LIVE_EXECUTION_STATE_DIR",
        str(Path(__file__).resolve().parent / "state"),
    )
)

# ---------------------------------------------------------------------------
# Jupiter endpoints.
#   QUOTE: single source of truth is backend/config.JUPITER_QUOTE_URL
#          (https://lite-api.jup.ag/swap/v1/quote); jupiter_executor imports
#          it rather than duplicating the string here.
#   SWAP : verified live on 2026-08-23 — POST with a minimal body returned
#          HTTP 422 ("Failed to deserialize ... quoteResponse: missing field
#          `inputMint`"), while a bogus sibling route returned 404 and GET on
#          this route returned 405. Route existence proven, not assumed.
# ---------------------------------------------------------------------------
JUPITER_SWAP_URL: str = "https://lite-api.jup.ag/swap/v1/swap"

# ---------------------------------------------------------------------------
# Hard risk limits — all fail closed. Mirrors the paper side's philosophy of
# hardcoded, non-env-configurable risk numbers.
# ---------------------------------------------------------------------------
MAX_TRADE_USD: float = 50.0        # per-trade ceiling
MAX_OPEN_POSITIONS: int = 3        # distinct concurrent mints
MAX_TOTAL_EXPOSURE_USD: float = 150.0   # sum of open cost basis (matches the
                                   # paper side's exposure_cap scale)

# Automatic daily-loss circuit breaker: if REALIZED losses recorded today
# reach this USD magnitude, the kill switch trips itself and refuses every
# further trade until a human clears it. Realized-only is a documented
# limitation: unrealized marks need pricing infra this package avoids.
DAILY_LOSS_BREAKER_USD: float = 75.0

SLIPPAGE_BPS: int = 50             # matches the paper-side quote param

# Manual confirmation must be consumed within this window of proposal.
# Fail-closed BOTH ways: expired-proposals cannot be approved, and approvals
# consumed after expiry are refused at consume-time too.
CONFIRM_EXPIRY_SECONDS: float = 300.0

# On-chain confirmation polling
CONFIRM_TIMEOUT_SECONDS: float = 60.0

