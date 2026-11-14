# 11 — §93 root filesystem audit — 2026-11-09

Recovered task: OpenCode session `ses_e4103b0c7ffeztj1LVbtumgb3N`.
R1 was committed (`7c6f904`); R2 was interrupted. This continuation preserved
that work, completed R2–R8 and repaired inherited R1 verification gaps.
Production wallet, keys and ledger were not used. Runtime tests use temporary
state, synthetic credentials and mocked external transports.

The recovered baseline was 1,746 passed / 5 environmental skips; the §92
handoff records 1,751 with a frontend build. This workspace has that build.

## R1 — writable runtime homes

| ID | Severity | File:line | Evidence (failing → passing) | Fix | Test |
|---|---|---|---|---|---|
| F1 | High | `scripts/audit_runtime_writes.py:599` | Recovery: 12 failed → R1 regressions pass | Strict AST/site inventory; dependent defaults and independent state/log overrides resolved; new unclassified writers fail | `test_new_write_in_existing_file_is_not_implicitly_approved` |
| F1 | High | `scripts/preflight_deploy.py:717` | Expanded-mount/traversal/nested-RO tests failed → pass | Parse expanded and short mounts; normalize paths; most-specific mount wins | `test_preflight_accepts_expanded_mounts_and_rejects_dotdot_escape`, `test_nested_readonly_mount_overrides_a_writable_parent` |
| F1 | High | `scripts/rofs_smoke.py:34` | State-preservation and CI-plan tests failed → pass | Exclusive random sibling probes; compose-derived disposable CI volumes/env/hardening | `test_smoke_preserves_existing_state_and_resolves_compose_defaults`, `test_ci_smoke_mounts_and_env_are_derived_from_compose` |
| F1 | High | `docker-compose.yml`, `Dockerfile` | Both simulated-EROFS role tests pass | Writable homes, uid 10001 and 0700 directories; container controls retained; backups exclude relocated secrets and include blocklist | `test_live_writes_succeed_under_simulated_erofs`, `test_api_writes_succeed_under_simulated_erofs`, `test_backup_denylist_excludes_new_secret_paths_and_includes_blocklist` |

Observed recovery output:

```text
FAILED ...::test_smoke_preserves_existing_state_and_resolves_compose_defaults[api]
FAILED ...::test_ci_smoke_mounts_and_env_are_derived_from_compose
12 failed, 7 deselected in 0.82s
```

Docker is unavailable locally. CI actually built the ARM64 and native images
and passed the compose-derived, network-disabled real-container smokes for
both roles: [verified run 42665808979](https://github.com/hixam001/trading-bot/actions/runs/42665808979). No local container run is claimed.
The deployment note migrates existing state before starting the signer and fixes
ownership after docker cp. Smoke never writes to configured state filenames.

## R2 — persistence failures and TLS identity

| ID | Severity | File:line | Evidence (failing → passing) | Fix | Test |
|---|---|---|---|---|---|
| F1 | High | `backend/blocklist.py:131`, `backend/api/auth.py:173` | Inherited error-handler NameError reproduced → pass | Non-fatal ERROR logs; hourly state alerts; revocation INTERNAL_ERROR event; Privy-specific ERROR assertion | `test_blocklist_write_failure_logs_error_and_alerts_hourly`, `test_session_revocation_write_failure_is_loud`, `test_privy_persist_failure_is_loud` |
| F1 | High | `backend/api/db_pg.py:103` | Pool verifier absent / known-pin downgrade tests failed → pass | Memory fallback; operator pin; actual TLS handshake pin before DB authentication and pool-init defense; known-pin probe failure refuses connection | `test_unwritable_tls_pin_is_held_in_memory_and_verified`, `test_pin_is_enforced_during_the_tls_handshake`, `test_operator_pin_is_checked_before_database_auth` |
| F1 | High | `backend/api/db_pg.py:83` | PostgreSQL negotiation tests failed → pass | Send SSLRequest before certificate probe, matching asyncpg | `test_certificate_probe_negotiates_postgres_starttls` |

Observed failures:

```text
NameError: name 'logging' is not defined
AttributeError: module 'api.db_pg' has no attribute '_verify_pool_connection'
FAILED ...::test_known_pin_cannot_fall_back_when_probe_fails
FAILED ...::test_operator_pin_is_checked_before_database_auth
FAILED ...::test_pin_is_enforced_during_the_tls_handshake
```

Final R2 targeted run: **13 passed**. In-memory TLS client/server handshakes
exercise matching and mismatching certificates without network sockets. The
first mutation run caught 135/136: Privy's log downgrade survived because its
test accepted another module's ERROR. Strengthening the assertion caught it in
the complete rerun; no production behavior was weakened.

## R3 — API-only credential startup gate

| ID | Severity | File:line | Evidence (failing → passing) | Fix | Test |
|---|---|---|---|---|---|
| F2 | High | `backend/config.py:160`, `backend/api/main.py:58`, `backend/docker-entrypoint.sh:81` | Both weak-secret imports fail on archived R1 → pass on final code | Clear invalid values, record reasons and CRITICAL logs; reject API/combined startup only | `test_weak_credentials_do_not_prevent_live_import`, `test_api_lifespan_refuses_invalid_credentials_before_db`, `test_entrypoint_rejects_weak_secrets_only_for_api` |

Baseline reproduction used an archived R1 checkout in a throwaway directory:

```text
FAILED ...::test_weak_credentials_do_not_prevent_live_import[ADMIN_TOKEN]
FAILED ...::test_weak_credentials_do_not_prevent_live_import[SESSION_SECRET]
2 failed in 0.12s
RuntimeError: ADMIN_TOKEN has insufficient entropy
```

Existing S2 tests now exercise the API gate and retain their boot-refusal intent.
The live-only entrypoint skips that gate; no wallet or live loop was started.

## R4 — public signatures survive redaction

| ID | Severity | File:line | Evidence (failing → passing) | Fix | Test |
|---|---|---|---|---|---|
| F3 | Medium | `backend/public_registry.py:19`, `backend/redaction.py:108` | Signature/parameter failures reproduced → 74 targeted/adversarial/memo/wallet tests pass | Thread-safe 5,000-entry LRU; secrets replaced before public exemptions; generic parameters limited to query/header contexts | `test_registered_signature_round_trips_logs_and_alerts_with_wallet_secret`, `test_public_registration_cannot_shield_secrets` |
| F3 | Medium | `backend/live_execution/solana.py`, `backend/live_execution/executor.py`, `backend/run_live_cycle.py` | Actual mocked FILLED/pending receipt flows pass; 119 execution regressions pass | Register signatures at production/receipt before logs and alerts | `test_actual_filled_log_contains_full_signature`, `test_pending_buy_alert_has_full_registered_signature` |

Observed failing evidence includes:

```text
FAILED ...::test_actual_filled_log_contains_full_signature[buy]
FAILED ...::test_generic_parameter_names_do_not_mask_plain_diagnostics[token=BONK rank]
FAILED ...::test_credential_parameters_and_header_values_stay_masked[Authorization: Bearer token=abc123456789]
```

Unchanged adversarial tests still mask unregistered 80–90-character base58 runs.
Configured/runtime/extra secrets remain masked even if registered public.

## R5 — normalized ticker keys

| ID | Severity | File:line | Evidence (failing → passing) | Fix | Test |
|---|---|---|---|---|---|
| F4 | Medium | `backend/llm/llm_brain.py:401` | 16 failed / 9 passed → 48 R5/brain tests pass | Shared prompt/parse/lookup normalization, including rebound set; collisions/placeholders excluded before bounding; one count-only INFO per tick | `test_displayed_symbol_buy_verdict_round_trips`, `test_rebounded_board_retains_displayed_key`, `test_colliding_tickers_get_no_brain_verdict`, `test_live_cycle_uses_candidate_mint_never_model_mint` |

Observed output:

```text
FAILED ...::test_live_cycle_uses_candidate_mint_never_model_mint[\u0421\u041e\u0418\u041d]
FAILED ...::test_live_cycle_uses_candidate_mint_never_model_mint[\uff30\uff25\uff30\uff25]
16 failed, 9 passed in 0.17s
```

Every verdict-map consumer was searched. The runner uses verdict_for; its mint
comes from the screener candidate, never the model's text.

## R6 — body bounds and spend visibility

| ID | Severity | File:line | Evidence (failing → passing) | Fix | Test |
|---|---|---|---|---|---|
| F6 | Medium | `backend/api/main.py:220`, `backend/config.py:231` | 4 failed / 1 passed initially; escaped-JSON case also reproduced → pass | 1 MiB global cap; dedicated ingest cap includes UTF-8 and JSON-escape overhead; >4 MiB preflight WARN | `test_default_one_mib_plus_one_rejected`, `test_max_ingest_uses_path_override`, `test_max_document_reaches_authenticated_ingest`, `test_ingest_chunked_over_limit_rejected` |
| F5 | Medium | `scripts/spend_projection.py:107`, `scripts/preflight_deploy.py`, `backend/run_live_cycle.py` | Missing projection/startup-log/mixed-case regressions failed → pass | Offline per-provider scenarios, startup effective caps, preflight WARN; caps unchanged | `test_hand_computed_live_brain_social_search_and_narrative_budget`, `test_env_provider_and_backend_normalization_matches_runtime`, `test_preflight_projection_warns_and_passes` |

Observed output:

```text
FAILED ...::test_default_one_mib_plus_one_rejected
FAILED ...::test_max_ingest_uses_path_override
FAILED ...::test_ingest_chunked_over_limit_rejected
FAILED ...::test_preflight_warns_above_four_mib
4 failed, 1 passed in 0.13s
```

The global limit honors environment configuration. Declared and chunked bodies
still receive 413 before parsing. Ingest's default bound is 2,465,536 bytes:
`4 * MAX_INGEST_CHARS` UTF-8 allowance, up to eight additional bytes per
character for JSON surrogate escapes, plus 65,536 metadata bytes. Maximum
escaped documents reach the authenticated real route with storage mocked.
Larger batches must be split to fit the aggregate request limit.

## R7 — consistent trust, concealment, and documentation

| ID | Severity | File:line | Evidence (failing → passing) | Fix | Test |
|---|---|---|---|---|---|
| F7 | Low | `backend/config.py`, `backend/api/auth.py`, `backend/api/main.py`, `backend/api/rate_limiter.py` | Synthetic production trust failed → pass | One pytest-only helper in all four consumers, including forwarded synthetic peers | `test_synthetic_hosts_are_never_trusted_outside_tests` |
| F8 | Low | `backend/api/routes/auth.py:193` | Empty/malformed/wrong-type signup returned 422 → 404 | Conceal before validation; opted-in validation preserved | `test_signup_is_concealed_before_body_validation`, `test_input_validation.py` |
| F9 | Low | `backend/tests/test_route_matrix.py:113` | 48 matrix tests pass; removed live-book auth mutation is caught | Probe real documented postures with no credentials and fresh limiter/auth state; explicit no-cookie logout exemption | `test_matrix_auth_matches_the_real_app` |
| F10 | Low | `docs/10_SECURITY.md` | Backup contents and actual write-failure semantics compared with docs | Correct revocation backup exclusion and silent-write residual | `test_audit_93_r1.py`, documentation review |

Observed output:

```text
FAILED ...::test_signup_is_concealed_before_body_validation[empty]
FAILED ...::test_signup_is_concealed_before_body_validation[malformed-json]
FAILED ...::test_signup_is_concealed_before_body_validation[wrong-content-type]
11 failed, 5 passed in 0.15s
```

R7 regression neighborhood: 80 passed. Matrix covers all documented rows,
including websocket/mount behavior and the idempotent logout exemption.

## R8 — documentation and verification records

| ID | Severity | File:line | Evidence (failing → passing) | Fix | Test |
|---|---|---|---|---|---|
| F1–F10 | Follow-through | `docs/10_SECURITY.md`, `docs/03_DEPLOYMENT.md`, `handoff.md`, `memory-bank/` | Missing/stale records → generated inventory, findings, migration, controls, residuals and observed evidence | Record limitations explicitly | Full suite, route-matrix docs checks, inventory check |
| Critical fixes | Verification | `scripts/mutation_check.py`, `docs/09_MUTATION_LOG.md` | First run 135/136 → final **137/137 caught**, zero survivors | New mutations and refreshed old anchors; no-flags command regenerates the log only on success | Full mutation harness; declaration integrity test |

## Spend projection and recommendation

Example: 60-second interval, 20 candidates, cross-pool research head 8, live
mode, placeholder DeepSeek/Groq/social keys and only SearXNG enabled. This is
a documented demand scenario, not measured utilization or real credential use.
Maximum 1,440 ticks/day; the actual scheduler sleeps after completing work.

| Provider | Metric | Typical/day | Stress/day | Existing cap |
|---|---|---:|---:|---:|
| DeepSeek | LLM calls | 1,453 | 37,443 | 2,000 |
| DeepSeek | Tokens (estimate) | 9,197,764 | 129,403,476 | 2,000,000 |
| DeepSeek | Cost USD (estimate) | 3.2914 | 68.2921 | 5.00 |
| Groq | LLM calls | 2,880 | 30,240 | 2,000 |
| Groq | Tokens (estimate) | 2,016,000 | 58,752,000 | 2,000,000 |
| Groq | Cost USD (estimate) | 2.5344 | 83.8656 | 5.00 |
| Web | Search attempts | 2,880 | 28,800 | 500 |
| Dexscreener | Cross-pool HTTP | 11,520 | 11,520 | Outside this guard |

Total LLM calls: **4,333 typical / 67,683 stress per day** across providers.
Typical assumes two cleared candidates/tick, a successful brain covering up to
eight, first search hop success without cache savings, three positions restated
every six hours, one close/day. Stress assumes missing brain verdicts, every
candidate passing, all fallback/search hops, stale restates retrying each tick,
and full book replacement. Research is Dexscreener HTTP, not an LLM call.
Token/cost estimates use assumed input lengths and repository pricing; stress
uses DeepSeek peak prices and no input-cache discount. They are not strict
upper bounds. Social shares the Groq counter; caps apply per provider.
API digests have a separate counter and are excluded from this projection.

I would first raise LLM_DAILY_TOKEN_CAP after measuring prompt sizes: 2,000,000
is well below this typical DeepSeek estimate. If social and SearXNG remain
enabled, review LLM_DAILY_CALL_CAP and SEARCH_DAILY_CALL_CAP too (this scenario
needs at least 2,880/day before headroom). **No cap was raised.** The operator
decides the spend budget; do not blindly apply the CLI's stress suggestions.

## Final verification

- Canonical `./scripts/prepush_check.sh`: all nine steps completed successfully.
  Executed with a clean environment, DATA_BACKEND=mock and temporary DB/state.
  An initial wrapper incorrectly set SPEND_GUARD_FILE across tests; removing
  that test-isolation conflict produced the passing canonical run.
- Backend: **1,934 passed**, 0 failed, 0 skipped, 102 existing deprecation
  warnings (the same warning count as §92). This exceeds the recovered
  1,746 + 5 environmental skips baseline and the built-frontend 1,751 baseline.
- Mutation: **137/137 caught**, 0 survivors; all source restorations verified;
  `python scripts/mutation_check.py` with no flags regenerated the log.
- Playwright Chromium: **41 passed** against a local mock backend (14.7 s).
- `pip check`: no broken requirements. `pip-audit` and `npm audit --omit=dev`:
  no known vulnerabilities / zero vulnerabilities.
- TypeScript `tsc --noEmit`, production `npm run build`, frontend secret-bundle
  scanner, changed-module pyflakes, inventory `--check` and `git diff --check`
  passed. Python dependency tooling and browser verification ran outside the
  sandbox because DB-backed tests hung inside its restricted environment.
- Initial local gitleaks step was skipped because no binary was installed.
  CI then flagged only the synthetic URL/header credential strings in the R4
  tests. Six exact commit/file/line fingerprints were added to .gitleaksignore;
  no scanning rule or general path was excluded. The official CI-pinned 8.24.3
  binary was downloaded to /tmp, verified against release SHA-256 checksums,
  and a redacted full-history scan passed with no leaks. The canonical gate
  was repeated with that binary on PATH.
- **Verified green full CI** on application commit b58de47:
  [Tests & Security Audit 42665808979](https://github.com/hixam001/trading-bot/actions/runs/42665808979).
  Backend/frontend tests and audits, ARM64 image build, native image build,
  and real-container read-only-root smoke for both roles all succeeded.
- **Verified green gitleaks CI** after the exact fixture exclusions on 0a9b40a:
  [gitleaks 42666899068](https://github.com/hixam001/trading-bot/actions/runs/42666899068).
  [Code scanning 42666899034](https://github.com/hixam001/trading-bot/actions/runs/42666899034)
  also succeeded. Application source is unchanged between b58de47 and 0a9b40a;
  that follow-up changes only scan fingerprints and documentation.

## UNVERIFIED

- Docker is not installed locally. Container verification was observed in
  the successful CI run, including fresh named-volume write paths for both
  roles; no production VM migration or local Docker execution is claimed.
- Actual VM migration, reboot, production backup/restore, real wallet and
  Supabase certificate/rotation behavior were not contacted or exercised.
- Current provider pricing and live traffic were not measured. Estimates use
  the repository snapshot and stated assumptions.

## Behaviour changes

1. The loss-memory/auto-block control now persists.
2. API-side LLM calls use their own spend counter.
3. Body cap default is 1 MiB.

CI follow-up: the extra §93 R8 commit records the exact synthetic-fixture exclusions after the first pushed scan; published phase commits were not rewritten.

The final documentation follow-up records these observed successful CI runs; it changes no application code.
