#!/usr/bin/env python3
"""
scripts/refresh_dex_labels.py — Operator utility to refresh approved DEX labels from Jupiter API (§79 Phase B).

Fetches the live program-id-to-label map from Jupiter, cross-references against
APPROVED_SWAP_PROGRAM_IDS in config.py, and writes backend/live_execution/dex_labels.json.
This enables Jupiter quotes to restrict routing exclusively to approved AMM/DEX programs.

Usage:
    python scripts/refresh_dex_labels.py
"""
import datetime
import json
import sys
from pathlib import Path

# Add backend to sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import httpx
from live_execution import config

JUPITER_PROGRAM_ID_TO_LABEL_URL = "https://quote-api.jup.ag/v6/program-id-to-label"
OUTPUT_FILE = Path(__file__).resolve().parent.parent / "backend" / "live_execution" / "dex_labels.json"


def refresh_labels() -> dict:
    print(f"Fetching program-id-to-label from {JUPITER_PROGRAM_ID_TO_LABEL_URL}...")
    try:
        resp = httpx.get(JUPITER_PROGRAM_ID_TO_LABEL_URL, timeout=15.0)
        resp.raise_for_status()
        id_to_label = resp.json()
    except Exception as exc:
        print(f"Error fetching DEX labels: {exc}", file=sys.stderr)
        sys.exit(1)

    matched_labels = set()
    unmapped_ids = []

    for prog_id in config.APPROVED_SWAP_PROGRAM_IDS:
        label = id_to_label.get(prog_id)
        if label:
            matched_labels.add(label)
        else:
            unmapped_ids.append(prog_id)

    sorted_labels = sorted(matched_labels)
    result = {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "labels": sorted_labels,
        "unmapped_ids": sorted(unmapped_ids),
    }

    OUTPUT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Successfully mapped {len(sorted_labels)} labels from {len(config.APPROVED_SWAP_PROGRAM_IDS)} approved programs.")
    print(f"Active labels: {', '.join(sorted_labels)}")
    print(f"Wrote {OUTPUT_FILE}")
    return result


if __name__ == "__main__":
    refresh_labels()

