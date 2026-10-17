# §81 Mutation Log

This document records the mutation verification discipline executed across all phases of Master Prompt §81.
For every refusal rule, security barrier, and classification branch introduced or modified, the active enforcement line was temporarily removed/inverted to confirm test failure, and subsequently restored.

| Phase | Target File | Mutation Description (Line Removed / Altered) | Test That Failed | Observed Failure Result |
|---|---|---|---|---|
| **§81-A** | `backend/live_execution/exit_errors.py` | Removed `custom_error_code(onchain_err) in EXIT_RETRYABLE_CUSTOM_ERRORS` | `test_sim_slippage_ladder_escalates_and_fills` | Ladder halted at rung 1 (50 bps) with guard refusal; did not escalate |
| **§81-A** | `backend/live_execution/executor.py` | Removed landed-with-error bypass of `is_blockhash_valid` | `test_landed_with_retryable_error_does_not_call_is_blockhash_valid` | Returned `exit_unresolved` instead of escalating to next ladder rung |
| **§81-A** | `backend/live_execution/solana.py` | Removed check enforcing all attempts return definitive `rpc_error` before raising `SendRejected` | `test_preflight_ambiguous_transport_failure_halts` | Treated transport drop as conclusive rejection instead of ambiguous fail-closed |
| **§81-B** | `backend/live_execution/reconcile.py` | Reverted `usd_size = r.get("usd_size")` back to `r.get("proceeds_usd")` | `test_check_wallet_drop_no_alarm_on_legitimate_sell` | False alarm `drop_detected=True` triggered on normal sell |
| **§81-B** | `backend/live_execution/reconcile.py` | Removed independent SOL channel drop check | `test_check_wallet_drop_detects_sol_drain` | Undetected SOL drain (`drop_detected=False`) |
| **§81-B** | `backend/live_execution/reconcile.py` | Removed `if current_usdc is None or current_sol is None` check | `test_check_wallet_drop_fails_closed_on_none_balance` | Failed open or crashed instead of returning `skipped=True, reason="balances_unreadable"` |
| **§81-C** | `backend/live_execution/sim_guard.py` | Removed `created_acct["delegate"] is not None` refusal check | `test_create_account_with_delegate_refused` | Simulation passed malicious delegate creation instead of refusing |
| **§81-C** | `backend/live_execution/solana.py` | Removed control-field equality checks in `responses_agree` | `test_responses_agree_control_fields` | Quorum allowed divergent token delegates across RPC nodes |
| **§81-C** | `backend/live_execution/trade_intent.py` | Changed `ROUND_HALF_UP` to `ROUND_FLOOR` in `usd_to_raw_amount` | `test_usd_to_raw_amount_property_cents` | Discrepancy on boundary cent amounts (0.01..100.00) |
| **§81-C** | `backend/live_execution/sim_guard.py` | Removed `count=False` suppression on `QuoteMismatch` in `_refuse` | `test_quote_mismatch_single_counter_increment` | Safety refusal counter incremented twice for single quote mismatch |
| **§81-D** | `backend/live_execution/quote_params.py` | Removed `if side == "buy"` so sells also filtered dexes | `test_build_quote_params_sell_never_filters_dexes` | Sells included `dexes` query parameter, risking liquidity lockup |
| **§81-D** | `backend/live_execution/config.py` | Re-added unverified Lifinity program IDs to `APPROVED_SWAP_PROGRAM_IDS` | `test_lifinity_unverified_ids_absent` | Static assertion failed verifying unverified IDs absent |
| **§81-D** | `backend/api/routes/system_status.py` | Removed `dex_filter` status field from `/api/system-status` response | `test_system_status_dex_filter_visibility` | Key absent in response payload |
| **§81-E** | `scripts/restore_state.py` | Removed `if ".." in member.name or member.name.startswith("/")` check | `test_restore_rejects_path_traversal` | In-memory Pass 1 allowed path traversal archive |
| **§81-E** | `scripts/restore_state.py` | Removed post-restore `kill_switch.trip(...)` call | `test_restore_trips_kill_switch` | Kill switch was not tripped following restore |
| **§81-E** | `scripts/restore_state.py` | Removed `PRAGMA integrity_check;` validation on staged SQLite DB | `test_restore_corrupt_db_aborts` | Corrupt database promoted to target instead of aborting |
| **§81-F** | `backend/conftest.py` | Removed tree hash comparison hook in `pytest_sessionfinish` | `test_hermetic_state_dir` | State directory modification allowed without test failure |
| **§81-F** | `.gitleaks.toml` | Re-added `.github/workflows/tests.yml` to path allowlist | `test_gitleaks_configuration_honest` | Test failed asserting zero allowlisted workflow paths |
| **§81-G** | `scripts/dry_run_guard.py` | Removed state dir isolation check (`LIVE_EXECUTION_STATE_DIR` unset without `--allow-live-state`) | `test_dry_run_guard_state_isolation_protection` | Script ran against live state dir without protection |
| **§81-G** | `backend/live_execution/jupiter_client.py` | Removed `x-api-key` header attachment in `get_jupiter_headers` | `test_jupiter_client_api_key_header` | Request headers lacked `x-api-key` when key configured |
| **§81-G** | `backend/live_execution/sim_guard.py` | Removed required quote field check in `validate_quote_against_intent` | `test_quote_contract_fail_closed` | Malformed quote passed instead of raising `QuoteMismatch` |
| **§81-H** | `backend/alerts.py` | Removed `topic.lower() in FORBIDDEN_NTFY_TOPICS` check in `is_ntfy_misconfigured` | `test_h1_ntfy_guessable_topic_refused` | Insecure topic reported `alerts: "ok"` on public ntfy |
| **§81-H** | `backend/data_providers/onchain_security.py` | Removed `_MINT_EXTENSION_CACHE.popitem(last=False)` in `_put_extension_cache` | `test_h2_mint_extension_cache_bounded` | Cache grew unbounded to 3000 items instead of capping at 2048 |
| **§81-H** | `backend/api/routes/stats.py` | Reverted merged pricing loop to separate passes | `test_h3_stats_single_pass_pricing` | `get_current_price` called 2N times instead of N times for N positions |
| **§81-H** | `frontend/src/App.tsx` | Removed primary feed error checks from `riskState` | `test_h4_frontend_offline_condition_documented` | Primary feeds failed to gate offline condition |
| **§81-H** | `backend/sizing.py` | Removed `(0.0, "security_unknown_below_floor")` return on below-floor haircut | `test_h5_compute_ticket_security_unknown_below_floor_reason` | Sizing emitted `None` instead of distinct refusal reason |
| **§81-H** | `scripts/gate_funnel_report.py` | Removed `_is_security_unknown_below_floor(r)` counting | `test_h5_gate_funnel_report_counts_security_unknown_below_floor` | Diagnostic report reported 0 blocked candidates instead of 2 |

