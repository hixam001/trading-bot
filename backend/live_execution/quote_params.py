"""
backend/live_execution/quote_params.py — Unified Jupiter quote parameter builder (§79 Phase B, §81 Phase D).

Guarantees consistency across buy, sell, and diagnostic quote requests.
Includes asLegacyTransaction=true, swapMode=ExactIn.
Buys include 'dexes' filter when labels exist; sells NEVER send 'dexes' to ensure
unrestricted emergency/crash exit execution.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from live_execution import config

log = logging.getLogger(__name__)

_DEX_LABELS_PATH = Path(__file__).resolve().parent / "dex_labels.json"
_ACTIVE_DEX_LABELS: list[str] = []

try:
    if _DEX_LABELS_PATH.is_file():
        _data = json.loads(_DEX_LABELS_PATH.read_text(encoding="utf-8"))
        _ACTIVE_DEX_LABELS = _data.get("labels", [])
        if _ACTIVE_DEX_LABELS:
            log.info("Loaded %d approved DEX labels for Jupiter quote filtering", len(_ACTIVE_DEX_LABELS))
        else:
            log.warning("dex filter inactive: run scripts/refresh_dex_labels.py")
    else:
        log.warning("dex filter inactive: run scripts/refresh_dex_labels.py")
except Exception as _exc:
    log.warning("Failed loading dex_labels.json: %s", _exc)
    log.warning("dex filter inactive: run scripts/refresh_dex_labels.py")


def get_active_dex_labels() -> list[str]:
    return list(_ACTIVE_DEX_LABELS)


def build_quote_params(
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    slippage_bps: int,
    side: str = "buy",
    dex_labels: Optional[list[str]] = None,
) -> dict[str, str]:
    """
    Build Jupiter /quote query parameters.
    Ensures identical parameters across buy, sell, and inspection paths.
    Buys apply 'dexes' filter when labels exist; sells never send 'dexes' (§81 Phase D-4).
    """
    params: dict[str, str] = {
        "inputMint": str(input_mint),
        "outputMint": str(output_mint),
        "amount": str(amount_raw),
        "slippageBps": str(int(slippage_bps)),
        "asLegacyTransaction": "true",
        "swapMode": "ExactIn",
    }
    if side.lower() == "buy":
        labels = dex_labels if dex_labels is not None else _ACTIVE_DEX_LABELS
        if labels:
            params["dexes"] = ",".join(labels)
    # Sells NEVER send dexes parameter so exit/crash liquidations are never blocked
    return params
