"""
live_execution/jupiter_client.py — Single source of truth for Jupiter URLs and request headers (§81 Phase G).
"""
from __future__ import annotations

import os

DEFAULT_JUPITER_BASE_URL = "https://lite-api.jup.ag/swap/v1"


def get_base_url() -> str:
    """Returns the base Jupiter Swap API URL."""
    url = os.getenv("JUPITER_BASE_URL", "").strip()
    return url if url else DEFAULT_JUPITER_BASE_URL


def get_headers() -> dict[str, str]:
    """
    Returns request headers for Jupiter API calls.
    Includes x-api-key if JUPITER_API_KEY is configured.
    """
    api_key = os.getenv("JUPITER_API_KEY", "").strip()
    return {"x-api-key": api_key} if api_key else {}


def quote_url() -> str:
    """Returns the full URL for the Jupiter quote endpoint."""
    return f"{get_base_url().rstrip('/')}/quote"


def swap_url() -> str:
    """Returns the full URL for the Jupiter swap endpoint."""
    return f"{get_base_url().rstrip('/')}/swap"


def program_id_to_label_url() -> str:
    """Returns the full URL for the program-id-to-label mapping endpoint."""
    return f"{get_base_url().rstrip('/')}/program-id-to-label"

