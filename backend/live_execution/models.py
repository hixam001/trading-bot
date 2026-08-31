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
    # §50: the exit rule that produced this close ("exit_stop_loss",
    # "exit_take_profit", "outofband", ...) for exit-mix forensics + tranche
    # counting. Absent on pre-§50 rows (treated as unknown), buys, and
    # hand-written records — never fabricated.
    rule_id: Optional[str] = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "ExecutionRecord":
        # §50: pre-§50 rows lack rule_id — the field filter lets the
        # dataclass default fill it; unknown stray keys are dropped too.
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


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

    # -- §50 exit forensics ---------------------------------------------------------
    def tranches_taken(self, mint: str) -> int:
        """
        Take-profit tranches taken against the CURRENT open buy of `mint`:
        the count of `exit_take_profit` close records newer than the open
        buy's own timestamp. Kills the latent re-trim bug at the source —
        before §50 the live path passed tranches_taken=0 to the exit engine
        on every cycle, so a TP rung would have re-trimmed every 60s until
        the position was gone.
        """
        records = self._load()
        open_ts = None
        for r in records:
            if (r.get("kind") == "buy" and r.get("mint") == mint
                    and r.get("status") in self._OPEN):
                open_ts = r.get("ts")
                break
        if open_ts is None:
            return 0
        n = 0
        for r in records:
            if (r.get("kind") == "close" and r.get("mint") == mint
                    and (r.get("rule_id") or "") == "exit_take_profit"
                    and float(r.get("ts") or 0.0) >= float(open_ts)):
                n += 1
        return n

    def last_close_ts(self, mint: str) -> Optional[float]:
        """§50: newest close record ts for the mint (sell-gate cooldown input)."""
        ts = None
        for r in self._load():
            if r.get("kind") == "close" and r.get("mint") == mint:
                t = float(r.get("ts") or 0.0)
                if ts is None or t > ts:
                    ts = t
        return ts

    def closes_since(self, ts: float) -> int:
        """§50: count of close records newer than `ts` (sell-gate 24h ceiling)."""
        return sum(1 for r in self._load()
                   if r.get("kind") == "close"
                   and float(r.get("ts") or 0.0) >= float(ts))

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

    def open_token_amounts(self) -> dict[str, float]:
        """{mint: token units still open} summed over unclosed buys."""
        out: dict[str, float] = {}
        for r in self._load():
            if r["kind"] == "buy" and r["status"] in self._OPEN:
                out[r["mint"]] = out.get(r["mint"], 0.0) + float(r.get("tokens_out") or 0.0)
        return out

    def deployed_today_usd(self) -> float:
        """Sum of buy cost stamped today - feeds MAX_DAILY_DEPLOY_USD."""
        import datetime as _dt

        today = _dt.date.fromtimestamp(self.now_fn())
        total = 0.0
        for r in self._load():
            if r["kind"] == "buy" and _dt.date.fromtimestamp(r["ts"]) == today:
                total += float(r.get("usd_size") or 0.0)
        return total

    def reduce_position(self, mint: str, fraction: float, proceeds_usd: float,
                        full_close: bool = False,
                        rule_id: Optional[str] = None) -> ExecutionRecord:
        # Partial-or-full SELL against the OLDEST open buy (FIFO). fraction=1.0
        # closes; smaller fractions shrink the open buy pro-rata - trims need this.
        #
        # full_close=True is the reconcile-clamped FULL exit: the exit engine
        # intended to close the whole position and the sell disposed of everything
        # the chain confirms we hold, so the position is CLOSED outright even though
        # the fraction landed just below the trim threshold. Without this, the
        # journal-vs-chain dust (buy fill slightly under quote) is left as a
        # phantom OPEN position that pollutes holdings and counts against
        # MAX_OPEN_POSITIONS forever (the stale-holdings bug).
        #
        # rule_id (§50): the exit rule that produced this close, stored for
        # exit-mix forensics and take-profit tranche counting.
        records = self._load()
        open_buys = [r for r in records if r["kind"] == "buy" and r["mint"] == mint and r["status"] in self._OPEN]
        if not open_buys:
            raise ValueError(f"no open position for {mint}")
        target_key = open_buys[0]["idempotency_key"]
        frac = min(max(fraction, 0.01), 1.0)
        rec = ExecutionRecord(
            kind="close",
            idempotency_key=f"close-{mint}-{new_id()}",
            mint=mint,
            usd_size=proceeds_usd,
            pnl_usd=None,
            ts=self.now_fn(),
            rule_id=rule_id,
        )
        for r in records:
            if (r["kind"] == "buy" and r["mint"] == mint
                    and r["status"] in self._OPEN
                    and r["idempotency_key"] == target_key):
                if full_close:
                    # Realize against the FULL cost and close outright. The
                    # journal-vs-chain dust is written off, never left open.
                    rec.pnl_usd = proceeds_usd - float(r["usd_size"])
                    r["status"] = "closed"
                else:
                    cost_part = float(r["usd_size"]) * frac
                    rec.pnl_usd = proceeds_usd - cost_part
                    if frac >= 0.999:
                        r["status"] = "closed"
                    else:
                        r["usd_size"] = float(r["usd_size"]) - cost_part
                        r["tokens_out"] = float(r.get("tokens_out") or 0.0) * (1.0 - frac)
                break
        records.append(rec.to_json())
        self._save(records)
        return rec

    def close_out_of_band(
        self, mint: str, proceeds_usd: Optional[float] = None,
        note: str = "",
    ) -> list[ExecutionRecord]:
        """
        Operator-review completion (2026-08-29, the vanished-position repair).

        reconcile() deliberately never mutates the ledger on a chain
        disagreement (an RPC glitch must not corrupt money records) — a
        position that vanished on-chain is flagged `chain_excluded` and
        logged "operator review needed" EVERY cycle until a human decides.
        This method IS that human decision, recorded as such:

          * closes EVERY open buy of `mint` (status -> "closed")
          * appends ONE close record with `pnl_usd=None` when proceeds are
            unknown — an out-of-band sell's proceeds are NOT fabricated; the
            daily-loss breaker (realized_pnl_today) skips None rows, so an
            unknown-proceeds close can never trip it on a made-up number
          * `proceeds_usd` may be provided when the operator knows the fill
            (e.g. from the wallet's sell tx); PnL is then realized against
            the summed cost of the closed buys
          * idempotency_key carries "outofband" + note for audit forensics

        Refuses (ValueError) when there is nothing open for the mint — a
        typo'd mint must not be able to invent a close.
        """
        records = self._load()
        open_buys = [r for r in records
                     if r["kind"] == "buy" and r["mint"] == mint
                     and r["status"] in self._OPEN]
        if not open_buys:
            raise ValueError(f"no open position for {mint}")
        total_cost = sum(float(r.get("usd_size") or 0.0) for r in open_buys)
        total_tokens = sum(float(r.get("tokens_out") or 0.0) for r in open_buys)
        for r in open_buys:
            r["status"] = "closed"
        rec = ExecutionRecord(
            kind="close",
            idempotency_key=f"close-{mint}-outofband-{new_id()}",
            mint=mint,
            usd_size=float(proceeds_usd) if proceeds_usd is not None else 0.0,
            tokens_out=total_tokens,
            price_usd=0.0,
            signature="",
            status="closed",
            ts=self.now_fn(),
            pnl_usd=(float(proceeds_usd) - total_cost)
                     if proceeds_usd is not None else None,
            rule_id="outofband",
        )
        if note:
            # Not a dataclass field; carried in the JSON row for forensics.
            rec_json = rec.to_json()
            rec_json["note"] = f"out-of-band: {note}"
            records.append(rec_json)
        else:
            records.append(rec.to_json())
        self._save(records)
        return [ExecutionRecord.from_json(r) for r in open_buys] + [rec]
