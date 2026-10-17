#!/usr/bin/env python3
"""
scripts/refresh_dex_labels.py — Operator utility to refresh approved DEX labels from Jupiter API (§79 Phase B, §81 Phase D).

Fetches the live program-id-to-label map from Jupiter, cross-references against
APPROVED_SWAP_PROGRAM_IDS in config.py, and writes backend/live_execution/dex_labels.json atomically.
This enables Jupiter quotes to restrict routing exclusively to approved AMM/DEX programs.

Usage:
    python scripts/refresh_dex_labels.py
"""
import datetime
import json
import os
import sys
from pathlib import Path

# Add backend to sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import httpx
import config as backend_config
from live_execution import config as live_config

OUTPUT_FILE = Path(__file__).resolve().parent.parent / "backend" / "live_execution" / "dex_labels.json"


def parse_label_map(data) -> dict[str, str]:
    """Parse Jupiter program-id-to-label payload in either object or list shape."""
    if isinstance(data, dict):
        res = {}
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                res[k.strip()] = v.strip()
        if not res:
            raise ValueError("Dictionary is empty or contains no valid string program-id to label mappings")
        return res
    elif isinstance(data, list):
        res = {}
        for item in data:
            if isinstance(item, dict):
                pid = item.get("programId") or item.get("id") or item.get("address")
                label = item.get("label") or item.get("name")
                if pid and label:
                    res[str(pid).strip()] = str(label).strip()
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                res[str(item[0]).strip()] = str(item[1]).strip()
        if not res:
            raise ValueError("List is empty or contains no valid program-id to label mappings")
        return res
    raise ValueError(f"Unrecognized program-id-to-label payload shape: {type(data)}")


def refresh_labels(labels_url: str | None = None, out_file: Path | None = None) -> dict:
    url = labels_url or getattr(backend_config, "JUPITER_PROGRAM_LABELS_URL", "https://lite-api.jup.ag/swap/v1/program-id-to-label")
    target_file = out_file or OUTPUT_FILE
    print(f"Fetching program-id-to-label from {url}...")
    try:
        resp = httpx.get(url, timeout=15.0)
        resp.raise_for_status()
        raw_data = resp.json()
        id_to_label = parse_label_map(raw_data)
    except Exception as exc:
        print(f"Error fetching or parsing DEX labels: {exc}", file=sys.stderr)
        sys.exit(1)

    matched_labels = set()
    unmapped_ids = []

    for prog_id in live_config.APPROVED_SWAP_PROGRAM_IDS:
        label = id_to_label.get(prog_id)
        if label:
            matched_labels.add(label)
        else:
            unmapped_ids.append(prog_id)

    sorted_labels = sorted(matched_labels)
    sorted_unmapped = sorted(unmapped_ids)
    result = {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_url": url,
        "labels": sorted_labels,
        "unmapped_ids": sorted_unmapped,
    }

    # Atomic write via temporary file
    tmp_path = target_file.with_name(f"{target_file.name}.tmp.{os.getpid()}")
    try:
        tmp_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        tmp_path.replace(target_file)
    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        print(f"Error writing labels file: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Successfully mapped {len(sorted_labels)} labels from {len(live_config.APPROVED_SWAP_PROGRAM_IDS)} approved programs.")
    print(f"Active labels: {', '.join(sorted_labels)}")
    if sorted_unmapped:
        print(f"Unmapped approved program IDs ({len(sorted_unmapped)}):")
        for uid in sorted_unmapped:
            print(f"  - {uid}")
    print(f"Wrote {target_file}")
    return result


if __name__ == "__main__":
    refresh_labels()
