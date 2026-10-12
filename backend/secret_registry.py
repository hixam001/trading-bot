"""
backend/secret_registry.py — centralized dynamic runtime secret registration (§77 E1, §78 F2).

Allows runtime subsystems (wallet keypair loader, Privy OAuth refresher, session manager)
to register sensitive secrets for log redaction without creating circular import dependencies.
"""
from __future__ import annotations

import logging

_RUNTIME_SECRETS: set[str] = set()


def register_secret(value: str) -> None:
    """Register a runtime secret string for logging and error response redaction."""
    if not value or not isinstance(value, str):
        return
    s = value.strip()
    if len(s) >= 6 and s not in _RUNTIME_SECRETS:
        _RUNTIME_SECRETS.add(s)
        # Notify active filters to compile new regex patterns
        for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "httpx", "api", "security"):
            lg = logging.getLogger(name)
            for f in lg.filters:
                if hasattr(f, "_refresh_secrets"):
                    f._refresh_secrets()


def is_secret_registered(value: str) -> bool:
    """Return True if the exact secret string is registered."""
    if not value or not isinstance(value, str):
        return False
    return value.strip() in _RUNTIME_SECRETS


def get_runtime_secrets() -> set[str]:
    """Return a copy of all currently registered runtime secrets."""
    return set(_RUNTIME_SECRETS)


def clear_runtime_secrets() -> None:
    """Clear all registered runtime secrets (testing utility)."""
    _RUNTIME_SECRETS.clear()
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "httpx", "api", "security"):
        lg = logging.getLogger(name)
        for f in lg.filters:
            if hasattr(f, "_refresh_secrets"):
                f._refresh_secrets()

