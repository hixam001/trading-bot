"""
backend/config_helpers.py — security and configuration validators (§78 F5).
"""
from __future__ import annotations


def validate_admin_token_entropy(token: str) -> bool:
    """Validate that ADMIN_TOKEN has sufficient entropy (>= 32 chars, >= 12 distinct chars)."""
    if not token or not isinstance(token, str):
        return False
    s = token.strip()
    if len(s) < 32:
        return False
    if len(set(s)) < 12:
        return False
    return True


def validate_session_secret(secret: str, admin_token: str = "") -> bool:
    """Validate a dedicated SESSION_SECRET.

    Same entropy floor as ADMIN_TOKEN, and it must not be a copy of the
    operator token: session signing and operator authentication are separate
    privileges, so one credential must never unlock both.
    """
    if not validate_admin_token_entropy(secret):
        return False
    if admin_token and secret.strip() == admin_token.strip():
        return False
    return True

