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

