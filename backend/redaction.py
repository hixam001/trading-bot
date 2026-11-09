"""
backend/redaction.py — one place for secret-shaped string scrubbing.

Every outbound/logging surface (logging filter, alert payloads, error bodies,
security monitor) delegates here so a new provider token shape is covered
everywhere at once. The patterns are intentionally shape-based: they must
catch material this process has never seen (a key pasted into an exception,
a URL echoed by httpx, a traceback repr of a keypair object).
"""
from __future__ import annotations

import re
from typing import Iterable

from public_registry import is_public_value

# Base58 alphabet (no 0, O, I, l). A secret key and public signature have the
# same shape. Unknown values stay masked; only exact registered signatures
# may pass the raw shape rule, after known secrets have already been removed.
_BASE58 = r"[1-9A-HJ-NP-Za-km-z]{80,90}"
_BASE58_KEYPAIR_RE = re.compile(
    rf"(?i)\b(?:keypair|private[\s_-]?key|secret[\s_-]?key|wallet[\s_-]?key)"
    rf"([^\n]{{0,24}}?)({_BASE58})"
)
# Any 80-90 char base58 run is keypair/signature sized; masking it is cheaper
# than risking a private key that arrived without a context word.
_RAW_BASE58_RE = re.compile(rf"\b{_BASE58}\b")
# A raw 64-int JSON array (the canonical Solana keypair file format).
_SOLANA_KEYPAIR_RE = re.compile(r"\[\s*\d+(?:\s*,\s*\d+){40,}\s*\]")
_PEM_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    re.DOTALL,
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.~+/]+=*")
_BASIC_AUTH_URL_RE = re.compile(r"(https?://)([^:\s/]+):([^@\s/]+)@")
_JSON_SENSITIVE_RE = re.compile(
    r"""(?i)(['"](?:api[_-]?key|apikey|token|secret|password|authorization|"""
    r"""refresh[_-]?token|privy[_-]?token|keypair)['"]\s*:\s*)['"]([^'"]+)['"]"""
)
# Query parameters that carry credentials, including the hyphen spelling used
# by Helius/Supabase-style RPC URLs (`?api-key=`).
_SENSITIVE_PARAM_RE = re.compile(
    r"""(?i)((?:[?&]|\b)(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|"""
    r"""refresh[_-]?token)=)[^&#\s,;&"']+"""
)
_GENERIC_QUERY_PARAM_RE = re.compile(
    r"""(?i)([?&](?:token|secret|password|key)=)[^&#\s,;&"']+"""
)
_CREDENTIAL_HEADER_RE = re.compile(
    r"(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)([^\r\n]*)"
)
_HEADER_PARAM_RE = re.compile(
    r"""(?i)(\b(?:token|secret|password|key)=)[^&#\s,;&"']+"""
)
# Credentials embedded in a URL path: https://rpc.example.com/v2/<key>,
# https://mainnet.helius-rpc.com/?api-key=... (query handled above), etc.
_URL_PATH_KEY_RE = re.compile(
    r"""(?i)(https?://[^\s/]+/(?:v\d+|rpc|api|key|token)/)([A-Za-z0-9_\-]{16,})"""
)
# Provider token shapes we have seen in this repo's integrations. These catch
# tokens even when the exact value was never registered in config.
_PROVIDER_TOKEN_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),          # OpenAI/OpenRouter style
    re.compile(r"\bgsk_[A-Za-z0-9_-]{20,}\b"),         # Groq
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),     # GitHub
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),   # Slack
    re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}\b"),       # Telegram bot token
    re.compile(r"\btk_[A-Za-z0-9_-]{16,}\b"),          # ntfy access token
)


def _config_secrets() -> list[str]:
    """Configured secret values, imported lazily (config imports this module)."""
    try:
        import config
    except Exception:
        return []
    out: list[str] = []
    for attr in dir(config):
        if attr.startswith("_"):
            continue
        if any(attr.endswith(s) for s in
               ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "DATABASE_URL",
                "DB_URL", "RPC_URL", "RPC_URL_2")):
            val = getattr(config, attr, None)
            if isinstance(val, str) and len(val.strip()) >= 6:
                out.append(val.strip())
    return out


def _runtime_secrets() -> list[str]:
    try:
        from secret_registry import get_runtime_secrets
        return [s for s in get_runtime_secrets() if s and len(s) >= 6]
    except Exception:
        return []


def redact_text(
    text: str,
    extra_secrets: Iterable[str] | None = None,
    secret_placeholder: str = "<REDACTED>",
) -> str:
    """Return *text* with every known/secret-shaped value masked.

    Never raises: redaction failure must not crash logging or an error
    handler. Unknown shapes pass through only if they do not match any of
    the structural patterns above.

    ``secret_placeholder`` only labels *registered/configured* secret values;
    structural patterns keep their own descriptive markers. Callers choose the
    label so existing log/alert/error-response contracts stay stable.
    """
    if not isinstance(text, str):
        text = str(text)
    try:
        sanitized = text
        secrets = _config_secrets() + _runtime_secrets()
        if extra_secrets:
            secrets.extend(s for s in extra_secrets if s and len(s) >= 6)
        for value in sorted(set(secrets), key=len, reverse=True):
            sanitized = sanitized.replace(value, secret_placeholder)

        sanitized = _PEM_RE.sub("<REDACTED-PRIVATE-KEY>", sanitized)
        sanitized = _SOLANA_KEYPAIR_RE.sub("[<REDACTED-KEYPAIR>]", sanitized)
        sanitized = _BASE58_KEYPAIR_RE.sub(
            lambda m: f"{m.group(0)[:len(m.group(0)) - len(m.group(2))]}<REDACTED-KEYPAIR>",
            sanitized,
        )
        sanitized = _RAW_BASE58_RE.sub(
            lambda match: (match.group(0) if is_public_value(match.group(0))
                           else "<REDACTED-KEYPAIR>"),
            sanitized,
        )
        sanitized = _BASIC_AUTH_URL_RE.sub(r"\1***:***@", sanitized)
        sanitized = _URL_PATH_KEY_RE.sub(r"\1<REDACTED>", sanitized)
        sanitized = _JSON_SENSITIVE_RE.sub(r'\1"<REDACTED>"', sanitized)
        sanitized = _SENSITIVE_PARAM_RE.sub(r"\1<REDACTED>", sanitized)
        sanitized = _GENERIC_QUERY_PARAM_RE.sub(r"\1<REDACTED>", sanitized)
        sanitized = _CREDENTIAL_HEADER_RE.sub(
            lambda match: match.group(1) + _HEADER_PARAM_RE.sub(
                r"\1<REDACTED>", match.group(2)),
            sanitized,
        )
        # Scrub named header parameters first: otherwise "Bearer token=..."
        # can have its parameter name consumed while leaving the value behind.
        sanitized = _BEARER_RE.sub("Bearer <REDACTED>", sanitized)
        for pat in _PROVIDER_TOKEN_RES:
            sanitized = pat.sub("<REDACTED-TOKEN>", sanitized)
        return sanitized
    except Exception:
        return "[REDACTION-FAILED]"
