#!/usr/bin/env python3
"""
scripts/ingest_directory.py — Bulk-ingest .md/.txt files into the knowledge base.

Usage:
    python scripts/ingest_directory.py /path/to/your/material
    python scripts/ingest_directory.py /path/to/material --no-digest
    python scripts/ingest_directory.py /path/to/material --re-digest

Options:
    --no-digest     Skip LLM summarization (raw file stored only).
                    Useful when Ollama is not running at ingest time.
                    Digests can be generated later by re-running without this flag.
    --re-digest     Regenerate digests for files that already have them.
                    By default, existing digests are left unchanged.

Run from the backend/ directory:
    cd backend && python scripts/ingest_directory.py /path/to/material
"""
from __future__ import annotations

import asyncio
import sys
import os
from pathlib import Path

# Ensure backend/ is on the path so we can import our modules
_backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(_backend_dir))

import knowledge_base


def _parse_args() -> tuple[Path, bool, bool]:
    """Return (source_dir, no_digest, re_digest)."""
    args = sys.argv[1:]
    no_digest = "--no-digest" in args
    re_digest = "--re-digest" in args
    positional = [a for a in args if not a.startswith("--")]

    if len(positional) != 1:
        print("Usage: python scripts/ingest_directory.py <directory> [--no-digest] [--re-digest]")
        sys.exit(1)

    source = Path(positional[0])
    if not source.is_dir():
        print(f"Error: not a directory: {positional[0]}")
        sys.exit(1)

    return source, no_digest, re_digest


async def main() -> None:
    source, no_digest, re_digest = _parse_args()

    files = sorted(source.glob("*.md")) + sorted(source.glob("*.txt"))
    if not files:
        print(f"No .md/.txt files found in {source}")
        sys.exit(1)

    print(f"Found {len(files)} file(s) in {source}")
    if no_digest:
        print("  [--no-digest] LLM summarization disabled — raw files only.")
    print()

    ingested, skipped, digest_ok, digest_failed = 0, 0, 0, 0

    for fpath in files:
        try:
            content = fpath.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"  ERROR reading {fpath.name}: {exc}")
            skipped += 1
            continue

        try:
            dest = knowledge_base.ingest_file(fpath.name, content)
            ingested += 1
            print(f"  ingested: {fpath.name} -> {dest.name}")
        except ValueError as exc:
            print(f"  skipped  {fpath.name}: {exc}")
            skipped += 1
            continue

        # Attempt to generate a digest unless suppressed
        if no_digest:
            continue

        digest_path = dest.parent / (dest.stem + ".digest.txt")
        if digest_path.exists() and not re_digest:
            print(f"           digest exists, skipping (use --re-digest to regenerate)")
            digest_ok += 1
            continue

        print(f"           generating digest via Ollama...", end="", flush=True)
        digest = await knowledge_base.generate_digest(fpath.name, content)
        if digest:
            digest_path.write_text(digest, encoding="utf-8")
            print(f" done ({len(digest)} chars)")
            print(f"           digest preview: {digest[:120].strip()}...")
            digest_ok += 1
        else:
            print(" FAILED (Ollama unavailable or timed out — raw file kept)")
            digest_failed += 1

    print()
    print(f"Done: {ingested} ingested, {skipped} skipped.", end="")
    if not no_digest:
        print(f" Digests: {digest_ok} ok, {digest_failed} failed.", end="")
    print()

    if digest_failed > 0:
        print()
        print("Tip: Re-run without --no-digest once Ollama is running to generate missing digests.")
        print("     Or add --re-digest to regenerate all digests.")


if __name__ == "__main__":
    asyncio.run(main())
