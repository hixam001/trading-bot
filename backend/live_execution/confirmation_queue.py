"""
live_execution/confirmation_queue.py — mandatory manual trade approval.

Flow:  propose() -> human inspects -> approve(id) -> execute path consume(id)

FAIL-CLOSED EXPIRY, CHECKED AT EVERY STAGE against an injectable clock:
  * pending past expiry            -> cannot be approved (marked expired)
  * approved but consumed late     -> consume() REFUSES and marks expired
  * unknown id / denied / already
    consumed                       -> consume() refuses
A trade only proceeds when consume() succeeds, which requires a valid
approval that existed inside its validity window at the moment of use.

State is one JSON file rewritten atomically per mutation.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

from live_execution import config
from live_execution.models import PendingConfirmation, new_id


class ConfirmationError(Exception):
    """Confirmation flow refused — nothing may execute."""


class ConfirmationQueue:
    def __init__(
        self,
        path: Path,
        now_fn: Callable[[], float] = time.time,
        expiry_seconds: float | None = None,
    ):
        self.path = Path(path)
        self.now_fn = now_fn
        self.expiry = (
            config.CONFIRM_EXPIRY_SECONDS if expiry_seconds is None
            else expiry_seconds
        )

    # -- storage ----------------------------------------------------------------
    def _load(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        try:
            return dict(json.loads(self.path.read_text()).get("confirmations", {}))
        except (ValueError, OSError) as exc:
            raise RuntimeError(
                f"confirmations file {self.path} corrupt ({exc}) — refusing "
                f"to trade until a human inspects it"
            )

    def _save(self, confirmations: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"confirmations": confirmations}, indent=2))
        os.replace(tmp, self.path)

    def _get(self, cid: str) -> PendingConfirmation:
        d = self._load().get(cid)
        if d is None:
            raise ConfirmationError(f"unknown confirmation id {cid!r}")
        return PendingConfirmation.from_json(d)

    def _put(self, pc: PendingConfirmation) -> None:
        all_c = self._load()
        all_c[pc.id] = pc.to_json()
        self._save(all_c)

    # -- flow ---------------------------------------------------------------------
    def propose(
        self, mint: str, decimals: int, usd_size: float,
        quote_snapshot: dict | None = None,
    ) -> PendingConfirmation:
        now = self.now_fn()
        pc = PendingConfirmation(
            id=new_id(),
            mint=mint,
            decimals=decimals,
            usd_size=usd_size,
            proposed_at=now,
            expires_at=now + self.expiry,
            quote_snapshot=quote_snapshot or {},
        )
        self._put(pc)
        return pc

    def _expire_if_due(self, pc: PendingConfirmation) -> bool:
        """Mark expired if past the window. Returns True if it WAS expired."""
        if pc.status in ("pending", "approved") and self.now_fn() > pc.expires_at:
            pc.status = "expired"
            self._put(pc)
            return True
        return False

    def approve(self, cid: str) -> PendingConfirmation:
        pc = self._get(cid)
        if self._expire_if_due(pc):
            raise ConfirmationError(
                f"confirmation {cid} EXPIRED before approval — propose again"
            )
        if pc.status != "pending":
            raise ConfirmationError(
                f"confirmation {cid} is {pc.status!r}, not pending — refusing"
            )
        pc.status = "approved"
        pc.approved_at = self.now_fn()
        self._put(pc)
        return pc

    def deny(self, cid: str) -> PendingConfirmation:
        pc = self._get(cid)
        if pc.status not in ("pending", "approved"):
            raise ConfirmationError(
                f"confirmation {cid} is {pc.status!r}; cannot deny"
            )
        pc.status = "denied"
        self._put(pc)
        return pc

    def consume(self, cid: str) -> PendingConfirmation:
        """
        THE gate immediately before any network call. Re-checks expiry with
        the current clock so an approval can never outlive its window.
        """
        pc = self._get(cid)
        if self._expire_if_due(pc):
            raise ConfirmationError(
                f"confirmation {cid} expired (even though earlier approved) "
                f"— fail-closed, propose again"
            )
        if pc.status != "approved":
            raise ConfirmationError(
                f"confirmation {cid} is {pc.status!r} — not consumable"
            )
        pc.status = "consumed"
        pc.consumed_at = self.now_fn()
        self._put(pc)
        return pc

    # -- views ----------------------------------------------------------------------
    def list_active(self) -> list[PendingConfirmation]:
        """Non-terminal items, expiring any that are due (side-effecting view)."""
        out: list[PendingConfirmation] = []
        for cid in sorted(self._load()):
            pc = self._get(cid)
            if self._expire_if_due(pc):
                continue
            if pc.status in ("pending", "approved"):
                out.append(pc)
        return out
