# Product Context — trading-bot

## Why it exists
Started as a paper-trading research tool, now a LIVE Solana memecoin trading
system. Deterministic rules decide entries; a cloud LLM (DeepSeek/Groq)
evaluates candidates before entry. Runs with real funds on mainnet via
Jupiter swaps, backed by a persistent execution ledger and safety-gated
kill-switch architecture.

## The user (single operator)
Runs the full stack on one machine (start.sh → FastAPI on :8000 + async
live cycle). Watches a terminal-style dark dashboard: live decision feed (WS),
open holdings with live P&L, trade journal (thesis vs outcome), equity stats,
refusal funnel, market-regime history, system status. An `[● LIVE · real money]`
badge is on the hero panel.

## Experience principles
- Every decision — pass or fail — is visible with its full rule breakdown
  and a grounded 1–2 sentence thesis; rejections are first-class citizens.
- "Why did the bot do nothing?" is answerable from the dashboard (regime
  panel, refusal funnel) without reading code.
- The system degrades loudly: provider failures, missing fields, and
  grounding flags are surfaced, never hidden.
- Risk posture is always visible: the AlertStrip shows breaker state, and
  the safety panel shows kill-switch and daily-loss breaker status.

## Domain model (short)
Candidate → (10-rule gate) → GateDecision → [LLM think: buy/skip/wait] →
(think ∩ gate = entry) → Jupiter swap → ExecutionLedger position →
(exit engine: 6-rule exit set, 15s fast scanner) → sell swap → closed
position + reflection. Regime snapshot once per tick. FeedEvent persisted
for every decision either way.
