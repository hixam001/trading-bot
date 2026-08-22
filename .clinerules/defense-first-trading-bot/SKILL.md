---
name: defense-first-trading-bot
description: Use this skill whenever writing, reviewing, or extending code in the trading-bot project — a paper-trading system that ingests external market data, calls a local LLM, and simulates a financial portfolio. Apply on every file touching money math, external API responses, LLM output parsing, or trade state transitions.
---

# Defense-first engineering for trading-bot

This project simulates a financial portfolio and will eventually be evaluated
against real market conditions. It handles three categories of untrusted or
fallible input: external API responses, LLM-generated JSON, and time-sensitive
state (open positions, prices). Treat all three as adversarial or unreliable
by default, not just "probably fine."

## Core rules

1. **Validate every external input before it touches money math or trade
   state.** API responses, LLM JSON output, and price feeds must all pass
   through explicit schema/type/range validation before being used to size a
   position, compute P&L, or decide a verdict. Never trust a field exists or
   has the expected type just because the API docs say it should.

2. **Fail closed, not open.** If a validation check fails, a request times
   out, or an LLM response is malformed, the default behavior is to skip/reject
   the candidate or halt the affected operation — never to substitute a
   guessed default value and continue silently. A skipped trade opportunity
   costs nothing. A trade opened on bad data costs real (paper) money and
   corrupts the track record. When in doubt, the safer failure is "do nothing."

3. **No real-execution code paths, ever, without an explicit, separate,
   human-reviewed change.** Do not write code that constructs, signs, or
   broadcasts a real Solana transaction under any framing (not even "just a
   stub," "just for testing," or "the user can enable it later"). If a task
   seems to require this, stop and flag it rather than implementing it.

4. **Every money-math function needs a test with a known-correct expected
   output**, especially P&L calculation, position sizing, slippage/fee
   application, and drawdown calculation. Off-by-one or sign errors in these
   functions produce a misleading track record that looks like a real trading
   edge — this is the single most damaging class of bug in this codebase.

5. **Treat the promotion gate as a security boundary, not a feature.** Its
   thresholds, its read-only nature, and the fact that it never
   auto-activates live trading are non-negotiable invariants. Any code change
   touching `promotion_gate.py` should be treated with the same scrutiny as
   an auth check in a normal web app.

6. **Log every rejection with a reason, not just every success.** When
   debugging a live trading strategy after the fact, "why didn't it buy X"
   is as important a question as "why did it buy Y." Silent skips make the
   system unauditable.

7. **Idempotency and crash-safety on trade state.** If the tick loop crashes
   or is killed mid-operation, restarting it must not double-open a position,
   lose track of an open position, or corrupt the trade log. Write state
   changes so a crash leaves the system in a recoverable, unambiguous state
   (e.g. write-then-confirm patterns, not multi-step updates with no
   rollback).

8. **Rate limits and API failures are expected, not exceptional.** External
   data APIs (Birdeye, Jupiter, etc.) will throttle, time out, or return
   malformed responses regularly in production use. Every external call
   needs a defined timeout, a defined retry policy (bounded, with backoff),
   and a defined fallback behavior — not a bare try/except that swallows the
   problem.

## Anti-patterns to reject in review

- `except Exception: pass` anywhere near money math or trade state.
- Using `.get(key, some_default)` on external API/LLM responses for values
  that feed directly into position sizing or verdicts, instead of explicit
  validation that rejects the candidate if the field is missing/wrong-typed.
- Any code path where a config flag like `PAPER_TRADING_ONLY` is checked in
  one place but not enforced at the actual point of execution.
- Retrofitting a "quick fix" for a data-source integration by guessing field
  names instead of confirming them against real API responses — an
  incorrect field mapping is a silent-corruption bug, not a cosmetic one.
