"""Process-local, bounded registry for transaction signatures (§93 R4).

Only signature-producing/receiving code registers values. This permits exact
public signatures through the base58 shape scrubber; it never takes priority
over configured, runtime or caller-supplied secrets in ``redact_text``.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from threading import RLock

_SIGNATURE_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{80,90}\Z")
_MAX_PUBLIC_VALUES = 5000
_PUBLIC_VALUES: OrderedDict[str, None] = OrderedDict()
_LOCK = RLock()


def register_public_value(value: str) -> None:
    """Remember an exact base58 signature, retaining at most 5,000 values."""
    if not isinstance(value, str) or not _SIGNATURE_RE.fullmatch(value):
        return
    with _LOCK:
        _PUBLIC_VALUES[value] = None
        _PUBLIC_VALUES.move_to_end(value)
        while len(_PUBLIC_VALUES) > _MAX_PUBLIC_VALUES:
            _PUBLIC_VALUES.popitem(last=False)


def is_public_value(value: str) -> bool:
    """Check exact membership and refresh its LRU position atomically."""
    with _LOCK:
        if value not in _PUBLIC_VALUES:
            return False
        _PUBLIC_VALUES.move_to_end(value)
        return True


def clear_public_values() -> None:
    """Reset the process-local registry (test utility)."""
    with _LOCK:
        _PUBLIC_VALUES.clear()
