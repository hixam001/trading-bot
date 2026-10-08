# Project Brief — trading-bot

**One-liner:** Solana memecoin trading bot — deterministic rules decide entries,
a cloud LLM evaluates each candidate, and a safety-gated execution engine
routes live swaps via Jupiter.

## Core requirement
Watch real Solana memecoin markets on a ~60s tick, evaluate every candidate
against ten deterministic rules (AND = entry decision), intersect with
LLM thesis (think stage), execute real Jupiter swaps through the live
execution engine, manage positions with a six-rule exit engine (15s fast
scanner), and log everything for auditable track records.

## Hard constraints
1. `PAPER_TRADING_ONLY=True` remains hardcoded in config.py as a historical
   safety flag. The live execution path (`run_live_cycle.py`) is the actual
   runner; the paper engine is retired (§52).
2. `promotion_gate.py` is read-only forever; never writes/triggers/promotes.
3. Fail closed: unknown data skips/rejects, never guesses; None ≠ False.
4. LLM evaluates candidates (think stage) but the RULE ENGINE is the
   deterministic authority — entry requires BOTH think-buy AND all-rules-pass.

## Success criteria
- Live trading with measurable, auditable track record
- Every decision auditable: full rule breakdown + thesis in the feed/journal
- Atomic money handling: cash debited/credited exactly once under retries,
  races, and crashes (proven by tests)
- Safety: kill-switch, daily-loss breaker, position/exposure caps,
  idempotent ledger

## Authoritative docs
docs/00_BLUEPRINT → 01_ARCHITECTURE → 02_FEATURE_LIST(status) →
03_GANTT → 05_VERIFICATION_APPENDIX → 06_REFERENCE_COMPARISON(the reference bot) →
07_PROJECT_REPORT. Living state: handoff.md (root) + memory-bank/.
