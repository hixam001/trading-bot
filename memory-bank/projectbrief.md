# Project Brief — trading-bot

**One-liner:** local paper-trading research system for Solana memecoins.
Deterministic rules decide; a local LLM only explains decided trades.

## Core requirement
Watch real Solana memecoin markets on a ~60s tick, evaluate every candidate
against ten deterministic rules (AND = entry decision), manage simulated
positions with three fixed numeric exit conditions, narrate every decision
with a grounded local LLM, and log everything for a 10-day calibration
window that tunes thresholds from evidence.

## Hard constraints
1. PAPER TRADING ONLY — no wallet, no transaction construction, ever.
   `PAPER_TRADING_ONLY=True` hardcoded in backend/config.py and asserted at
   runtime inside every position-opening function.
2. promotion_gate.py is read-only forever; never writes/triggers/promotes.
3. Fail closed: unknown data skips/rejects, never guesses; None ≠ False.
4. No LLM in any decision path — narration and advisory flags only.

## Success criteria
- 10-day live calibration window producing a trustworthy track record
- Every decision auditable: full rule breakdown + thesis in the feed/journal
- Atomic money handling: cash debited/credited exactly once under retries,
  races, and crashes (proven by tests)

## Authoritative docs
docs/00_BLUEPRINT → 01_ARCHITECTURE → 02_FEATURE_LIST(status) →
03_GANTT → 05_VERIFICATION_APPENDIX → 06_REFERENCE_COMPARISON(omotrades) →
07_PROJECT_REPORT. Living state: handoff.md (root) + memory-bank/.
