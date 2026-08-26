"""
live_execution/wallet.py — local keypair loading for signing.

The secret key NEVER leaves this process except into the signing call, and is
never logged. Loading fails closed: missing file, unreadable JSON, or a
missing `solders` install are all refusals before any trade flow continues.
"""
from __future__ import annotations

import json
from pathlib import Path

from live_execution import config


class WalletError(Exception):
    """Wallet could not be loaded — nothing was signed or sent."""


def load_keypair(path: str | None = None):
    """
    Load the operator's ed25519 keypair from a Solana JSON byte-array file.
    Returns a solders Keypair; raises WalletError on any problem.
    Lazy-imports solders so the rest of the package imports cleanly without.
    """
    kp_path = Path(path or config.WALLET_KEYPAIR_PATH)
    if not kp_path.is_file():
        raise WalletError(
            f"WALLET_KEYPAIR_PATH not set or file missing ({str(kp_path)!r})"
        )
    try:
        raw = json.loads(kp_path.read_text())
    except (ValueError, OSError) as exc:
        raise WalletError(f"keypair file unreadable: {exc}") from exc
    if not isinstance(raw, list) or not all(
        isinstance(b, int) and 0 <= b <= 255 for b in raw
    ):
        raise WalletError(
            "keypair file is not a Solana JSON byte array — refusing"
        )

    try:
        from solders.keypair import Keypair  # type: ignore
    except ImportError as exc:
        raise WalletError(
            "solders is not installed — run: pip install solders"
        ) from exc
    try:
        return Keypair.from_json(str(kp_path))
    except Exception as exc:  # solders raises assorted types
        raise WalletError(f"keypair rejected by solders: {exc}") from exc


def pubkey_string(keypair) -> str:
    """Public address for display/logging. Safe: public key only."""
    return str(keypair.pubkey())


def verify_expected_address(keypair) -> str:
    """
    Fail-closed identity check (omo keys.server.ts parity): when
    EXPECTED_WALLET_ADDRESS is set, the loaded keypair MUST derive that
    exact pubkey - otherwise refuse loudly instead of quietly trading from
    some other account. Returns the verified address.
    """
    expected = config.EXPECTED_WALLET_ADDRESS.strip()
    actual = pubkey_string(keypair)
    if expected and actual != expected:
        raise WalletError(
            f"keypair derives {actual}, which is not the expected wallet "
            f"{expected} - refusing to trade from an unpublished account"
        )
    return actual
