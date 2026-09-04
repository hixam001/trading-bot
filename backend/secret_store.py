"""
secret_store.py — encrypted-at-rest persistence for runtime-rotated secrets.

WHY: the fomo.fun Privy refresh token ROTATES on every session mint, so it
cannot live only in .env — the bot must persist the newest value or the
auth chain dies on the first restart (data_providers/crowd.py). Until §54
that sidecar (.fomo_privy.json) was plaintext JSON: a long-lived credential
readable by anything that can read the file. This module wraps it in Fernet
(AES-128-CBC + HMAC-SHA256, authenticated) so the secret is NEVER on disk in
a readable form.

Key resolution (either channel, in order):
  1. SECRET_STORE_KEY (env) — a Fernet key (urlsafe-base64, 32 bytes) for
     file-less hosts (PaaS). Generate one with:
       python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  2. SECRET_STORE_KEY_FILE — an auto-generated 0600 key file, created on
     first use (default <repo>/.secret_store.key, gitignored).

FAIL-SOFT by design (the sidecar contract crowd.py always had): a missing
or invalid key, an undecryptable/tampered sidecar, or an unavailable
`cryptography` install degrades to "no persisted token" — the .env
bootstrap re-seeds the chain and the crowd feed never stalls the tick. A
sidecar that cannot be encrypted is never written in plaintext instead.

All writes are atomic (tmp + os.replace, POSIX) and land at mode 0600 —
os.replace preserves the SOURCE file's mode, so even a legacy 0644
predecessor is replaced by a 0600 file.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger(__name__)

# On-disk envelope: {"v": 1, "enc": "<fernet token>"}. The explicit version
# field leaves room for a future format change without silently mis-reading
# old files.
_FORMAT_VERSION = 1


def _fernet():
    """Lazy import so the rest of the package imports without the dep."""
    from cryptography.fernet import Fernet
    return Fernet


def _write_private(path: str, text: str) -> bool:
    """Atomic 0600 write. Returns False (never raises) on OSError."""
    try:
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.error("secret_store: could not write %s: %s", path, exc)
        return False


def _resolve_key() -> Optional[bytes]:
    """
    Valid Fernet key bytes, or None when encryption is unavailable.

    Env channel first (file-less hosts): the value must already be a valid
    Fernet key — an invalid one is a config error, logged loudly, and the
    store degrades to disabled rather than silently regenerating a DIFFERENT
    key (which would orphan every existing sidecar).
    """
    if config.SECRET_STORE_KEY:
        try:
            _fernet()(config.SECRET_STORE_KEY.encode())  # validates shape
            return config.SECRET_STORE_KEY.encode()
        except ImportError:
            log.error("secret_store: SECRET_STORE_KEY is set but "
                      "'cryptography' is not installed — encrypted "
                      "sidecars disabled")
            return None
        except (ValueError, TypeError):
            log.error("secret_store: SECRET_STORE_KEY is not a valid Fernet "
                      "key (generate one: python -c \"from "
                      "cryptography.fernet import Fernet; "
                      "print(Fernet.generate_key().decode())\") — encrypted "
                      "sidecars disabled")
            return None
    # Key-file channel: load the existing key or create one on first use.
    kf = Path(config.SECRET_STORE_KEY_FILE)
    try:
        raw = kf.read_text().strip()
        if raw:
            _fernet()(raw.encode())  # validates shape
            return raw.encode()
    except FileNotFoundError:
        pass
    except ImportError:
        log.error("secret_store: 'cryptography' is not installed — "
                  "encrypted sidecars disabled")
        return None
    except (ValueError, TypeError):
        # A corrupt key file means EXISTING sidecars are already unreadable
        # under it; regenerating would orphan them silently. Fail soft.
        log.error("secret_store: key file %s is corrupt — not regenerating "
                  "(delete it to start a fresh chain); encrypted sidecars "
                  "disabled", kf)
        return None
    except OSError as exc:
        log.error("secret_store: key file %s unreadable: %s", kf, exc)
        return None
    try:
        key = _fernet().generate_key()
    except ImportError:  # pragma: no cover — same import, defensive
        return None
    if not _write_private(str(kf), key.decode() + "\n"):
        return None
    log.info("secret_store: generated new encryption key at %s (mode 600)",
             kf)
    return key


def encrypt_to_file(path: str, payload: dict) -> bool:
    """
    Persist `payload` Fernet-encrypted at `path` (atomic, 0600). Returns
    True on success; False on any failure — in which case NOTHING was
    written (the caller must never fall back to plaintext).
    """
    key = _resolve_key()
    if key is None:
        return False
    try:
        token = _fernet()(key).encrypt(
            json.dumps(payload).encode()).decode()
    except Exception as exc:  # unexpected serialization/crypto failure
        log.error("secret_store: encryption failed for %s: %s", path, exc)
        return False
    if not _write_private(
            path, json.dumps({"v": _FORMAT_VERSION, "enc": token})):
        return False
    return True


def decrypt_from_file(path: str) -> Optional[dict]:
    """
    Read + decrypt the envelope at `path`. None when the file is absent,
    unreadable, not the encrypted format, tampered, or undecryptable under
    the current key (e.g. the operator rotated keys) — callers fall back to
    their bootstrap channel.
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as exc:
        log.warning("secret_store: state file unreadable (%s): %s", path, exc)
        return None
    if not isinstance(data, dict) or "enc" not in data:
        return None
    key = _resolve_key()
    if key is None:
        return None
    try:
        plain = _fernet()(key).decrypt(str(data["enc"]).encode())
        payload = json.loads(plain)
    except Exception:
        log.warning("secret_store: %s could not be decrypted (wrong key or "
                    "tampered?) — falling back to bootstrap channels", path)
        return None
    return payload if isinstance(payload, dict) else None


def load_legacy_plaintext(path: str) -> Optional[dict]:
    """
    Detect + read a PRE-§54 plaintext sidecar ({"refresh_token": ...})
    exactly once, so the caller can migrate it to the encrypted envelope.
    Returns None for anything else (absent, encrypted format, corrupt).
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return None
    if (not isinstance(data, dict) or "enc" in data
            or "refresh_token" not in data):
        return None
    return data

