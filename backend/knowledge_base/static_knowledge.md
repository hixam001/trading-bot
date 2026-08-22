# static_knowledge.md — operator-authored context injected into prompts (F1)

## What this system is
A deterministic, paper-trading research bot for Solana memecoins. Decisions
are made ONLY by a fixed rule set; the LLM narrates decisions that have
already been made and may never decide or override anything.

## Operating doctrine
- Liquidity is exit-ability: a pool that can't absorb the position size is
  not a market, it's a trap.
- Buy/sell transaction counts are a cleaner crowd signal than dollar volume,
  which whales can fake with self-trades.
- A fresh launch that is already crashing hard (young AND down) is the
  classic "buy the dip into zero" pattern — never catch it.
- Thin 24h volume relative to market cap suggests bundled or wash-traded
  supply rather than organic interest.
- Unknown is not safe: an unchecked security field is treated as unknown,
  and unknown never blocks or clears a candidate by itself.

## Calibration posture
All thresholds start as documented placeholders. They are tuned only from
the 10-day paper-trading record, one variable at a time, with the specific
rule's rejection log as evidence.
