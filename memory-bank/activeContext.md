# Active Context — trading-bot

**As of 2026-08-22 (post Tasks A+B implementation).** Full detail:
`handoff.md` (root). **NOTE: repo moved to
`/home/hixam/Downloads/Projects/trading-bot/`** — old path deleted.

## Current focus
Tasks A (dual-lens discovery + thesis reuse) and B (num_ctx + provider
shutdown) are IMPLEMENTED but PENDING VERIFICATION — the integrated terminal
died when the old workspace path was deleted, so pytest could not run yet.
`verify_tasks.sh` at repo root runs the full check; a fresh terminal session
should execute it, then commit/push.

## Task C BLOCKED
`live_execution/jupiter_executor.py` does not exist in this repo (verified
root + backend/, plus git history). It presumably lives in the separate
real-money project. Waiting for: its path, or pasted contents, or explicit
descoping. Do NOT create a swap-signing module from scratch in this repo.

## Implementation state (uncommitted)
- models.Candidate.discovery_source ("trending"|"new_listing"|"both"|
  "unknown"), observability-only; mock tags all three
- data_providers/new_listings.py NEW: SUBSCRIBE_TOKEN_NEW_LISTING ws feed,
  buffered drain, session auto-disable after 3 failed connects
- live.py dual-lens merge ("both" on overlap) + feed lifecycle in aclose
- llm/reuse.py reused_if_stable (5%/10%/3pp delta table w/ abs noise floors)
  + main.py cross-tick thesis cache wired into run_tick(state)
- config.OLLAMA_NUM_CTX=1024 (+rationale); narrator sends num_ctx
- main() finally closes provider AND narrator (B.2 fixed)
- tests/test_discovery_isolation.py + tests/test_narration_reuse.py added
- Task C IMPLEMENTED: live_execution/ package (config, jupiter_executor w/
  lite-api + decimals fail-closed refusals + local signing via solders +
  RPC send/confirm; CLI entry), offline tests added. VERIFY pending shell.

## Verification checklist (run verify_tasks.sh)
pytest all-pass · mock shows trending/new_listing/both · no rule references
discovery_source · reuse thresholds sanity · num_ctx grep · provider-close
grep · ollama prompt_eval_count measurement (raise num_ctx to 2048 only if
measured >700) · live_execution offline tests · backend↮live_execution
isolation grep. Needs `pip install solders` in .venv for signing later.

## Watch-outs
- websockets lib needed by new_listings (ships with uvicorn[standard] ✓)
- New-listing WS may require a Birdeye plan that includes BDS feeds; free
  key → lens auto-disables, trending-only continues (by design)

