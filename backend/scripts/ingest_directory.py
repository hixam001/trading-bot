"""
scripts/ingest_directory.py — bulk knowledge ingestion CLI (F3).

Usage:
    .venv/bin/python scripts/ingest_directory.py path/to/dir [more_dirs...]

Ingests .md/.txt/.json files; skips empties; prints one line per file.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge_base.loader import ingest_file  # noqa: E402

SUPPORTED = {".md", ".txt", ".json"}


async def main(paths: list[str]) -> None:
    ok = skipped = failed = 0
    for raw in paths:
        root = Path(raw)
        files = sorted(root.rglob("*")) if root.is_dir() else [root]
        for f in files:
            if not f.is_file() or f.suffix.lower() not in SUPPORTED:
                continue
            try:
                content = f.read_text(encoding="utf-8")
                if not content.strip():
                    print(f"SKIP (empty): {f}")
                    skipped += 1
                    continue
                result = await ingest_file(f.name, content)
                print(f"OK: {f} -> {result['filename']} "
                      f"(digest {len(result['digest'])} chars)")
                ok += 1
            except Exception as exc:
                print(f"FAILED: {f}: {exc}")
                failed += 1
    print(f"\nDone: {ok} ingested, {skipped} skipped, {failed} failed")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    asyncio.run(main(sys.argv[1:]))
