#!/usr/bin/env python3
"""
scripts/resolve_pending_buy.py — operator resolution for one pending live buy (§88 F).

A pending buy is an on-chain commitment whose send outcome was unknown. The
cycle resolver normally settles it, but an operator may need to intervene when
the chain data is conclusive before the automatic path acts:

    --expire            the transaction can never land: the blockhash is
                        provably invalid, every endpoint reports the signature
                        absent, and the row is >= 90 s old; release caps/slots.
    --force-expire      the automatic checks cannot run (RPC down): skip ONLY
                        the blockhash/age gates; every endpoint must still
                        report the signature absent. Audited with forced=true.
    --adopt-from-sig    the fill landed but was never adopted; read the actual
                        token/USDC deltas from the confirmed transaction and
                        turn the pending row into a managed position.

Every action is destructive bookkeeping: the tool prints the row, requires an
interactive confirmation (or explicit --yes) and appends one JSON audit line
to ``resolve_pending_buy_audit.log``. It never imports the private key; fill
actuals are read from public transaction data and the owner defaults to
``EXPECTED_WALLET_ADDRESS``.

Exit codes: 0 resolved, 1 refused/unknown/no-op, 2 usage error.

Examples:
    docker compose run --rm --entrypoint python live \\
        /app/scripts/resolve_pending_buy.py '<key>' --expire
    docker compose run --rm --entrypoint python live \\
        /app/scripts/resolve_pending_buy.py '<key>' \\
        --adopt-from-sig '<signature>' --yes
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Callable, Optional, TextIO

# Add backend directory to sys.path so live_execution is importable when this
# file is executed directly from the repository (not only via the image).
repo_root = Path(__file__).resolve().parents[1]
backend_dir = repo_root / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from live_execution import config as live_config
from live_execution import solana
from live_execution.commit_log import CommitLog
from live_execution.executor import _fill_actuals
from live_execution.models import ExecutionLedger


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve ONE pending live buy (expire it or adopt a landed fill).",
    )
    parser.add_argument(
        "key",
        help="idempotency_key of the pending buy row to resolve.",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--expire",
        action="store_true",
        help="Mark the buy expired: it provably cannot land; release caps/slots.",
    )
    action.add_argument(
        "--force-expire",
        action="store_true",
        help=("Mark the buy expired after bypassing ONLY the blockhash/age "
              "gates (use when RPC is down). Every endpoint must still report "
              "the signature absent; audited with forced=true."),
    )
    action.add_argument(
        "--adopt-from-sig",
        nargs="?",
        const="",
        default=None,
        metavar="SIG",
        help=("Adopt a confirmed fill. SIG overrides the stored signature; "
              "omit the value to use the pending row's own signature."),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not prompt (required when stdin is not a TTY).",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="State directory (defaults to live_execution.config.STATE_DIR).",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="Ledger path (defaults to <state-dir>/executions.json).",
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        help="Audit log path (defaults to <state-dir>/resolve_pending_buy_audit.log).",
    )
    return parser.parse_args(argv)


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    state_dir = args.state_dir or live_config.STATE_DIR
    ledger_path = args.ledger or state_dir / "executions.json"
    audit_path = args.audit_log or state_dir / "resolve_pending_buy_audit.log"
    return state_dir, ledger_path, audit_path


def _find_pending(ledger: ExecutionLedger, key: str):
    return next((r for r in ledger.pending_buys()
                 if r.idempotency_key == key), None)


def _append_audit(path: Path, payload: dict) -> None:
    """Append one machine-readable line; never let audit I/O hide the result."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"WARNING: could not write audit log {path}: {exc}",
              file=sys.stderr)


def _audit_payload(rec, action: str, result: str, ledger_path: Path,
                   *, signature: str = "", usd_size=None, tokens_out=None,
                   reason: str = "", forced: bool = False,
                   now_fn: Callable[[], float] = time.time) -> dict:
    return {
        "ts": now_fn(),
        "key": rec.idempotency_key,
        "mint": rec.mint,
        "action": action,
        "result": result,
        "signature": signature or rec.signature or "",
        "usd_size": usd_size,
        "tokens_out": tokens_out,
        "reason": reason,
        "forced": forced,
        "ledger": str(ledger_path),
    }


def _confirm(action: str, rec, input_fn: Callable[[str], str],
             stdin: TextIO) -> bool:
    print(f"pending buy : {rec.idempotency_key}")
    print(f"mint        : {rec.mint}")
    print(f"size        : ${float(rec.usd_size or 0.0):.4f}")
    print(f"signature   : {rec.signature or '(none)'}")
    print(f"blockhash   : {rec.blockhash or '(unknown)'}")
    print(f"age         : {max(time.time() - float(rec.ts or 0.0), 0.0):.0f}s")
    if not stdin.isatty():
        print("REFUSED: stdin is not a TTY; pass --yes to confirm "
              "non-interactively.", file=sys.stderr)
        return False
    answer = input_fn(f"Type '{action}' to confirm: ")
    return answer.strip().lower() == action


def _refuse_expire(rec, audit_path: Path, ledger_path: Path, reason: str,
                   *, forced: bool, now_fn: Callable[[], float]) -> int:
    print(f"REFUSED: {reason}; pending row left untouched.", file=sys.stderr)
    _append_audit(audit_path, _audit_payload(
        rec, "expire", "refused", ledger_path, reason=reason,
        forced=forced, now_fn=now_fn))
    return 1


async def resolve(args: argparse.Namespace, *,
                  input_fn: Optional[Callable[[str], str]] = None,
                  stdin: Optional[TextIO] = None,
                  actuals_fn: Optional[Callable] = None,
                  commit_log_cls=None,
                  blockhash_valid_fn: Optional[Callable] = None,
                  signature_status_all_fn: Optional[Callable] = None,
                  now_fn: Optional[Callable[[], float]] = None) -> int:
    input_fn = input_fn or input
    actuals_fn = actuals_fn or _fill_actuals
    commit_log_cls = commit_log_cls or CommitLog
    blockhash_valid_fn = blockhash_valid_fn or solana.is_blockhash_valid
    signature_status_all_fn = (
        signature_status_all_fn or solana.get_signature_status_all)
    now_fn = now_fn or time.time
    state_dir, ledger_path, audit_path = _paths(args)
    ledger = ExecutionLedger(ledger_path)
    rec = _find_pending(ledger, args.key)
    action = ("expire" if (args.expire or args.force_expire) else "adopt")

    if rec is None:
        print(f"ERROR: no pending buy with key {args.key!r} in {ledger_path}",
              file=sys.stderr)
        _append_audit(audit_path, {
            "ts": time.time(), "key": args.key, "action": action,
            "result": "not-found", "ledger": str(ledger_path),
        })
        return 1

    signature = (args.adopt_from_sig or rec.signature) if action == "adopt" else ""
    if action == "adopt" and not signature:
        print("ERROR: no signature to adopt; pass --adopt-from-sig SIG "
              "or store one on the pending row.", file=sys.stderr)
        _append_audit(audit_path, _audit_payload(
            rec, action, "refused", ledger_path,
            reason="no signature"))
        return 1

    if not args.yes:
        if stdin is None:
            stdin = sys.stdin
        if not _confirm(action, rec, input_fn, stdin):
            print("REFUSED: operator did not confirm; nothing changed.",
                  file=sys.stderr)
            _append_audit(audit_path, _audit_payload(
                rec, action, "refused", ledger_path, signature=signature,
                reason="operator did not confirm"))
            return 1

    if action == "expire":
        forced = bool(getattr(args, "force_expire", False))
        if not forced:
            age = now_fn() - float(rec.ts or 0.0)
            if age < live_config.PENDING_EXPIRY_MIN_AGE_SECONDS:
                return _refuse_expire(
                    rec, audit_path, ledger_path,
                    f"row is only {age:.0f}s old; wait "
                    f"{live_config.PENDING_EXPIRY_MIN_AGE_SECONDS:.0f}s before "
                    "expiring (a late land is still possible)",
                    forced=forced, now_fn=now_fn)
            if not rec.blockhash:
                return _refuse_expire(
                    rec, audit_path, ledger_path,
                    "no blockhash stored; cannot prove the transaction can "
                    "never land", forced=forced, now_fn=now_fn)
            try:
                valid = await blockhash_valid_fn(rec.blockhash)
            except Exception as exc:
                return _refuse_expire(
                    rec, audit_path, ledger_path,
                    f"blockhash validity read failed: {exc}",
                    forced=forced, now_fn=now_fn)
            if valid is None:
                return _refuse_expire(
                    rec, audit_path, ledger_path,
                    "blockhash validity is unknown (RPC failure); refusing to "
                    "expire on unknown data", forced=forced, now_fn=now_fn)
            if valid is not False:
                return _refuse_expire(
                    rec, audit_path, ledger_path,
                    "blockhash is still valid; the transaction can still land",
                    forced=forced, now_fn=now_fn)

        # --force-expire skips ONLY the blockhash/age gates: a signature that
        # is present on any endpoint must still block expiry (adopt it), and an
        # unreadable/empty re-query is unknown, never "absent".
        if not rec.signature:
            return _refuse_expire(
                rec, audit_path, ledger_path,
                "no signature stored; cannot prove the transaction is absent "
                "from every endpoint", forced=forced, now_fn=now_fn)
        try:
            statuses = await signature_status_all_fn(rec.signature)
        except Exception as exc:
            return _refuse_expire(
                rec, audit_path, ledger_path,
                f"signature status re-query failed: {exc}",
                forced=forced, now_fn=now_fn)
        if not statuses:
            return _refuse_expire(
                rec, audit_path, ledger_path,
                "no endpoint returned a status; refusing to expire on "
                "unknown data", forced=forced, now_fn=now_fn)
        if any(status is not None for status in statuses):
            return _refuse_expire(
                rec, audit_path, ledger_path,
                "signature is present on-chain; do not expire — adopt the "
                "confirmed fill with --adopt-from-sig",
                forced=forced, now_fn=now_fn)

        reason = (
            "operator FORCE-expired pending buy: chain proof bypassed"
            if forced else
            "operator expired pending buy: blockhash cannot land")
        settled = ledger.settle_pending_buy(args.key, "expired", reason=reason)
        if not settled:
            print("ERROR: pending row changed before it could be expired; "
                  "re-read the ledger.", file=sys.stderr)
            _append_audit(audit_path, _audit_payload(
                rec, action, "no-op", ledger_path, reason="row no longer pending",
                forced=forced, now_fn=now_fn))
            return 1
        if rec.commit_hash:
            try:
                commit_log_cls(state_dir / "commits.json").fail(
                    rec.commit_hash, reason)
            except Exception as exc:
                print(f"WARNING: commit log fail() skipped: {exc}",
                      file=sys.stderr)
        _append_audit(audit_path, _audit_payload(
            rec, action, "expired", ledger_path, reason=reason,
            forced=forced, now_fn=now_fn))
        print(f"EXPIRED {rec.idempotency_key} ({rec.mint[:8]}); caps released.")
        return 0

    # -- adopt: require ACTUAL amounts from the confirmed transaction. The
    # stored quote is deliberately NOT a fallback here — an operator acting
    # manually has the chain data available, and inventing a basis is worse
    # than leaving the row pending. The owner is mandatory: without it the
    # balance deltas would sum the pool/counterparty accounts, not the wallet.
    owner = live_config.EXPECTED_WALLET_ADDRESS or None
    if not owner:
        print(
            "ERROR: EXPECTED_WALLET_ADDRESS is not set; fill deltas cannot be "
            "attributed to this wallet. Set it in .env before adopting.",
            file=sys.stderr)
        _append_audit(audit_path, _audit_payload(
            rec, action, "refused", ledger_path, signature=signature,
            reason="EXPECTED_WALLET_ADDRESS not set"))
        return 1
    try:
        actuals = await actuals_fn("buy", rec.mint, signature, owner=owner)
    except Exception as exc:
        print(f"ERROR: fill actuals read failed for signature "
              f"{signature[:16]}: {exc}; pending row left untouched.",
              file=sys.stderr)
        _append_audit(audit_path, _audit_payload(
            rec, action, "refused", ledger_path, signature=signature,
            reason=f"fill actuals read failed: {exc}"))
        return 1
    usd_actual = actuals.get("usdc_delta")
    tokens_actual = actuals.get("tokens_delta")
    if (usd_actual is None or tokens_actual is None
            or float(usd_actual) <= 0 or float(tokens_actual) <= 0):
        print(
            "ERROR: fill actuals unreadable for signature "
            f"{signature[:16]} (usd={usd_actual!r} tokens={tokens_actual!r}); "
            "pending row left untouched — NEVER fabricating a cost basis.",
            file=sys.stderr)
        _append_audit(audit_path, _audit_payload(
            rec, action, "refused", ledger_path, signature=signature,
            reason="fill actuals unreadable"))
        return 1

    usd_actual = float(usd_actual)
    tokens_actual = float(tokens_actual)
    price = usd_actual / tokens_actual
    settled = ledger.settle_pending_buy(
        args.key, "confirmed", usd_size=usd_actual, tokens_out=tokens_actual,
        price_usd=price, signature=signature,
        reason="operator adopted confirmed fill")
    if not settled:
        print("ERROR: pending row changed before it could be adopted; "
              "re-read the ledger.", file=sys.stderr)
        _append_audit(audit_path, _audit_payload(
            rec, action, "no-op", ledger_path, signature=signature,
            reason="row no longer pending"))
        return 1
    if rec.commit_hash:
        try:
            commit_log_cls(state_dir / "commits.json").bind(
                rec.commit_hash, signature, allow_failed=True)
        except Exception as exc:
            print(f"WARNING: commit log bind() skipped: {exc}", file=sys.stderr)
    _append_audit(audit_path, _audit_payload(
        rec, action, "adopted", ledger_path, signature=signature,
        usd_size=usd_actual, tokens_out=tokens_actual,
        reason="operator adopted confirmed fill"))
    print(f"ADOPTED {rec.idempotency_key} ({rec.mint[:8]}): "
          f"${usd_actual:.4f} -> {tokens_actual:.6f} tokens sig "
          f"{signature[:16]}; the position is now managed by the exit engine.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return asyncio.run(resolve(args))


if __name__ == "__main__":
    sys.exit(main())
