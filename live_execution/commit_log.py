"""live_execution/commit_log.py - local pre-broadcast intent log.

the reference precommit parity without the memo layer: before an order is broadcast,
sha256(nonce + | + canonical_payload) is recorded here together with the
plaintext payload, so any later claim can be recomputed and checked.
After confirmation the signature is bound to the same row.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional
import uuid


class CommitLog:
    def __init__(self, path, now_fn=time.time):
        self.path = Path(path)
        self.now_fn = now_fn

    def _load(self) -> list:
        if not self.path.is_file():
            return []
        try:
            return json.loads(self.path.read_text())
        except (ValueError, OSError):
            return []

    def _save(self, commits) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(commits, indent=2))
        os.replace(tmp, self.path)

    def seal(self, kind: str, payload: dict) -> dict:
        nonce = uuid.uuid4().hex
        canonical = json.dumps(payload, sort_keys=True)
        digest = hashlib.sha256((nonce + "|" + canonical).encode()).hexdigest()
        rec = {}
        rec["kind"] = kind
        rec["nonce"] = nonce
        rec["payload"] = payload
        rec["hash"] = digest
        rec["sealed_at"] = self.now_fn()
        rec["signature"] = None
        rec["status"] = "sealed"
        commits = self._load()
        commits.append(rec)
        self._save(commits)
        return rec

    def bind(self, digest: str, signature: str) -> bool:
        """Attach the confirmed signature to the sealed intent."""
        commits = self._load()
        for rec in commits:
            if rec.get("hash") == digest and rec.get("status") == "sealed":
                rec["signature"] = signature
                rec["status"] = "bound"
                self._save(commits)
                return True
        return False

    def recent(self, limit: int = 25) -> list:
        commits = self._load()
        return list(reversed(commits[-limit:]))

    def all(self) -> list:
        return self._load()
