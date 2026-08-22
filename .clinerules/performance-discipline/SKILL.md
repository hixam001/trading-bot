---
name: performance-discipline
description: Use this skill when writing or reviewing any code in the trading-bot project that touches network I/O, the tick loop, LLM calls, or the API/frontend data path. The system runs on modest local hardware (6GB VRAM GPU) and needs to score multiple candidates per short tick interval without becoming the bottleneck.
---

# Performance discipline for trading-bot

The system's speed ceiling is the local LLM (Qwen3-8B via Ollama, ~23 tok/s
on the target hardware) and external API rate limits — not raw CPU. Every
other part of the pipeline should be fast enough to never be the bottleneck
next to those two. Optimize accordingly: don't spend effort speeding up
things that are already far from the critical path, and don't let avoidable
overhead stack on top of the LLM/API latency that's already there.

## Core rules

1. **Async I/O for all network calls.** Data ingestion (Birdeye/Jupiter/etc.)
   and Ollama calls should use `asyncio` + `httpx.AsyncClient` (or equivalent)
   rather than blocking `requests` calls in the tick loop, so multiple
   candidates can be fetched/scored concurrently instead of serially. This is
   the single highest-leverage performance change available given the LLM is
   already the bottleneck per-call — running independent calls concurrently
   is free wall-clock time.

2. **Reuse connections.** Use a single persistent `httpx.AsyncClient` (or
   session) per process for each external service, not a new client per
   request — TCP/TLS handshake overhead is disproportionately expensive at
   the frequency this system calls out.

3. **Batch where the API allows it.** If a data provider supports fetching
   multiple tokens in one call, prefer that over N sequential single-token
   calls. Check each provider's docs for batch/bulk endpoints before writing
   a per-candidate loop.

4. **Avoid N+1 patterns against the database.** The API layer (FastAPI +
   SQLite) should fetch what a page needs in one query with appropriate
   `LIMIT`/`WHERE` clauses, not one query per row. Add indexes on columns
   used in `WHERE`/`ORDER BY` for the feed, journal, and stats queries
   (`ts`, `is_open`, `closed_at` at minimum).

5. **The tick loop must never block the API from serving requests.** Run
   them as separate async tasks or separate processes sharing the SQLite
   store (per the PRD) — a slow LLM call must not make the dashboard feel
   unresponsive.

6. **Don't over-fetch.** Only request the fields/candidates actually needed
   for the current tick's filter pass; don't pull large historical windows
   from an API when a narrower query would do.

7. **Measure before optimizing.** Add lightweight timing (e.g. log the
   duration of each tick's phases: fetch, filter, LLM score, execute) so
   future performance work targets the actual bottleneck rather than a
   guessed one. Do not add caching, threading, or other complexity to a path
   that hasn't been shown to be slow.

8. **Prompt length matters for LLM latency.** Keep the knowledge-base context
   injected into each scoring prompt (static knowledge + similar-trade
   retrieval) as concise as the task allows — longer prompts cost real
   tokens/sec on a local 8B model. Prefer summarized, bulleted context over
   verbose prose when building prompts programmatically.

## Anti-patterns to reject in review

- Sequential `requests.get()` calls in a loop over candidates where an async
  gather would work and the API supports concurrent requests.
- Creating a new HTTP client/session inside a function that's called
  repeatedly, instead of reusing one at module or app scope.
- Fetching an entire table into Python to filter/sort in application code
  when the same filter/sort could be pushed into the SQL query.
- Adding a cache, background worker, or other complexity as a first
  response to a performance concern, before measuring where time is
  actually going.
