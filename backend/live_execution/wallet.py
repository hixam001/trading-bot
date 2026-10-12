"""
live_execution/wallet.py — local keypair loading for signing.

The secret key NEVER leaves this process except into the signing call, and is
never logged. Loading fails closed: missing/invalid material, unreadable
JSON, or a missing `solders` install are all refusals before any trade flow
continues.

Secret resolution (deployment parity — INFRASTRUCTURE, not a safety flag;
arming is still only possible via the hardcoded flags in config.py):
  1. explicit `path` argument            — caller-provided file (tests/CLIs)
  2. WALLET_KEYPAIR_PATH (env)           — JSON byte-array file; PREFERRED on
     a VM (chmod 600, outside the repo, bind-mounted read-only into the
     container so the material never appears in `docker inspect`).
  3. WALLET_KEYPAIR_JSON (env)           — the keypair JSON direct from the
     environment, for hosts that cannot mount secret files (PaaS). Parsed
     in-memory; NEVER written to disk, never logged.
  4. none of the above                   — WalletError; nothing signs, nothing
     sends. "No wallet configured" can never start real-money execution.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from live_execution import config


class WalletError(Exception):
    """Wallet could not be loaded — nothing was signed or sent."""


# ---------------------------------------------------------------------------
# Log redaction: the keypair material is a JSON array of 64 ints. If any log
# record ever carries it (a stray exception dump, a debug print), mask it
# before it can land in logs/backend.log. Same idempotent-filter pattern as
# data_providers/crowd.py's _ApiKeyRedactor. Installed on the httpx logger
# and the root logger; matches arrays of >=41 ints (a keypair has 64) so
# ordinary short numeric arrays are never touched.
# ---------------------------------------------------------------------------
_KP_RE = re.compile(r"\[\s*\d+(?:\s*,\s*\d+){40,}\s*\]")


class _KeypairRedactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = _KP_RE.sub("[<REDACTED-KEYPAIR>]", msg)
            if redacted != msg:
                record.msg = redacted
                record.args = None
        except Exception:  # never break logging
            pass
        return True


def _install_keypair_redactor() -> None:
    for logger_name in ("httpx", ""):
        lg = logging.getLogger(logger_name)
        if not any(isinstance(f, _KeypairRedactor) for f in lg.filters):
            lg.addFilter(_KeypairRedactor())


_install_keypair_redactor()


def _parse_material(text: str, source: str) -> list:
    """json.loads + structural validation shared by both secret channels."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise WalletError(
            f"keypair from {source} is not valid JSON: {exc}"
        ) from exc
    if (
        not isinstance(raw, list)
        or len(raw) != 64
        or not all(isinstance(b, int) and 0 <= b <= 255 for b in raw)
    ):
        raise WalletError(
            f"keypair from {source} is not a Solana JSON byte array "
            "(exactly 64 ints, each 0-255) — refusing"
        )
    return raw


def _read_file_material(kp_path: Path) -> str:
    if not kp_path.is_file():
        raise WalletError(
            f"WALLET_KEYPAIR_PATH not set or file missing ({str(kp_path)!r})"
        )
    # B3: refuse loose permissions on POSIX (must be 0600 or 0400, no group/world access)
    import os
    import stat
    if os.name == "posix":
        st_mode = kp_path.stat().st_mode
        if stat.S_IMODE(st_mode) & 0o077:
            raise WalletError(
                "keypair file permissions too open (must be 0600/0400) — refusing"
            )
    try:
        content = kp_path.read_text()
        stripped = content.strip()
        # If the file is an encrypted envelope created by secret_store, decrypt in memory
        if stripped.startswith("{") and '"enc"' in stripped:
            try:
                import secret_store
                decrypted = secret_store.decrypt_from_file(str(kp_path))
                if decrypted is None:
                    raise WalletError(
                        f"keypair file {str(kp_path)!r} is encrypted but could not be decrypted "
                        "(invalid key, missing key file, or tampered)"
                    )
                if isinstance(decrypted, list):
                    return json.dumps(decrypted)
                if isinstance(decrypted, dict):
                    for k in ("keypair", "raw", "bytes", "material"):
                        if k in decrypted and isinstance(decrypted[k], list):
                            return json.dumps(decrypted[k])
                raise WalletError(
                    f"encrypted keypair file {str(kp_path)!r} has invalid decrypted payload format"
                )
            except ImportError:
                raise WalletError("secret_store module unavailable to decrypt encrypted keypair file")
        return content
    except OSError as exc:
        raise WalletError(f"keypair file unreadable: {exc}") from exc


def load_keypair(path: str | None = None):
    """
    Load the operator's ed25519 keypair for signing. Returns a solders
    Keypair; raises WalletError on any problem. Lazy-imports solders so the
    rest of the package imports cleanly without. See the module docstring
    for the full secret-resolution order.
    """
    if path:
        raw = _parse_material(
            _read_file_material(Path(path)), f"file {str(path)!r}"
        )
    elif config.WALLET_KEYPAIR_PATH:
        raw = _parse_material(
            _read_file_material(Path(config.WALLET_KEYPAIR_PATH)),
            f"file {config.WALLET_KEYPAIR_PATH!r}",
        )
    elif config.WALLET_KEYPAIR_JSON.strip():
        raw = _parse_material(
            config.WALLET_KEYPAIR_JSON, "WALLET_KEYPAIR_JSON"
        )
    else:
        raise WalletError(
            "no wallet configured: set WALLET_KEYPAIR_PATH (preferred) "
            "or WALLET_KEYPAIR_JSON"
        )

    try:
        from solders.keypair import Keypair  # type: ignore
    except ImportError as exc:
        raise WalletError(
            "solders is not installed — run: pip install solders"
        ) from exc
    try:
        # from_bytes on the ALREADY-VALIDATED array: solders' from_json wants
        # a JSON-content string (not a path — passing the path made solders
        # parse the path itself and fail-closed with "expected value at line
        # 1 column 1"; bug found by the first real keypair load, 2026-08-28).
        # Reusing `raw` also avoids re-reading the material after validation.
        kp = Keypair.from_bytes(bytes(raw))
        try:
            from secret_registry import register_secret
            register_secret(json.dumps(raw))
            register_secret(str(kp))
            register_secret(bytes(raw).hex())
            register_secret(bytes(raw[:32]).hex())
        except Exception:
            pass
        return kp
    except Exception as exc:  # solders raises assorted types
        raise WalletError(f"keypair rejected by solders: {exc}") from exc


def pubkey_string(keypair) -> str:
    """Public address for display/logging. Safe: public key only."""
    return str(keypair.pubkey())


def verify_expected_address(keypair) -> str:
    """
    Fail-closed identity check (the reference keys.server.ts parity): when
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


def public_address() -> str | None:
    """Return the wallet's public address (Base58 string) without loading the signing key.

    Safe for internet-facing processes (FastAPI).
    Priority:
      1. config.EXPECTED_WALLET_ADDRESS if set and valid Base58 Solana pubkey.
      2. STATE_DIR/wallet_pubkey.txt written by run_live_cycle.py after verifying keypair.
      3. None (fail closed).
    """
    expected = getattr(config, "EXPECTED_WALLET_ADDRESS", "").strip()
    if expected:
        try:
            from solders.pubkey import Pubkey
            Pubkey.from_string(expected)
            return expected
        except Exception:
            pass

    pubkey_path = getattr(config, "STATE_DIR", Path(__file__).parent / "state") / "wallet_pubkey.txt"
    try:
        if pubkey_path.is_file():
            val = pubkey_path.read_text(encoding="utf-8").strip()
            if val:
                from solders.pubkey import Pubkey
                Pubkey.from_string(val)
                return val
    except Exception:
        pass

    return None


async def get_wallet_cash_usd() -> float | None:
    """Fetch wallet's live on-chain USDC cash balance (§78 F3).

    Returns float >= 0.0 when readable.
    Returns None when wallet is unreadable, pubkey is unknown, or RPC is unreachable.
    Fail-closed: never return 0.0 for unknown states (0.0 means broke, None means unknown).
    """
    try:
        from live_execution import solana
        pubkey = public_address()
        if not pubkey:
            return None
        bal = await solana.get_usdc_balance(pubkey)
        if bal is None:
            return None
        return max(bal, 0.0)
    except Exception:
        return None


