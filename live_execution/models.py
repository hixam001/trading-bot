"""
live_execution/models.py — dataclasses + the idempotency/exposure ledger.

The ledger is the package's memory:
  * idempotency  — one execution attempt per caller-supplied key; replays
                   return the original outcome instead of sending again
  * exposure     — open cost basis per mint feeds the position-count and
                   total-exposure caps
  * realized P&L — close entries feed the automatic daily-loss breaker

Storage is one human-readable JSON file rewritten atomically
(tmp file + os.replace) on every mutation. Scale is trivially small
(operator-driven trades), so correctness beats cleverness here.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional


def new_id() -> str:
    """Short opaque id for confirmations / records."""
    return uuid.uuid4().hex[:12]


@dataclass
class PendingConfirmation:
    """A proposed trade awaiting (or past) human approval."""

    id: str
    mint: str
    decimals: int
    usd_size: float
    proposed_at: float
    expires_at: float
    status: str = "pending"          # pending|approved|denied|expired|consumed
    quote_snapshot: dict = field(default_factory=dict)   # informational only
    approved_at: Optional[float] = None
    consumed_at: Optional[float] = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "PendingConfirmation":
        return cls(**d)


@dataclass
class ExecutionRecord:
    """One idempotent execution attempt (buy) or close event."""

    kind: str                        # "buy" | "close"
    idempotency_key: str             # unique for buys; closes derive their own
    mint: str
    usd_size: float                  # cost for buys; proceeds for closes
    tokens_out: float = 0.0
    price_usd: float = 0.0
    signature: str = ""              # empty until broadcast succeeds
    status: str = "recorded"         # recorded|sent|confirmed|failed|closed
    ts: float = field(default_factory=time.time)
    pnl_usd: Optional[float] = None  # closes only

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "ExecutionRecord":
        return cls(**d)


class ExecutionLedger:
    """File-backed store of ExecutionRecords (see module docstring)."""

    _OPEN = ("recorded", "sent", "confirmed")

    def __init__(self, path: Path, now_fn: Callable[[], float] = time.time):
        self.path = Path(path)
        self.now_fn = now_fn

    # -- storage --------------------------------------------------------------
    def _load(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text())
        except (ValueError, OSError):
            # A corrupt ledger must NOT look like an empty one (that would
            # forget open exposure and idempotency history). Fail loudly.
            raise RuntimeError(
                f"execution ledger at {self.path} is corrupt — refusing to "
                f"trade until a human inspects it"
            )
        return list(data.get("records", []))

    def _save(self, records: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"records": records}, indent=2))
        os.replace(tmp, self.path)          # atomic on POSIX

    # -- writes -----------------------------------------------------------------
    def append(self, rec: ExecutionRecord) -> None:
        records = self._load()
        records.append(rec.to_json())
        self._save(records)

    def record_buy(
        self,
        idempotency_key: str,
        mint: str,
        usd_size: float,
        tokens_out: float,
        price_usd: float,
        signature: str,
        status: str = "confirmed",
    ) -> ExecutionRecord:
        rec = ExecutionRecord(
            kind="buy",
            idempotency_key=idempotency_key,
            mint=mint,
            usd_size=usd_size,
            tokens_out=tokens_out,
            price_usd=price_usd,
            signature=signature,
            status=status,
            ts=self.now_fn(),
        )
        self.append(rec)
        return rec

    def mark_close(self, mint: str, proceeds_usd: float) -> ExecutionRecord:
        """
        Close the OLDEST open buy of `mint` (FIFO), realize PnL against its
        cost, and append a close record. Refuses if nothing is open.
        """
        records = self._load()
        open_buys = [r for r in records
                     if r["kind"] == "buy"
                     and r["mint"] == mint
                     and r["status"] in self._OPEN]
        if not open_buys:
            raise ValueError(f"no open position for {mint}")
        cost = open_buys[0]["usd_size"]
        rec = ExecutionRecord(
            kind="close",
            idempotency_key=f"close-{mint}-{new_id()}",
            mint=mint,
            usd_size=proceeds_usd,
            pnl_usd=proceeds_usd - cost,
            ts=self.now_fn(),
        )
        # Mark the matched buy closed in the SAME write (no lost exposure).
        for r in records:
            if (r["kind"] == "buy" and r["mint"] == mint
                    and r["status"] in self._OPEN
                    and r["idempotency_key"] == open_buys[0]["idempotency_key"]):
                r["status"] = "closed"
                break
        records.append(rec.to_json())
        self._save(records)
        return rec

    # -- reads --------------------------------------------------------------------
    def get_by_idempotency_key(self, key: str) -> Optional[ExecutionRecord]:
        for r in self._load():
            if r["kind"] == "buy" and r["idempotency_key"] == key:
                return ExecutionRecord.from_json(r)
        return None

    def open_positions(self) -> dict[str, float]:
        """{mint: open cost basis} for buys not yet closed."""
        out: dict[str, float] = {}
        for r in self._load():
            if r["kind"] == "buy" and r["status"] in self._OPEN:
                out[r["mint"]] = out.get(r["mint"], 0.0) + r["usd_size"]
        return out

    def total_open_exposure(self) -> float:
        return sum(self.open_positions().values())

    def realized_pnl_today(self) -> float:
        """Sum of pnl on close entries stamped today (local date)."""
        import datetime as _dt

        today = _dt.date.fromtimestamp(self.now_fn())
        total = 0.0
        for r in self._load():
            if r["kind"] != "close" or r.get("pnl_usd") is None:
                continue
            if _dt.date.fromtimestamp(r["ts"]) == today:
                total += r["pnl_usd"]
        return total
