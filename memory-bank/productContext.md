# Product Context — trading-bot

## Why it exists
Research, not revenue: produce a measurable, auditable paper track record
for Solana memecoin strategies so thresholds can be tuned from evidence
instead of guesswork, ahead of any human-reviewed decision about real
capital (which stays outside this system's scope).

## The user (single operator)
Runs everything locally on one machine (6GB VRAM laptop, Ollama + app on
:8000). Watches a terminal-style dark dashboard: live decision feed (WS),
open holdings with live P&L, trade journal (thesis vs outcome vs
reflection), equity stats, market-regime history, promotion-gate status,
knowledge base, system status. A persistent "PAPER TRADING — NO REAL FUNDS"
banner is on every view.

## Experience principles
- Every decision — pass or fail — is visible with its full rule breakdown
  and a grounded 1–2 sentence thesis; rejections are first-class citizens.
- "Why did the bot do nothing?" is answerable from the dashboard (regime
  panel) without reading code.
- The system degrades loudly: provider failures, missing fields, and
  grounding flags are surfaced, never hidden.

## Domain model (short)
Candidate → (10-rule gate) → GateDecision → [open/scale-in] → Trade →
(fixed exits) → closed Trade + reflection. Regime snapshot once per tick.
FeedEvent persisted for every decision either way.
