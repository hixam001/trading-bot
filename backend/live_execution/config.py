"""
live_execution/config.py — live-execution settings.

Deliberately SEPARATE from backend/config.py: the paper system's
PAPER_TRADING_ONLY=True stays hardcoded True forever and must never gate (or
be gated by) this package.

THE TWO LOAD-BEARING SAFETY FLAGS ARE HARDCODED — never env-settable,
never changed by code. Edited only by a human, manually, in this file.

CURRENT STATE (committed 2026-08-28 by explicit operator direction after the
§27 devnet drill passed 5/5 — handoff §31):

  LIVE_TRADING_ENABLED       = True    (master arm switch — ARMED)
  REQUIRE_MANUAL_CONFIRMATION = False  (autonomous flow — operator-armed)

Everything downstream still fails closed on any unreadable/invalid state, and
the remaining safety layers are untouched: kill switch, automatic daily-loss
circuit breaker, idempotency ledger, exposure/position caps, SOL reserve,
wallet identity pin, decimals guards. There is deliberately NO
environment-variable bypass: one stray .env line must never be able to arm
or disarm real-money execution.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# SAFETY FLAGS — HARDCODED. NEVER READ FROM ENV. NEVER CHANGED BY CODE.
# ---------------------------------------------------------------------------
LIVE_TRADING_ENABLED: bool = True
REQUIRE_MANUAL_CONFIRMATION: bool = False

# ---------------------------------------------------------------------------
# Operator infrastructure (NOT safety flags — arming still requires the
# hardcoded master switch above plus manual confirmation per trade).
# ---------------------------------------------------------------------------
# Wallet secret resolution (INFRASTRUCTURE, not a safety flag — arming still
# requires the hardcoded master switches above). Two channels; set exactly
# one. WALLET_KEYPAIR_PATH wins when both are set (a mounted secret file is
# the safer channel); WALLET_KEYPAIR_JSON exists for hosts that cannot mount
# files (its material is parsed in-memory, never written to disk, never
# logged). Neither set → wallet load fails closed → nothing real-money starts.
WALLET_KEYPAIR_PATH: str = os.getenv("WALLET_KEYPAIR_PATH", "")
WALLET_KEYPAIR_JSON: str = os.getenv("WALLET_KEYPAIR_JSON", "")

# RPC for sendTransaction + confirmation polling. Public endpoint works but
# rate-limits hard; set SOLANA_RPC_URL to a paid RPC for real use.
RPC_URL: str = os.getenv(
    "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
)

# Mutable state (confirmations, idempotency ledger, kill switch). Gitignored;
# override the directory for isolated drills/backups if needed. An EMPTY env
# value must fall back to the default (Path("") = cwd once put the live
# CommitLedger at the repo root — exactly the accident this prevents).
STATE_DIR: Path = Path(
    os.getenv("LIVE_EXECUTION_STATE_DIR")
    or str(Path(__file__).resolve().parent / "state")
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



# ---------------------------------------------------------------------------
# reference-parity execution plumbing (2026-08-25).
# ---------------------------------------------------------------------------
# Rotating RPC list: first configured endpoint wins, then public fallbacks
# (the reference solana.server.ts parity). Public endpoints rate-limit hard; set
# SOLANA_RPC_URL to a paid RPC before arming.
RPC_URLS: list = [
    u for u in (
        os.getenv("SOLANA_RPC_URL", ""),
        "https://api.mainnet-beta.solana.com",
        "https://solana-rpc.publicnode.com",
    ) if u
]

# Fail-closed identity check (the reference keys.server.ts parity): when set, the
# loaded keypair MUST derive this exact pubkey or loading refuses loudly.
EXPECTED_WALLET_ADDRESS: str = os.getenv("EXPECTED_WALLET_ADDRESS", "")

# Quote guards (the reference EXECUTION_LIMITS parity).
MAX_PRICE_IMPACT_PCT: float = 2.5      # block quotes above this impact

# SOL reserve floor — fee-budget protection (REF-R11 micro-bootstrap, handoff
# §26). The live wallet is operator-funded with a small SOL fee reserve
# (0.03 SOL at bootstrap) plus separate USDC trading capital; a floor sized
# for a large wallet would brick a micro wallet after one order's fees.
# Operator-tunable via env because it is infrastructure, NOT a safety flag —
# arming still requires the hardcoded switches above. Fail-closed as ever:
# at or below the floor every order blocks BEFORE any network call.
MIN_SOL_RESERVE: float = float(os.getenv("SOLANA_MIN_SOL_RESERVE", "0.01"))

# Minimum live ticket — EQUITY-PROPORTIONAL (§45, operator decision 2026-08-30).
# The old fixed $0.50 (MIN_LIVE_TICKET_USD below, kept for historical
# reference only) froze every entry on a live book whose cash dipped under
# ~$3.33: with SIZING_MODE="fixed" the ticket is cash * 0.15, and 0.496 < 0.50
# refuses the order — the "site says ENTER but nothing executes" incident.
# A fixed floor cannot adapt to book size, so the floor now scales with the
# book: whatever the equity is, there is a formula for the minimum ticket.
#
#     min_live_ticket(equity) = max(ABS_FLOOR_USD, equity * EQUITY_FRACTION)
#
#   * equity is AT-COST equity (cash + sum of open position cost) — never
#     price-dependent, never raises, always available from the ledger;
#   * equity <= 0 / unreadable fails closed to the dust floor (a dead book
#     cannot authorise size, but a $0.10 order is still deliberately possible
#     only while cash covers it — the cash gate checks that separately);
#   * the cash_available gate rule AND the sizing refusal AND the
#     risk_budget floor threading all use this ONE formula, so the gate and
#     sizing can never disagree about the threshold again.
#
# Operator chose 10% (the most conservative of the ratios considered): a
# position below a tenth of the book is not worth placing, and once more than
# ~1/3 of the book is deployed, new entries pause until cash recovers.
# Hardcoded like the other risk numbers in this file — never env-settable.
MIN_LIVE_TICKET_EQUITY_FRACTION: float = 0.10
MIN_LIVE_TICKET_ABS_FLOOR_USD: float = 0.10

# Legacy constant (handoff §26 micro-bootstrap). SUPERSEDED by the formula
# above as the operative floor — kept so historical docs/tests citing $0.50
# remain readable. Nothing reads this in the trade path any more.
MIN_LIVE_TICKET_USD: float = 0.5


def min_live_ticket_usd(equity_usd) -> float:
    """
    The §45 live minimum ticket: max(dust floor, at-cost equity * fraction).

    Pure arithmetic over one public number. Malformed input (None / NaN /
    inf / negative) fails closed to the dust floor — never raises, never
    guesses. The model decides WHETHER to enter; this only bounds the size.
    """
    try:
        eq = float(equity_usd)
    except (TypeError, ValueError):
        return MIN_LIVE_TICKET_ABS_FLOOR_USD
    if eq != eq or eq in (float("inf"), float("-inf")) or eq <= 0.0:
        return MIN_LIVE_TICKET_ABS_FLOOR_USD
    return round(max(MIN_LIVE_TICKET_ABS_FLOOR_USD,
                     eq * MIN_LIVE_TICKET_EQUITY_FRACTION), 4)

# Rolling UTC-day notional cap on NEW deployments (the reference maxDailyUsd parity,
# scaled to this book). Checked against the execution ledger.
MAX_DAILY_DEPLOY_USD: float = 300.0


# ---------------------------------------------------------------------------
# DEVNET DRILL (P0-1): exercises wallet load, address verify, chain reads,
# build/sign/send/confirm - WITHOUT Jupiter and WITHOUT tokens. Safe by
# construction: devnet endpoint only. Run via run_live_cycle.py --drill.
# ---------------------------------------------------------------------------
DRILL_RPC_URL: str = os.getenv(
    "DRILL_RPC_URL", "https://api.devnet.solana.com"
)
DRILL_TRANSFER_LAMPORTS: int = int(os.getenv("DRILL_TRANSFER_LAMPORTS", "100000"))  # 0.0001 SOL
