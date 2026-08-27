# 08 — LLM API Migration and Feedback Plan

**Status:** proposed; paper-trading only
**Date:** 2026-08-26
**Scope:** replace Qwen/Ollama with the DeepSeek API for thesis generation and model analysis, retain Groq for social evidence, and add measurable feedback without allowing an LLM to control money.

## 1. Decision Summary

### Recommended production arrangement

| Work | Primary | Fallback | Why |
|---|---|---|---|
| Thesis and pre-trade thinker | **DeepSeek V4 Flash, direct DeepSeek API, non-thinking mode** | deterministic refusal/template explanation | Best cost/quality fit for short structured analysis. Direct API avoids an additional gateway dependency and supports JSON output. |
| Twitter/social evidence classifier | **Groq**, using the fastest currently available small structured-output model that passes the benchmark | skip the social line and continue with tape/rules | Social output is evidence only and benefits more from low latency and availability than from the thinker’s deeper reasoning. |
| Post-close reflection | DeepSeek V4 Flash, queued and rate-limited | deterministic reflection | Not time-critical; run off-peak and never block exits. |
| Daily feedback report | DeepSeek V4 Flash only when the report adds value; deterministic aggregates remain authoritative | deterministic report | The model may summarize evidence and propose experiments, never edit thresholds. |

The LLM does **not** execute trades. “DeepSeek for thesis and execution” means
DeepSeek supplies the pre-trade thesis/verdict and post-close analysis that
surround the execution stage. The actual sequence remains `think -> gate ->
seal -> deterministic paper execution`; rules, cash guards, exits, and
`PAPER_TRADING_ONLY` remain authoritative. Groq is used only for the separate
social-evidence read and can never approve or veto a trade.

Use **V4 Pro only as an evaluation challenger**, not as the default trading-path model. Its higher output price and latency are difficult to justify for a 1–2 sentence JSON decision. Do not use a reasoning/thinking mode in the hot path until an offline benchmark proves a measurable improvement in precision after fees and slippage.

DeepSeek’s official pricing page, checked 2026-08-26, lists V4 Flash JSON output, a 1M context limit, and separate peak/off-peak prices. The exact price and model version must be read from the provider page at implementation time because DeepSeek states that prices can change.

## 2. Current State and Reference Finding

The current code is not Qwen-only in the same sense for every task:

- `backend/llm/thinker.py` calls local Qwen before the deterministic gate. Entry requires `think.verdict == "buy"` **and** every active rule to pass.
- `backend/llm/narrator.py` and the post-close reflection also use Ollama/Qwen, with template fallbacks.
- `backend/llm/social.py` is already a generic OpenAI-compatible client. Its provider is selected by `SOCIAL_LLM_BASE_URL`, `SOCIAL_LLM_API_KEY`, and `SOCIAL_LLM_MODEL`; the current default is Groq.
- `backend/llm/reuse.py` suppresses duplicate thinker calls for stable candidates for three ticks.
- `backend/learning_loop.py` stores daily P&L and rejection breakdowns and logs manual threshold-review recommendations.
- `backend/main.py` schedules reflections after close and runs daily learning once per UTC day.
- `provider_call_counters` currently records call/error/429 counts, but not model, token usage, cost, latency, or cache hits.

The older architecture and project-report sections still describe narration-only Qwen. The current handoff and code are authoritative for this plan: the thinker is a veto layer, but the rules, exits, cash guards, blocklist, and paper-only boundary remain authoritative.

### Does the reference have self-learning?

Based on the reviewed public reference payloads, proof material, and the repository’s comparison, there is **no evidence of autonomous model training, weight updates, or an API-driven self-learning loop**. The reference appears to use a live agent/reasoning layer, market and social inputs, position-management state, and commit/reveal auditability. That is adaptive decision context, not demonstrated self-training.

This project currently has a narrower feedback loop:

1. It records every candidate, rule result, model verdict, source, grounding flag, seal, trade, exit, and reflection.
2. It computes daily win rate, profit factor, drawdown, P&L, and rejection counts.
3. It emits human-review recommendations.
4. It never automatically changes a threshold, prompt, model, or trade decision.

Therefore, the feedback loop is **implemented as measurement and review**, not self-learning. It is not yet implemented as a robust model-evaluation loop because token usage, counterfactual outcomes, calibration buckets, model-version comparisons, and recommendation approval records are missing.

One current safety detail must be corrected during migration: the live
template fallback in `thinker.py` can currently return `buy` when Ollama is
down. The target API contract is stricter: an unavailable, timed-out, or
unparseable thinker response returns `pass` for entry purposes. A
deterministic template may still explain the refusal, but it must never
create a positive model veto result during provider failure.

## 3. Why DeepSeek for the Thinker

The thinker needs structured JSON, short grounded output, good numeric reading, and enough judgment to reject weak attention. It does not need a million-token context, tools, images, or open-ended prose.

V4 Flash is the right first candidate because:

- it supports JSON output;
- direct API access fits the existing async `httpx` pattern;
- non-thinking mode should reduce latency and output-token spend;
- the published price is low enough for frequent short calls;
- a cached, stable prompt prefix can reduce input cost if the API reports cache hits;
- its large context limit does not require us to send large context. The bot should continue sending only a compact candidate view.

This is a recommendation to benchmark, not a claim that the model will improve trades automatically. The acceptance criterion is realized paper-trading quality after costs, not eloquence or model preference.

The same page currently lists V4 Flash at **$0.22 / 1M cache-miss input
tokens** and **$0.66 / 1M output tokens** off-peak; cache-hit input is
$0.007 / 1M. Peak rates are double those values. V4 Pro is currently
$0.66 / $1.98 per 1M cache-miss input/output tokens off-peak. At a rough
500-input/100-output request, V4 Flash is about **$0.000176** off-peak when
the input is a cache miss, versus about **$0.000528** for V4 Pro. These are
planning examples, not a guarantee of future billing.

## 4. Twitter and Social Policy

Keep Twitter/social on Groq initially. The social stage is deliberately weaker than the thinker:

- it returns only `organic`, `peaked`, or `unclear` plus one evidence sentence;
- it cannot open, size, veto, close, or override a trade;
- it is capped at `SOCIAL_READ_PER_TICK` and already runs concurrently;
- stale or missing social data is acceptable because the tape and deterministic rules remain available.

Using DeepSeek for both stages would simplify vendor count, but it would couple two different latency and availability requirements and make a social outage more likely to delay the hot path. Keep Groq unless the benchmark shows that its social classification materially harms entry precision. A future single-provider fallback may use DeepSeek for social only during Groq failure, with a strict timeout and no retry storm.

Twitter data acquisition is a separate problem from LLM selection. Do not equate “Twitter enabled” with “Twitter evidence is fresh.” Add source timestamp, post count, query, and fetch status to the candidate evidence before treating it as useful.

## 5. Token and Cost Budget

### Hot-path target

The thinker prompt should be reduced to a stable system prefix plus a compact candidate payload. Target, to be measured from actual API usage:

- input: 300–600 tokens per non-reused candidate;
- output: 60–140 tokens;
- maximum output: 192 tokens;
- temperature: 0.0–0.2;
- one request per candidate at most;
- timeout: 8–12 seconds;
- no automatic retry after a valid HTTP response with invalid JSON;
- concurrency: start at 4 and tune from p95 latency and provider limits.

At 20 candidates per minute, an uncapped design could issue 28,800 thinker calls per day. That is unacceptable as a default cost assumption. The effective budget must be reduced by the existing and planned controls:

1. blocklist before enrichment and thinking;
2. candidate priority: only the top scored-by-data candidates receive API thinking, while every candidate still receives deterministic rules and a logged template thesis;
3. thesis reuse for unchanged candidates;
4. no duplicate in-flight request for a mint;
5. a daily token/USD budget with fail-closed degradation to the template;
6. queued reflections and daily reports outside the trade path;
7. per-provider rate-limit and error circuit breakers.

### Cost formula

Persist provider-reported usage and calculate:

`cost = (input_tokens / 1,000,000 × input_price) + (output_tokens / 1,000,000 × output_price)`

Track cache-hit and cache-miss input tokens separately. For DeepSeek, calculate peak and off-peak cost using the price returned by the configured pricing snapshot. Never infer usage from character count when the API supplies usage metadata.

### Peak-hour policy

The DeepSeek pricing page currently defines weekday peak windows as **01:00–04:00 UTC and 06:00–10:00 UTC**, with off-peak rates outside those windows. The implementation should:

- continue thinker requests during all market hours when the candidate is high priority;
- avoid non-urgent reflections, challenger evaluations, and daily narrative summaries during peak windows;
- record `is_peak_window` on every request;
- enforce separate hot-path and background budgets;
- never delay a risk exit to wait for off-peak pricing;
- support an operator-configurable UTC schedule because price policy may change.

The bot must not assume that off-peak means free or that peak pricing is a reason to skip a time-sensitive deterministic exit.

## 6. Required API Adapter

Create a provider-neutral `LLMClient` boundary rather than embedding DeepSeek calls in `Thinker`:

```text
LLMClient.complete_json(task, system_prompt, user_prompt, budget) -> LLMResult
```

`LLMResult` should include:

- parsed payload and raw response hash;
- provider, model, task, request ID;
- input/output/total/cache token counts;
- estimated cost and pricing snapshot ID;
- latency, HTTP status, retry count, peak/off-peak flag;
- finish reason;
- validation result and degradation reason.

Adapters:

- `deepseek`: direct `/chat/completions`, JSON response format, non-thinking mode, bounded timeout;
- `groq`: existing OpenAI-compatible social adapter, enhanced with usage capture and structured-output validation;
- `template`: deterministic local implementation, never billed and always available.

Keep API keys server-side in `.env`; redact keys, authorization headers, prompts containing sensitive tokens, and raw bearer values from logs. Store hashes or truncated request metadata where practical.

## 7. Feedback and Self-Improvement Design

### Stage A: observability before adaptation

Add an `llm_call_usage` repository table in both `api/db.py` and `api/db_pg.py` with one row per request. Add fields for task, provider, model, candidate mint, tick ID, outcome status, usage, price snapshot, cost, latency, cache status, and degradation reason.

Add a stable `model_version` and `prompt_version` to feed events and decision commits. This makes model comparisons possible without rewriting history.

### Stage B: outcome labels

For every thinker decision, calculate delayed counterfactual labels at fixed horizons, for example 5m, 15m, 1h, 6h, and 24h:

- maximum favorable excursion;
- maximum adverse excursion;
- net return after configured fees/slippage;
- liquidity and volume deterioration;
- whether the deterministic gate would have entered;
- whether the model said buy or pass;
- whether the actual paper position entered and its exit reason.

These labels must be append-only and must use the original candidate snapshot plus later market observations. Do not relabel old decisions using current thresholds.

### Stage C: model scorecard

Report separately for thinker `buy` and `pass`:

- precision of buys at each horizon;
- missed-opportunity rate for passes;
- false-positive loss and false-negative opportunity;
- calibration by liquidity, age, crowd heat, regime, discovery source, and peak/off-peak period;
- p50/p95 latency;
- valid JSON rate;
- fallback rate;
- tokens and USD per accepted entry, rejected candidate, and closed trade.

The model is useful only if it improves risk-adjusted paper outcomes without exceeding the latency and cost budget. A lower raw win rate may still be acceptable if drawdown and adverse excursion improve; the scorecard must show both.

### Stage D: human-approved experiments

The learning loop may produce proposals such as:

- change one rule threshold;
- change candidate priority budget;
- change thinker model or prompt version;
- change social model;
- increase or decrease a research cap.

Each proposal needs an experiment ID, reason, sample size, expected metric, approval status, start/end timestamps, and rollback value. Applying a proposal remains a manual action during paper calibration. No API response, reflection, or daily report may edit `config.py`, activate trading, or alter an open position.

## 8. Rollout Plan

1. **Baseline capture:** record seven days of current Qwen results, usage estimates, latency, model veto rate, and paper outcomes. Preserve the existing calibration window.
2. **Adapter and accounting:** implement the provider-neutral client, DeepSeek adapter, usage table, pricing snapshot, redaction, timeout, circuit breaker, and tests against mocked OpenAI-compatible responses.
3. **Shadow mode:** call DeepSeek for the same candidate snapshot but do not use its verdict. Compare JSON validity, latency, token cost, thesis grounding, and agreement with Qwen/template.
4. **Replay evaluation:** run both models over sealed historical snapshots and the DONT corpus. Measure precision, adverse excursion, missed upside, and cost. No live API result may mutate historical trades.
5. **Paper canary:** route only a configurable fraction of thinker calls to DeepSeek while the deterministic gate and all exits remain unchanged. Keep provider/model IDs in every decision record.
6. **Promotion within paper:** switch the default thinker to V4 Flash only if it meets the acceptance gates below for a complete observation window.
7. **Background migration:** move reflections and daily summaries to an off-peak queue after hot-path accounting is stable.
8. **Review:** update stale docs that still claim narration-only Qwen, then update `handoff.md`, `activeContext.md`, and the project report with measured results.

## 9. Acceptance Gates

Do not make DeepSeek the default thinker until all are true:

- at least 99% valid structured responses in shadow/canary mode;
- p95 thinker latency does not cause the tick loop to miss its operating budget;
- fallback works with provider down, timeout, malformed JSON, 429, and quota exhaustion;
- no API failure can open, close, size, or modify cash;
- token usage and estimated cost are present for every attempted request;
- grounding and audit fields remain queryable;
- paper outcome scorecard is no worse than the Qwen baseline on the agreed risk metrics, or an observed improvement is documented;
- total daily LLM spend remains below the configured budget;
- backend and combined test suites pass, including SQLite and Supabase repository parity;
- live execution remains disarmed and `backend/` still has no import of `live_execution`.

## 10. Immediate Next Tasks

1. Add usage/model/prompt version fields and repository functions in both database backends.
2. Extract a shared OpenAI-compatible JSON client with DeepSeek and Groq adapters.
3. Add token/cost/latency counters to `/api/system-status` and a daily cost section to learning output.
4. Add shadow-mode thinker comparison and replay tests using sealed snapshots.
5. Add delayed outcome-label generation for thinker decisions.
6. Keep Groq for social and benchmark it independently from thinker quality.
7. Run the current calibration window to completion before changing thresholds or interpreting model superiority.
8. Update stale architecture/report wording after measured migration results, not before.

## 11. Non-Negotiable Constraints

- `PAPER_TRADING_ONLY = True` remains hardcoded and runtime asserted.
- Deterministic rules and numeric exits retain ownership of trading state.
- Model `buy` is at most a veto/input, never sufficient by itself.
- Unknown market data remains unknown; no model may fill missing values.
- Atomic write-then-rowcount-then-cash behavior is unchanged.
- Provider failures degrade to a deterministic result and are recorded loudly.
- No automatic threshold, prompt, model, or live-trading promotion changes.
- API keys, bearer tokens, and sensitive raw prompts do not enter logs.
