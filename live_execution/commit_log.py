"""live_execution/commit_log.py - local pre-broadcast intent log.

the reference precommit parity: before an order is broadcast,
sha256(nonce + | + canonical_payload) is recorded here together with the
plaintext payload, so any later claim can be recomputed and checked.

REF-R11: when armed, the same hash is ALSO written on-chain as a Solana
memo BEFORE the fill is broadcast (live_execution/memo.py). The memo
signature/slot are attached here so the local seal and the on-chain
commitment stay one auditable record:

    sealed -> published (memo confirmed on-chain) -> bound (fill confirmed)

A commit whose memo could not be published is marked "failed" with the
reason — the fill never runs (fail closed, handoff §22 requirement 4), and
the refusal stays visible in the record instead of disappearing.
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
        # REF-R11: on-chain memo fields (null until published).
        rec["memo_signature"] = None
        rec["memo_slot"] = None
        rec["memo_published_at"] = None
        commits = self._load()
        commits.append(rec)
        self._save(commits)
        return rec

    def record_memo(self, digest: str, memo_signature: str,
                    memo_slot: Optional[int]) -> bool:
        """Attach the confirmed on-chain memo (sealed -> published).

        Only a row still in "sealed" state can be published — a failed or
        already-bound commit is never relabelled."""
        commits = self._load()
        for rec in commits:
            if rec.get("hash") == digest and rec.get("status") == "sealed":
                rec["memo_signature"] = memo_signature
                rec["memo_slot"] = memo_slot
                rec["memo_published_at"] = self.now_fn()
                rec["status"] = "published"
                self._save(commits)
                return True
        return False

    def fail(self, digest: str, reason: str) -> bool:
        """Mark a commit that could not be published/executed (honest record).

        The row keeps its payload+nonce+hash so the refusal is auditable —
        a skipped trade must be as visible as an executed one."""
        commits = self._load()
        for rec in commits:
            if rec.get("hash") == digest and rec.get("status") in ("sealed", "published"):
                rec["status"] = "failed"
                rec["fail_reason"] = reason
                self._save(commits)
                return True
        return False

    def bind(self, digest: str, signature: str) -> bool:
        """Attach the confirmed fill signature to the sealed intent.

        Normal armed flow binds from "published" (memo first, always); the
        "sealed" path is kept so a local-only seal can still be bound."""
        commits = self._load()
        for rec in commits:
            if rec.get("hash") == digest and rec.get("status") in ("sealed", "published"):
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
