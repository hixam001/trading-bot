#!/usr/bin/env python3
"""Resolve a journaled sell using locked accounting methods; never signs/sends.

Exit codes match resolve_pending_buy: 0 resolved/listed, 1 refused, 2 usage.
Release requires the operator's attestation AND an interactive typed action.
"""
from __future__ import annotations

import argparse
import asyncio
import math
from pathlib import Path
import sys
import time

_BACKEND = Path(__file__).resolve().parents[1] / 'backend'
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from live_execution import config
from live_execution.commit_log import CommitLog
from live_execution.executor import _resolve_sell_signature, _account_confirmed_sell
from live_execution.models import ExecutionLedger


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=None)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='Read-only status and release evidence')
    for command in ('release', 'confirm'):
        sub = commands.add_parser(command)
        sub.add_argument('--mint', required=True)
        sub.add_argument('--signature', required=True)
        if command == 'release':
            sub.add_argument('--i-verified-signature-never-landed', action='store_true')
        else:
            sub.add_argument('--proceeds-usd', required=True, type=float)
    return parser.parse_args(argv)


def _refuse(reason):
    print(f'REFUSED: {reason}; no resolution recorded.', file=sys.stderr)
    return 1


async def resolve(args, *, input_fn=None, stdin=None):
    directory = args.state_dir or config.STATE_DIR
    log = CommitLog(directory / 'commits.json')
    records = log.all()
    if args.command == 'list':
        for rec in records:
            attempt = rec.get('sell_attempt', {})
            if attempt.get('status') != 'pending':
                continue
            diagnostics = {}
            state, reason = await _resolve_sell_signature(
                attempt['signature'], rec, log, persist_candidate=False, diagnostics=diagnostics)
            signed = attempt.get('signed_at')
            age = 'unknown (legacy)' if signed is None else f'{max(0, time.time() - signed):.0f}s'
            print(f"mint={rec['payload']['mint']} signature={attempt['signature']} age={age} status={state} conditions={reason}")
            print("release_conditions " + " ".join(f"{key}={value}" for key, value in diagnostics.items()))
            print(f"https://solscan.io/tx/{attempt['signature']}")
        return 0
    rec = next((r for r in records if r.get('kind') == 'sell'
                and r.get('payload', {}).get('mint') == args.mint
                and any(a.get('signature') == args.signature for a in
                        [r.get('sell_attempt', {})] + r.get('sell_attempt_history', []))), None)
    if rec is None:
        return _refuse('signature is unknown to commits.json for this mint')
    attempts = [rec.get('sell_attempt', {})] + rec.get('sell_attempt_history', [])
    attempt = next(a for a in attempts if a.get('signature') == args.signature)
    target = 'failed' if args.command == 'release' else 'confirmed'
    if args.command == 'release' and not args.i_verified_signature_never_landed:
        return _refuse('--i-verified-signature-never-landed is required')
    if args.command == 'confirm' and (not math.isfinite(args.proceeds_usd) or args.proceeds_usd < 0):
        return _refuse('proceeds must be finite and nonnegative')
    if attempt.get('status') == target:
        return 0  # A retry after settlement cannot duplicate ledger accounting.
    if attempt.get('status') != 'pending':
        return _refuse('existing settlement conflicts with requested action')
    if args.command == 'confirm':
        state, _ = await _resolve_sell_signature(args.signature, rec, log, persist_candidate=False)
        if state != 'confirmed':
            return _refuse('chain confirmation is unknown or conflicting')
    print(f"pending sell: mint={args.mint} signature={args.signature}")
    input_fn = input_fn or input
    stdin = stdin or sys.stdin
    if not stdin.isatty() or input_fn(f"Type '{args.command}' to confirm: ").strip().lower() != args.command:
        return _refuse('operator did not type the action at an interactive prompt')
    if args.command == 'release':
        note = 'operator release: signature verified never landed'
        log.settle_sell_attempt(rec['hash'], args.signature, 'failed', note=note, archive=True)
        log.note(rec['hash'], note + ': ' + args.signature)
    else:
        ledger = ExecutionLedger(directory / 'executions.json')
        accounting = _account_confirmed_sell(
            rec, ledger, log, args.proceeds_usd,
            dict(side="sell", mint=args.mint, signature=args.signature))
        if accounting is not None and accounting.unresolved:
            return _refuse("confirmed sell accounting needs repair")
        log.note(rec['hash'], 'operator confirmed: ' + args.signature)
    print(f'{args.command}: {args.signature}')
    return 0


def main(argv=None):
    try:
        return asyncio.run(resolve(parse_args(argv)))
    except (RuntimeError, ValueError, OSError):
        return _refuse('state unavailable or accounting failed; inspect state validation')


if __name__ == '__main__':
    raise SystemExit(main())
