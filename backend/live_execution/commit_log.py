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
from live_execution.state_validation import finite_number
from live_execution.state_lock import serialized_write
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
            data = json.loads(self.path.read_text())
        except (ValueError, OSError) as exc:
            raise RuntimeError(
                f"commit log at {self.path} is unreadable ({exc}) — refusing "
                f"to seal or overwrite its history"
            ) from exc
        if not isinstance(data, list):
            raise RuntimeError(
                f"commit log at {self.path} is malformed (expected a list, "
                f"got {type(data).__name__}) — refusing to overwrite its history"
            )
        validate_commits(data)
        return data

    def _save(self, commits) -> None:
        validate_commits(commits)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(commits, indent=2))
        os.replace(tmp, self.path)

    @serialized_write
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

    @serialized_write
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

    @serialized_write
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

    @serialized_write
    def bind(self, digest: str, signature: str,
             allow_failed: bool = False) -> bool:
        """Attach the confirmed fill signature to the sealed intent.

        Normal armed flow binds from "published" (memo first, always); the
        "sealed" path is kept so a local-only seal can still be bound.

        §90-C3: `allow_failed=True` is reserved for chain-proven adoption of a
        commit that was marked failed prematurely (expiry/orphan healing). It
        clears the stale fail reason; the default still refuses failed rows."""
        commits = self._load()
        allowed = ("sealed", "published")
        if allow_failed:
            allowed += ("failed",)
        for rec in commits:
            if rec.get("hash") == digest and rec.get("status") in allowed:
                was_failed = rec.get("status") == "failed"
                rec["signature"] = signature
                rec["status"] = "bound"
                if was_failed:
                    rec.pop("fail_reason", None)
                self._save(commits)
                return True
        return False

    @serialized_write
    def note(self, digest: str, text: str) -> bool:
        """Attach an informational audit note to a commit record (§79 Phase B ladder)."""
        commits = self._load()
        for rec in commits:
            if rec.get("hash") == digest:
                notes = rec.setdefault("notes", [])
                notes.append({"text": text, "at": self.now_fn()})
                self._save(commits)
                return True
        return False

    def recent(self, limit: int = 25) -> list:
        commits = self._load()
        return list(reversed(commits[-limit:]))

    @serialized_write
    def record_sell_attempt(self, digest: str, signature: str, blockhash: str,
                            terms: dict) -> None:
        """Durably remember a signed exit BEFORE it can be sent.

        This marker is independent of the public commit status: a confirmation
        can bind the commit before the accounting write completes.
        """
        commits = self._load()
        for rec in commits:
            if rec.get("hash") == digest:
                previous = rec.get("sell_attempt")
                if previous and previous.get("status") == "pending":
                    if previous.get("signature") != signature:
                        raise RuntimeError("unresolved sell attempt cannot be replaced")
                    return
                if previous:
                    # Preserve failed signed attempts when a conclusive retry
                    # replaces the active marker; audit history is append-only.
                    rec.setdefault("sell_attempt_history", []).append(previous)
                rec["sell_attempt"] = dict(terms, signature=signature,
                                            blockhash=blockhash, status="pending")
                self._save(commits)
                return
        raise RuntimeError("sell commit missing before broadcast")

    @serialized_write
    def settle_sell_attempt(self, digest: str, signature: str, status: str) -> None:
        if status not in ("confirmed", "failed"):
            raise ValueError("invalid sell settlement")
        commits = self._load()
        for rec in commits:
            attempt = rec.get("sell_attempt")
            if rec.get("hash") == digest and attempt and attempt.get("signature") == signature:
                attempt["status"] = status
                self._save(commits)
                return
        raise RuntimeError("sell attempt missing at settlement")

    def pending_sells(self, mint: str) -> list[dict]:
        return [rec for rec in self._load()
                if rec.get("kind") == "sell"
                and rec.get("payload", {}).get("mint") == mint
                and rec.get("sell_attempt", {}).get("status") == "pending"]

    def all(self) -> list:
        return self._load()

    @serialized_write
    def reconcile_orphaned(self, max_age_seconds: float = 600.0) -> int:
        """Heal commits stuck at "published" that never filled (item 3).

        A commit whose memo is on chain but which has no bound fill is
        ambiguous on the proof surface (binding.json shows it "unbound").
        Once it is older than max_age_seconds the fill attempt is long
        resolved: a real fill would have been bound within seconds. So any
        still-"published" row that old definitively did NOT fill and is
        marked failed with an honest reason (reusing the existing failed
        status so no UI/proof route needs a new state). Idempotent and cheap;
        intended to run once per cycle at startup. Returns rows healed.
        """
        now = self.now_fn()
        commits = self._load()
        healed = 0
        for rec in commits:
            if rec.get("status") != "published":
                continue
            if rec.get("sell_attempt", {}).get("status") == "pending":
                continue  # Age alone never proves a signed exit did not land.
            published_at = (rec.get("memo_published_at")
                            or rec.get("sealed_at") or 0.0)
            if now - float(published_at) < max_age_seconds:
                continue  # too fresh — a fill may still be in flight
            rec["status"] = "failed"
            rec["fail_reason"] = ("memo published but no fill followed "
                                  "(orphan reconciled)")
            healed += 1
        if healed:
            self._save(commits)
        return healed


def validate_commits(commits):
    for index, row in enumerate(commits):
        bad = f"commit log record {index} invalid; preserve history and repair explicitly"
        if not isinstance(row, dict) or row.get("kind") not in ("buy", "sell") or row.get("status") not in ("sealed", "published", "bound", "failed"):
            raise RuntimeError(bad)
        for field in ("hash", "nonce"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise RuntimeError(bad + ": " + field)
        if not isinstance(row.get("payload"), dict):
            raise RuntimeError(bad + ": payload")
        for field in ("sealed_at", "memo_published_at"):
            value = row.get(field)
            if value is not None and (not finite_number(value) or value < 0):
                raise RuntimeError(bad + ": " + field)
        attempt = row.get("sell_attempt")
        if attempt is not None:
            if row["kind"] != "sell" or not isinstance(row["payload"].get("mint"), str) or not row["payload"]["mint"].strip():
                raise RuntimeError(bad + ": signed sell mint identity")
            if not isinstance(attempt, dict) or attempt.get("status") not in ("pending", "confirmed", "failed") or not isinstance(attempt.get("signature"), str) or not attempt["signature"]:
                raise RuntimeError(bad + ": sell_attempt")
            if not isinstance(attempt.get("blockhash"), str):
                raise RuntimeError(bad + ": sell blockhash")
            for field in ("fraction", "tokens", "proceeds_usd"):
                value = attempt.get(field)
                if not finite_number(value) or value < 0:
                    raise RuntimeError(bad + ": sell_attempt." + field)
            if not 0 < attempt["fraction"] <= 1 or not isinstance(attempt.get("wallet"), str) or not attempt["wallet"] or not isinstance(attempt.get("full_close"), bool):
                raise RuntimeError(bad + ": sell terms")
