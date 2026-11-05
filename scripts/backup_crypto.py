"""
scripts/backup_crypto.py — versioned backup envelope (§92 S4).

v2 format:

    b"TBACKUP2\\x00" | u32 big-endian header length | JSON header |
    AES-256-GCM ciphertext

The header carries a random 16-byte scrypt salt and 12-byte GCM nonce. The
header bytes are used as associated data, so changing the KDF parameters or
salt invalidates the tag. scrypt is memory-hard: a stolen archive cannot be
brute-forced at SHA-256 speed.

Legacy archives (raw Fernet tokens) are still decrypted when the magic is
absent, so an operator can restore the last good backup after upgrading.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"TBACKUP2\x00"
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 128 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_SCRYPT_N = 2 ** 20


def derive_key(password: str, salt: bytes, n: int = SCRYPT_N,
               r: int = SCRYPT_R, p: int = SCRYPT_P) -> bytes:
    if n < 2 ** 14 or n > MAX_SCRYPT_N or n & (n - 1):
        raise ValueError("scrypt n must be a power of two between 2**14 and 2**20")
    if not (1 <= r <= 32) or not (1 <= p <= 16):
        raise ValueError("scrypt r/p out of range")
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        maxmem=SCRYPT_MAXMEM, dklen=32)


def encrypt_archive(tar_bytes: bytes, password: str) -> bytes:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    header = {
        "version": 2,
        "kdf": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }
    header_bytes = json.dumps(
        header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    prefix = MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes
    key = derive_key(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    ciphertext = AESGCM(key).encrypt(nonce, tar_bytes, prefix)
    return prefix + ciphertext


def _decrypt_v2(blob: bytes, password: str) -> bytes:
    offset = len(MAGIC)
    if len(blob) < offset + 5:
        raise ValueError("truncated v2 header")
    (length,) = struct.unpack(">I", blob[offset:offset + 4])
    if length <= 0 or length > MAX_HEADER_BYTES:
        raise ValueError("v2 header length out of range")
    header_start = offset + 4
    header_end = header_start + length
    if len(blob) <= header_end:
        raise ValueError("truncated v2 ciphertext")
    header = json.loads(blob[header_start:header_end].decode("utf-8"))
    if header.get("version") != 2 or header.get("kdf") != "scrypt":
        raise ValueError("unsupported backup format version")
    salt = base64.b64decode(header["salt"], validate=True)
    nonce = base64.b64decode(header["nonce"], validate=True)
    if len(salt) < 16 or len(nonce) != 12:
        raise ValueError("invalid salt or nonce length")
    key = derive_key(
        password, salt,
        int(header["n"]), int(header["r"]), int(header["p"]))
    prefix = blob[:header_end]
    return AESGCM(key).decrypt(nonce, blob[header_end:], prefix)


def decrypt_archive(blob: bytes, password: str) -> bytes:
    """Decrypt v2 or legacy Fernet bytes. Raises on wrong key/tamper."""
    try:
        if blob.startswith(MAGIC):
            return _decrypt_v2(blob, password)
        return Fernet(password.encode("utf-8")).decrypt(blob)
    except InvalidToken:
        raise ValueError("InvalidToken (wrong key or corrupted data)") from None
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(str(exc) or "corrupted archive") from None
