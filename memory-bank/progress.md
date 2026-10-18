# Progress — trading-bot

## Works (all verified)
- [x] CI Pipeline & Pytest Path Resolution Fix (2026-10-18):
      1. Added `pythonpath = . backend` to `pytest.ini` and ensured `_ROOT` is in `sys.path` in `backend/conftest.py` for bare `pytest` invocations.
      2. Ensured `backend/live_execution/state` directory is created on fresh checkouts before hermeticity snapshot.
      3. All GitHub Actions workflows (`Tests & Security Audit`, `gitleaks`, `CodeQL`) passing green on `main`. Full suite: **1164 passed**, 0 failed.
- [x] §81 Phase H: Smaller items (2026-10-17):
      1. Alert channel defaults (H-1): `backend/alerts.py` forbids guessable topics (`trading-bot`, `alerts`, etc.) on public `ntfy.sh` without auth; reports `alerts: "misconfigured"` on `/api/safety` status and logs clear errors; added `openssl rand -hex 12` guidance in `.env.example` and `docs/11_DEPLOYMENT.md`.
      2. Bounded mint extension cache (H-2): `backend/data_providers/onchain_security.py` caps `_MINT_EXTENSION_CACHE` to `MAX_MINT_EXTENSION_CACHE = 2048` using `OrderedDict` with LRU eviction on insert.
      3. Merged position pricing (H-3): `backend/api/routes/stats.py` merged duplicate loops over `open_trades` into a single pass feeding both unrealized PnL marks and real equity marks (exactly N calls for N open positions).
      4. Offline banner condition documented & verified (H-4): verified per DESIGN.md §3.3 & §3.4 that secondary feeds render localized panel errors while primary feeds drive global offline banner; added route-mocked Playwright tests.
      5. Unknown-security sizing haircut below floor visibility (H-5): `backend/sizing.py` emits `reason="security_unknown_below_floor"` when 0.5x haircut drops ticket below floor; `run_live_cycle.py` records refusal reason; `scripts/gate_funnel_report.py` counts it on its own line in daily funnel, summary, and text/JSON outputs.
      6. Mutation ledger (H-6): created `docs/17_MUTATION_LOG.md` recording all 27 mutation experiments executed across §81.
      7. Full test suite: **1164 passed**, 0 failed (+6 new tests in `test_audit_81_h.py`).
- [x] §81 Phase G: Real-world validation and API-risk containment (2026-10-17):
      1. Pre-arm & post-upgrade dry run (`scripts/dry_run_guard.py`): operator-run, read-only verification executing quote fetching, transaction assembly, bytecode inspection, and multi-RPC pre-sign simulation with real TradeIntent assertions without keypair (AST verified: zero references to `load_keypair` or signing helpers). Refuses if live state directory is unisolated unless `--allow-live-state` is provided. Documented in `docs/11_DEPLOYMENT.md`.
      2. Centralized Jupiter API client (`backend/live_execution/jupiter_client.py`): unified source of truth for URLs and request headers with optional `JUPITER_API_KEY` (`x-api-key`) and `JUPITER_BASE_URL`. Backward-compatible fallback ensuring byte-for-byte identical default requests.
      3. Jupiter API migration evaluation (`docs/16_JUPITER_API_MIGRATION.md`): documented endpoint usage, confirmed developers.jup.ag Metis deprecation and keyless 0.5 RPS limits, analyzed guard assumptions and failure modes under V2, evaluated and rejected Swap V2 adoption due to ALT restrictions, and specified operator verification checklist.
      4. Fail-closed quote contract: `validate_quote_against_intent` strictly fails closed with `QuoteMismatch` if any required quote parameter is missing or malformed.
      5. Full test suite: **1158 passed**, 0 failed (+11 new tests in `test_audit_81_g.py`).
- [x] §81 Phase F: Make tests and CI honest (2026-10-17):
      1. Hermetic tests state boundary: `backend/conftest.py` redirects `LIVE_EXECUTION_STATE_DIR` and `LIVE_STATE_DIR` to a pytest session temp dir before imports. Added `pytest_sessionstart` and `pytest_sessionfinish` snapshot guards asserting zero mutations under `backend/live_execution/state/` across test runs.
      2. Honest Gitleaks config: removed `.github/workflows/tests.yml` path allowlist and dead dummy regexes from `.gitleaks.toml`.
      3. Deterministic E2E tests: removed `EXPECTED_WALLET_ADDRESS` from CI backend command; removed all non-deterministic `.or()` assertions in `frontend/e2e/*.spec.ts`, replacing them with explicit `page.route()` mock fixtures.
      4. Verifiable claims & prepush script: cleaned overclaimed verification lines in `memory-bank/decisionLog.md`; created executable `scripts/prepush_check.sh` running full local gate (pytest, audits, ast, tsc, build, e2e, gitleaks).
      5. Full test suite: **1147 passed**, 0 failed (+5 new tests in `test_audit_81_f.py`).
- [x] §81 Phase E: Make restore safe (2026-10-17):
      1. Multi-pass safe restore architecture: Pass 1 in-memory verification (manifest map completeness, file sizes, sha256 hashes, JSON validity) without writing to disk. Pass 2 atomic staging (`.staged.<pid>.*`) and PRAGMA integrity_check on the staged SQLite copy before promoting. Pass 3 atomic rename via `os.replace`.
      2. Pre-restore rollback snapshot: automatically preserves existing state under `<target_state_dir>/.pre-restore-<timestamp>/` when `--force` is used or target is non-empty.
      3. Mandatory safety latch: always trips the kill switch post-restore (`kill_switch.trip("restored from <archive> at <UTC ts>; run reconcile and clear manually", state_dir=target_state_dir)`), overwriting archive kill_switch.json. Prints clear command (`python backend/live_execution/scripts/confirm_trade.py resume`).
      4. Strict path safety: enforces `member.isfile()`, rejects symlinks/hardlinks/devices/directories, rejects absolute paths and traversal `..`. Only allows `state/*`, `trading_bot.db`, and `MANIFEST.json`.
      5. Full test suite: **1142 passed**, 0 failed (+7 new tests in `test_audit_81_e.py`).
- [x] §81 Phase D: DEX allowlist and label hygiene (2026-10-17):
      1. Removed unverified Lifinity program IDs from `APPROVED_SWAP_PROGRAM_IDS` in `live_execution/config.py` (live check verified they do not appear in Jupiter's program-id-to-label map).
      2. Visible inert filter: WARNING log on startup if labels empty; `dex_filter: "inactive" | "active (N labels)"` exposed on `GET /api/system-status`.
      3. Modernized `scripts/refresh_dex_labels.py`: uses `JUPITER_PROGRAM_LABELS_URL` (`https://lite-api.jup.ag/swap/v1/program-id-to-label`); parses both object and list response shapes; atomic write via tempfile; zero references to dead `quote-api.jup.ag` in active code.
      4. DEX filter applied exclusively to buys (`side="buy"`); sells (`side="sell"`) NEVER send `dexes` to ensure crash/emergency exits are never blocked.
      5. Full test suite: **1135 passed**, 0 failed (+7 new tests in `test_audit_81_d.py`).
- [x] §81 Phase C: Close remaining signing-guard gaps (2026-10-17):
      1. Created account control-field checks: Section E in `sim_guard.py` checks all post-state wallet-owned accounts. Newly created accounts require delegate is None, close_authority is None, delegated_amount == 0, state == 1 (initialized).
      2. Quorum control-field checks in `responses_agree`: exact match on delegate, close_authority, delegated_amount, state.
      3. Quote mismatch single counter & alert: QuoteMismatch(ExecutionError), count=False in _refuse prevents double counting.
      4. Mandatory TradeIntent: removed quote-synthesizing fallback; test helper in `tests/_helpers.py`; AST check ensures production code never imports _helpers.
      5. Consistent USD->raw conversion: `usd_to_raw_amount` in `trade_intent.py` using Decimal and ROUND_HALF_UP; property tested across 10,000 cents.
      6. Reference price check before memo: moved before `logc.seal` and `publish_commit_memo` in `place_buy`.
      7. Priority fee limit scales by non-CB instructions: `min(200_000 * n_non_cb_instructions, 1_400_000)` per Solana runtime default.
      8. Full test suite: **1128 passed**, 0 failed (+18 new tests in `test_audit_81_c.py`).
- [x] §81 Phase B: Fix wallet-drop alarm reliability and ledger flow counting (2026-10-17):
      1. Flow accounting in `reconcile.py`: reads `usd_size` for close records (with `proceeds_usd` fallback).
      2. Two-channel checks (USDC and SOL independently): eliminated hardcoded $150 SOL mark.
      3. Fail-closed on unreadable: skips drop check when USDC or SOL is None without corrupting snapshot.
      4. Logged exceptions instead of swallowing in `run_live_cycle.py`.
      5. Created `scripts/ack_wallet_change.py` with strict keypair isolation (AST verified).
      6. Added `WALLET_DROP_TRIPS_KILL_SWITCH = False` (default alert-only).
      7. Full test suite: **1110 passed**, 0 failed (+8 new tests in `test_audit_81_b.py`).
- [x] §81 Phase A: Make the sell slippage ladder actually escalate (2026-10-17):
      1. Structured exit failure classification (`live_execution/exit_errors.py`): `custom_error_code`, `ExitFailure`, `SendRejected`, `classify_exit_failure`.
      2. SimulationRefused carrying `onchain_err` in `sim_guard.py`. Retries only on confirmed slippage (custom code 6001 / 0x1771 from Jupiter docs).
      3. Preflight RPC error preservation: `send_raw_transaction_detailed` in `solana.py`.
      4. Landed-with-error conclusive resolution: never checks blockhash when error landed on-chain; escalates if retryable, halts if non-retryable without `exit_unresolved`.
      5. Full test suite: **1102 passed**, 0 failed (+36 new tests in `test_audit_81_a.py`).
- [x] Mandatory Pre-Commit and Pre-Push Verification Gate (2026-10-14):
      1. Hard requirement: every commit and push requires all 7 test suites to pass cleanly.
      2. Hermetic backend suite: 1,066 tests passing (0 failures, without `.env`, fake RPC).
      3. Dependency audits: `pip check`, `pip-audit -r backend/requirements.lock`, `npm audit --omit=dev`.
      4. Frontend stability: `tsc --noEmit` and `npm run build` clean (zero errors).
      5. Playwright E2E: 30 tests green against mock backend, resilient to empty-state and populated runs.
      6. Secret hygiene: zero secrets or unallowlisted patterns via `gitleaks git --verbose`.
      7. CI workflow hardened and fully green on `main` (Tests & Security Audit, gitleaks, CodeQL).
- [x] §79 Phase G: Gate funnel diagnostic report (2026-10-13):
      1. Diagnostic script (`scripts/gate_funnel_report.py`) diagnosing why the bot has had zero gate-passers / closed trades since §57.
      2. Reads verbatim `feed_events` (`rule_breakdown`, `failed_rule_ids`) and `trades`.
      3. Computes: daily candidate funnel (seen, gate-passed, think-passed, entered), per-rule fail rate & sole-blocker count, counterfactuals ("if rule X were removed, N candidates would have passed the gate"), top 3 blocking rules failing metric distribution (min, median, p90 vs threshold), and LLM refusal rate among gate-passers with feed verdict mix.
      4. Database support: SQLite (`--db` or default) and Postgres/Supabase via `api.db_pg.get_db()`.
      5. Full test suite: **1066 passed**, 0 failed (+4 new tests in `test_audit_79_g.py`).
- [x] §79 Phase F: Honest observability and configuration consistency (2026-10-13):
      1. Refusal counters (`backend/live_execution/safety_counters.py`): tracks rolling 24h safety refusals (`quote_mismatch`, `sim_refused`, `guard_refused`, `quorum_failed`), atomically stored to `STATE_DIR/safety_counters.json`, surfaced on `GET /api/safety`. Displays `null` (unavailable) when uninitialized.
      2. Real equity metric & honesty on `GET /api/stats`: added `wallet_equity_usd` ($USDC + SOL \times price + open\ positions$) with status (`complete`/`partial`/`unavailable`) and `missing: [...]` list. Preserved API/live boundary via `wallet.public_address()`.
      3. Proxy CIDR parity: standardized `FORWARDED_ALLOW_IPS` to `"127.0.0.1,172.16.0.0/12,::1/128"`; removed broad `10.0.0.0/8` default.
      4. SearXNG digest honesty: deleted unverified digest comment from `docker-compose.yml`.
      5. Frontend & CI: distinct equity labels ("Booked P&L equity" vs "Wallet equity"), refusal counters row, Playwright e2e race-free selectors (`firstRow.or(emptyState)`), and hardened `.github/workflows/tests.yml`.
      6. Full test suite: **1062 passed**, 0 failed (+4 new tests in `test_audit_79_f.py`).
- [x] §79 Phase E: Backups and restore (2026-10-13):
      1. Encrypted state backup script (`scripts/backup_state.py`): packages `executions.json`, `commits.json`, `trading_bot.db`, `kill_switch.txt`, `operator_break.txt` into AES-256-GCM encrypted `.agybkp` archive using `BACKUP_ENCRYPTION_KEY` (sha256-derived). Excludes all secret material. Keeps newest 14 archives.
      2. State restore script (`scripts/restore_state.py`): pre-restore safety checks (refuses if engine running without `--force`), creates automatic `.pre_restore_<ts>` rollback backup, verifies manifest hashes and SQLite integrity.
      3. Backup shell script & cron (`deploy/backup.sh`): executable wrapper loading `.env` and executing backup.
      4. Added `engine_backups` volume in `docker-compose.yml`, `BACKUP_ENCRYPTION_KEY` in `.env.example`, and comprehensive `docs/15_BACKUP_RESTORE.md`.
      5. Full test suite: **1058 passed**, 0 failed (+8 new tests in `test_audit_79_e.py`).
- [x] §79 Phase D: Outbound alerting (2026-10-13):
      1. Outbound push alerting subsystem (`backend/alerts.py`) supporting ntfy and Telegram channels with non-blocking, fail-safe async/sync dispatch via in-process `asyncio.Queue(maxsize=100)` drained by a background worker task with per-call 5s timeout.
      2. Automated secret registration and payload sanitization (masking registered tokens, config secrets, Solana keypairs, Bearer tokens, basic auth credentials).
      3. Deduplication and flood control: per-key cooldown (5m CRITICAL, 15m default), global cap of 20 messages/hour with suppression tracking, and refusal aggregation (first immediately, then 30m aggregate digest).
      4. Wired event hooks: `KILL_SWITCH_ENGAGED`, `DAILY_LOSS_BREAKER`, `SIM_REFUSAL`, `GUARD_REFUSAL`, `EXIT_UNRESOLVED`, `SELL_NO_REF_PRICE`, `RPC_NOT_DIVERSE`, `SIM_SINGLE_RPC_SELL`, `LIVE_BOOT`, `LOW_SOL`, `LOW_USDC`, `WALLET_DROP`, `LOGIN_FAILURE_BURST`, `HEARTBEAT_STALE`/`RECOVERED`.
      5. Created `scripts/send_test_alert.py`, added Section 7 to `docs/11_DEPLOYMENT.md`, and surfaced `outbound_alerts` status on `GET /api/safety`.
      6. Full test suite: **1050 passed**, 0 failed (+16 new tests in `test_audit_79_d.py`).
- [x] §79 Phase C: Token-safety gate improvements (2026-10-13):
      1. Freeze authority live known-bad check: `freeze_authority_revoked is False` blocks in `security_clear`; `None` still passes (preserving B10).
      2. Token-2022 mint-extension screen: added `get_mint_extension_flags` and `parse_mint_extensions` in `data_providers/onchain_security.py` using `getAccountInfo(encoding="jsonParsed")`. Blocks `permanentDelegate`, `transferHook`, `nonTransferable`, `pausableConfig`, `defaultAccountState (frozen)`, and active transfer fee > `MAX_TRANSFER_FEE_BPS` (100 bps = 1.0%). Results cached per mint with 1 hour TTL.
      3. Enriched `Candidate` with `token_program`, `dangerous_extensions`, `transfer_fee_bps`, `transfer_fee_enable`, and `security_unknown`.
      4. Unknown-security sizing haircut: in `sizing.py`, when `security_unknown` (mint or freeze authority unknown or RPC lookup failed), ticket is multiplied by `UNKNOWN_SECURITY_SIZE_MULT = 0.5`. Falls to 0.0 ("no trade") if below minimum ticket floor; never executes below minimum size. Set to 1.0 restores baseline behavior.
      5. Shadow risk flags: computed `top_holder_pct` and `holder_count` buckets on `FeedEvent.risk_flags` without affecting gate verdicts. Added `SHADOW RISK FLAGS` table to `scripts/perf_report.py`.
      6. Full test suite: **1034 passed**, 0 failed (+19 new tests in `test_audit_79_c.py`).
- [x] §79 Phase B: Route hygiene, landing quality, and safe exit retries (2026-10-13):
      1. Unified quote params via `build_quote_params` in `quote_params.py` with `asLegacyTransaction=true`, `swapMode=ExactIn`, and optional `dexes`. Unified across buy, sell, and diagnostic CLI script.
      2. Lifinity program IDs verified (v1 `EewxydAPCCVuNEyrVN68PuSYdQ7wKn27V9Gjeoi8dy3S`, v2 `2wT8Yq49kHgDzXuPxZSaeLaH1qbmGXtEyPy64bL7aD3c`) via official Lifinity documentation and `@jup-ag/lifinity-sdk-v2`, and restored to `APPROVED_SWAP_PROGRAM_IDS`. Added `scripts/refresh_dex_labels.py` and `dex_labels.json`.
      3. Priority fee & Compute budget guard: configured `PRIORITY_FEE_MAX_LAMPORTS = 200_000`, `PRIORITY_LEVEL = "high"`, `PRIORITY_FEE_HARD_CAP_LAMPORTS = 1_000_000`. Wired priority fee payload into `_build_swap_transaction`. In `inspect_swap_transaction`, decoded ComputeBudget instructions (tags 1..4), rejected unapproved tags and duplicates, and enforced `eff_limit * price / 1_000_000 <= PRIORITY_FEE_HARD_CAP_LAMPORTS`. Derived `SIM_MAX_SOL_DECREASE_LAMPORTS = 10_000_000` (rent + priority fee + base fee).
      4. Bounded exit slippage ladder for sells only (`EXIT_SLIPPAGE_LADDER_BPS = (50, 150, 300)` over `EXIT_MAX_ATTEMPTS = 3`). Retries only on retryable slippage/blockhash errors (`is_retryable_exit_error`). Double-fill safety: conclusively resolves previous attempt status (`getSignatureStatuses` + `isBlockhashValid`) before next attempt; halts immediately with `exit_unresolved` if pending or blockhash still valid. Appends notes under existing commit hash via `CommitLog.note`.
      5. Full test suite: **1015 passed**, 0 failed (+12 new tests in `test_audit_79_b.py`).
- [x] §79 Phase A: Make the simulation layer trustworthy (2026-10-13):
      1. Bound pre-sign simulation to local intent via `TradeIntent` (`live_execution/trade_intent.py`), validating quote against locally calculated `in_amount_raw`, mints, slippage, `ExactIn`, no hidden `platformFee`, threshold self-consistency, and independent price floors (`BUY_MAX_PRICE_DEVIATION = 0.10`, `SELL_MAX_PRICE_DEVIATION = 0.50`). Refuses buys without independent mark.
      2. Sealed signing channel: removed `AwaitableSignedTx` and `_sign_transaction`; introduced `sign_after_checks` returning frozen `SignedSwap` with private `_checked=True`; introduced `send_checked_swap` gating broadcast. Eliminated sync unpacking bypass.
      3. Multi-RPC quorum with host deduplication: `responses_agree` validates both SOL lamports (10,000 lamports tolerance) and SPL token balances (50 bps tolerance). Buys strictly require $\ge 2$ agreeing endpoints across distinct hosts (`SIM_MIN_AGREEING_RPCS = 2`). Sells allow single RPC only when `SIM_SELL_ALLOW_SINGLE_RPC = True`. Pre-state fetched across quorum. Added `solana.check_rpc_diversity()`.
      4. Post-state token account control-field validation: asserts `delegate`, `close_authority`, and `delegated_amount` unchanged; asserts `state != 2` (not frozen).
      5. Full test suite: **1003 passed**, 0 failed (+10 new tests in `test_audit_79_a.py`).
- [x] §78 Post-Audit Hardening (Phases A–F) (2026-10-12):
      1. Phase A (Signing Guard Bypass Remediation): Closed reproduced drain/sink/owner bypasses (A1, A2, A3) in `jupiter_executor.py`. Enforced owner checks for Token/Token-2022 tags 1, 16, 18 against payer; proved wallet ownership in execution order; restricted CreateAccount owner to Token/Token-2022, space <= 165 bytes, and lamports <= 2_500_000; enforced 10_000_000 lamports (0.01 SOL) aggregate top-level SOL outflow cap (`MAX_SYSTEM_OUTFLOW_LAMPORTS`). Created no-key diagnostic tool `scripts/inspect_live_quote.py`. Added 24 tests in `test_audit_78_a.py`. Full suite: **958 passed**, 0 failed.
      2. Phase B (Pre-Sign Simulation Layer): Added `solana.simulate_transaction` and `solana.get_multiple_accounts` with dual-RPC consensus. Created `live_execution/sim_guard.py` asserting post-simulation invariants: `err is None`, SOL decrease <= 0.01 SOL, input token decrease within 1% tolerance, output token increase >= otherAmountThreshold landing in wallet ATA (or native SOL on unwrap), third token accounts untouched, and zero owner changes or unexpected closures. Hardcoded `SIMULATION_REQUIRED: bool = True` and wired into `_sign_transaction` as single signing choke point. Extended `drill.py` with simulation step. Added 12 tests in `test_audit_78_b.py`. Full suite: **970 passed**, 0 failed.
      3. Phase C (Truthful Docs & Comments): Rewrote `docs/14_SIGNING_GUARD.md` detailing 3-layer architecture, removing false claims regarding preflight balance verification, documenting residual risks, converting absolute links to relative paths, and documenting `inspect_live_quote.py`. Corrected inverted `skipPreflight` comments in `jupiter_executor.py` and `solana.py`. Cleaned unverified gitleaks claims in memory-bank. Added tests in `test_docs_security.py`. Full suite: **972 passed**, 0 failed.
      4. Phase D (Broken Pin & Dependency Hygiene): Pinned SearXNG image to verified `2026.10.9-f4822b3fc`, documented manifest digest inspection in `docs/11_DEPLOYMENT.md`, aligned native `SEARXNG_SECRET` environment passing, bumped `urllib3>=2.8.0` in `requirements.txt` and `requirements.lock` (resolving PYSEC-2026-4175/4176/4177 with 0 pip-audit vulnerabilities), updated `Dockerfile` to install from lockfile with `--no-deps`, aligned CI/Dockerfile to Python 3.14, added scheduled weekly CI audit cron, created `.github/dependabot.yml`, and switched Dockerfile healthcheck to lightweight `/api/health`. Added 8 tests in `test_audit_78_d.py`. Full suite: **980 passed**, 0 failed.
      5. Phase E (Session Revocation Memory Growth Remediation): Validated session cookies against HMAC signature and max-age before storing in revocation registry, discarding unauthenticated/forged/expired spam without memory allocation. Bounded revocation registry to `MAX_REVOKED_SESSIONS = 10_000` entries using `OrderedDict` with $O(1)$ LRU eviction and timestamp-based pruning. Documented in-memory durability limitation in docstring and `docs/11_DEPLOYMENT.md`. Added 6 tests in `test_audit_78_e.py`. Full suite: **986 passed**, 0 failed.
      6. Phase F (Minor Hardening Leftovers Remediation): Changed live service restart policy to `restart: on-failure` in `docker-compose.yml` with clean `exit 0` entrypoint shutdown when disarmed/unconfigured. Decoupled runtime secret registration into `backend/secret_registry.py`, registered keypair forms (JSON, base58, hex) on load, and extended logging redactor regex to mask single-quoted Python dict representations. Merged duplicate cash queries into fail-closed `get_wallet_cash_usd()` returning None (never 0.0) when unreadable, updated stats/holdings API responses with `cash_status`, and updated frontend to render "unavailable". Added `TRUSTED_PROXY_CIDRS` with rightmost untrusted IP extraction in `get_client_ip()`. Enforced entropy checks on `ADMIN_TOKEN` (>= 32 chars and >= 12 distinct chars via `config_helpers.py`). Resolved linter warnings (`LLMVerdict`/`LLMBrainResult` in `run_live_cycle.py` and empty f-string in `db_pg.py`). Added 7 tests in `test_audit_78_f.py`. Full suite: **993 passed**, 0 failed.

- [x] §77 Security Audit Remediation (Phases A–E) (2026-10-11):
      1. Phase A (Edge Exposure & Auth): Removed admin-token proxy injection from deploy docs (`test_docs_security.py`); bound uvicorn/compose to loopback and trusted subnets; unified `get_client_ip()` defeating forged proxy IP rotation; enforced 32+ char `ADMIN_TOKEN` minimum entropy; eliminated `test-captcha-pass` bypass; fixed login lockout precedence to prevent operator DoS.
      2. Phase B (Wallet Secret Isolation): Added `wallet.public_address()`; removed `load_keypair()` from all `backend/api/` modules; split `docker-compose.yml` into `api` (no keypair mount) and `live` (read-only keypair mount) services with `ROLE=api|live`; enforced `0o600`/`0o400` POSIX permissions on keypair file.
      3. Phase C (Signing Guard & Deny-By-Default): Added `inspect_swap_transaction(tx, payer_pubkey)` inspecting versioned and legacy Solana transactions prior to signing; enforces deny-by-default allowlist (System, ComputeBudget, Token, Token-2022, AssociatedToken, and `APPROVED_SWAP_PROGRAM_IDS`); pruned unverified Moonshot ID; documented architecture in `docs/14_SIGNING_GUARD.md`.
      4. Phase D (Hermetic Tests & CI): Hardened tests to pass hermetically with zero network, no `.env`, and unbuilt frontend; created GitHub Actions CI workflow (`.github/workflows/tests.yml`); generated `backend/requirements.lock`; pinned SearXNG image to `searxng/searxng:2024.12.30-d3e758a` with mandatory `SEARXNG_SECRET`.
      5. Phase E (Defense-in-Depth): Added `register_secret()` for runtime token scrubbing (Privy OAuth refresh tokens) and JSON-quoted field scrubbing; wrapped Postgres `reset_book` in atomic transaction; capped WebSocket connections per IP (`MAX_WS_PER_IP = 4`) with 30s timeout harvest; guarded against CORS wildcard with credentials; added session revocation on logout; corrected ARIA role in live feed; configured `.gitleaksignore` and `.gitleaks.toml` for test dummy credential false-positives.
      6. Verified: full test suite passes with **934/934 tests green** (0 failures); gitleaks allowlist configured for test fixtures. Full detail: handoff §77.

- [x] §76 Operational Launchers & Docker Container Infrastructure Alignment (2026-10-11):
      1. Local launchers (`start.sh`, `stop.sh`): Dynamic `API_PORT` resolution honoring `.env` with fallback to `PORT` and default `8000`; eliminated paper-era `TICK_LOOP_IN_PROCESS=0` residue; enforced `0o750` permissions on `logs/` directory; `stop.sh` dynamically reports offline port.
      2. Container entrypoint hardening (`backend/docker-entrypoint.sh`, `backend/tests/test_docker_entrypoint.py`): Audit A8 safety regex fix for `LIVE_TRADING_ENABLED` extraction (`grep -cE '^LIVE_TRADING_ENABLED\b[^=]*=[[:space:]]*True\b'`) preventing comments from enabling live execution; dynamic `PORT` / `API_PORT` fallback. Added regression tests (15/15 green).
      3. Production container image (`Dockerfile`, `.dockerignore`): `COPY scripts/ /app/scripts/` to ensure diagnostic CLI tools are available in image; pre-created unprivileged `0o750` `/app/logs` volume chowned to `bot:bot`; `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` configured; healthcheck tests `PORT` or `API_PORT`.
      4. Docker compose parity (`docker-compose.yml`): `depends_on: [searxng]`; added persistent `engine_logs` named volume mapped to `/app/logs` and configured `LOG_DIR=/app/logs`.
      5. Verified: full test suite passes with **904/904 tests green** and frontend build passes cleanly in <1s. Full detail: handoff §76.

- [x] §75 Code quality review cleanups, dead code elimination, query optimizations, and price caching (2026-10-11):
      1. Dead code & unused modules: deleted retired paper-era modules `backend/llm/reuse.py` and `backend/tests/test_narration_reuse.py`; deleted experimental spike `scripts/scrapling_spike.py`; removed dead helper `_i()` in `research.py`; relocated CLI diagnostic `test_llm.py` -> `scripts/check_llm_health.py`; cleaned 55 unused imports across 16 files.
      2. Query optimizations: added `get_feed_events_since` to SQLite and Postgres, cutting full 100-row deserialization in WebSocket broadcaster; added lightweight `get_feed_verdicts` projection for refusal funnel route.
      3. Price caching: added short-TTL (2.0s) in-memory price caching in `JupiterProvider.get_current_price` coalescing redundant RPC quotes across concurrent reads and exit loops.
      4. Sizing consolidation & bug fixes: unified inline position sizing in `stats.py` to `sizing.compute_entry_cost`; restored masked on-chain security fallback in `LiveProviderStack.get_security_info`; standardized `App.tsx` and `LiveBook.tsx` on shared `Panel` and `Empty` primitives.
      5. Verified: test suite passed with **901/901 tests green** (now 903 with §76 entrypoint tests). Full detail: handoff §75.

- [x] §74 Centralized rotating logging, security monitoring, and automated threat event tracking (2026-10-11):
      1. Rotating file logging: `logs/` directory (mode `0o750`) with `logs/app.log`, `logs/error.log`, and `logs/security.log` capped at 10MB per file with 5 backups.
      2. Universal secret redactor: `SecretRedactorFilter` active on all handlers and console, scrubbing admin tokens, API keys, basic auth URLs, Bearer tokens, and Solana keypairs before write.
      3. Security event monitor: `SecurityMonitor` tracks `AUTH_FAILURE`, `AUTH_LOCKOUT`, `RATE_LIMIT_EXCEEDED`, `BOT_DETECTED`, `BLOCKED_PATH_PROBE`, `SUSPICIOUS_UPLOAD`, and `INTERNAL_ERROR` in a thread-safe 300-event ring buffer.
      4. Operator monitoring endpoints: `GET /api/admin/monitoring/security-events` and `GET /api/admin/monitoring/logs` gated by admin token.
      5. Verified: full test suite passes with **913/913 tests green** (+11 new tests in `test_logging_and_monitoring.py`). Full detail: handoff §74.

- [x] §73 Production error shielding, generic 500 error responses, logging secret filter, and validation input stripping (2026-10-11):
      1. Exception shielding: `unhandled_exception_handler` and `http_exception_handler` return generic `{"detail": "Internal server error", "error_id": "<hex_id>"}` with status 500; zero stack traces or internal variable names leak to clients.
      2. Private correlated logging: full tracebacks, method, path, and client IP are logged privately alongside `error_id` for operator debugging.
      3. Secret redaction filter: `SecretRedactorFilter` active across all server/app loggers to mask tokens, API keys, Bearer headers, basic auth URLs, and Solana 64-int keypairs.
      4. Validation sanitization: `validation_exception_handler` with `jsonable_encoder` strips raw `"input"` from 422 errors, preventing secrets or oversized malicious inputs from reflecting.
      5. Verified: full test suite passes with **902/902 tests green** (+11 new tests in `test_error_handling_security.py`). Full detail: handoff §73.

- [x] §72 Production deployment hardening: debug mode, sourcemaps, and .git directory defense (2026-10-11):
      1. Debug mode: `ENVIRONMENT=production` and `DEBUG=false` in `config.py`; FastAPI stack traces disabled; Swagger UI (`/docs`), ReDoc (`/redoc`), and `/openapi.json` disabled in production.
      2. Sourcemaps: `build: { sourcemap: false }` in `frontend/vite.config.ts`; audited 0 `.map` files in `dist`; `.map` requests explicitly 404'd in `spa_fallback`.
      3. .git directory defense: excluded in `.dockerignore`; `_is_blocked_static_path` in FastAPI blocks dotfiles and hidden folders with 404; edge blocking documented for Caddy and Nginx.
      4. Verified: full test suite passes with **891/891 tests green** (+9 new tests in `test_production_hardening.py`). Full detail: handoff §72.

- [x] §71 Rule 19: HTTPS enforcement everywhere and hosting setup verification (2026-10-11):
      1. Force HTTPS middleware: HTTP 308 Permanent Redirects on non-loopback connections and reverse proxies (`X-Forwarded-Proto: http`, `X-Forwarded-Ssl: off`, RFC 7239 `Forwarded: proto=http`).
      2. Security headers: HSTS preload (`max-age=63072000; includeSubDomains; preload`) and CSP `upgrade-insecure-requests`.
      3. Cookie alignment: `Secure=True` enforced for session cookies over TLS, with unencrypted loopback dev mode preserved.
      4. Docker & reverse proxy: Uvicorn `--proxy-headers --forwarded-allow-ips='*'`, `docker-compose.yml` loopback binding, Oracle VM ingress port 80/443 rules and Caddy/Nginx configs.
      5. Insecure content audit: Verified zero mixed-content / unencrypted assets, automatic `wss://` upgrade, and HTTPS for all external API dependencies.
      6. Verified: full test suite passes with **882/882 tests green** (+12 new tests in `test_https_enforcement.py`). Full detail: handoff §71.

- [x] §70 Ten-rule security audit & platform hardening (2026-10-10):
      1. Cookie authentication: moved tokens from `localStorage` into secure, `HttpOnly`, `SameSite=Lax`, `Path=/api` cookies with rolling 15-minute idle expiration and 24-hour absolute session ceiling (`test_cookie_auth.py`, 9 tests).
      2. Access control & record ownership: audited all endpoints by ID; added Supabase Row-Level Security migration `006_rls_policies.sql` (`test_authz_surface.py`, 14 tests).
      3. Mass assignment: Pydantic `extra="ignore"` on all write models, stripping unexpected fields (`test_mass_assignment.py`, 4 tests).
      4. Rate limiting: in-memory sliding window token bucket middleware (auth: 5/min, AI: 10/min, writes: 20/min, reads: 120/min) (`test_rate_limiting.py`, 5 tests).
      5. Billing caps: documented usage alert and hard cap procedures across all external services.
      6. Bot protection: HMAC-SHA256 challenge-response and 2.0s human time-lock before login processing (`test_bot_protection.py`, 5 tests).
      7. SQL injection: 100% parameterized queries in SQLite and PostgreSQL/Supabase with strict column whitelisting (`test_db_surface_parity.py`, 2 tests).
      8. Input validation: comprehensive type, length, regex, and boundary enforcement, blocking null bytes and path traversal (`test_input_validation.py`, 18 tests).
      9. XSS defense: safe React JSX rendering, URL protocol sanitation, Content-Security-Policy headers.
      10. File upload security: whitelist (`.md`, `.txt`, `.json`, `.csv`), binary executable magic bytes and shebang inspection, `0o600` permissions, storage outside web root (`test_file_upload_security.py`, 31 tests).
      Verified: full test suite passes with **870/870 tests green**. Full detail: handoff §70.

- [x] §69 Finish zero-as-unknown fix in research.py (2026-10-10):
      1. Fixed `aggregate_pairs()` in `backend/data_providers/research.py`: tracks reported numeric `txns.h6.buys`/`sells` and returns `None` when unreported, preserving explicit `0` and summing mixed reporting pairs. Kept `_i()` intact.
      2. Replaced tautological tests in `backend/tests/test_zero_as_unknown.py` with real end-to-end tests for `aggregate_pairs` and `enrich_with_research` using `httpx.MockTransport` covering all 4 cases: (a) all 0 -> 0, (b) no txns -> None, (c) mixed pairs -> sum, (d) h6 key missing -> None.
      3. Verified new tests fail against pre-fix code; full suite passes with 796/796 tests green. No changes to `live_execution/`, `rule_engine/`, or frontend. Full detail: handoff §69.

- [x] §68 Close four gaps left by §67 (2026-10-10):
      1. Risk header fail-open: safety.error and unloaded safety.data now treat safety state as offline ("feed degraded"); kill switch / breaker still outrank; offline banner derived from riskState; DESIGN.md §3.4 updated.
      2. Zero-as-None parity: discovery.py (buys/sells 1h/6h) and research.py (buys/sells 6h) preserve 0 counts instead of coercing to None.
      3. LiveFeed ARIA roles: applied role="list" / role="listitem" with aria-current (uncommitted in §67).
      4. Test coverage: 5 new backend unit tests (796 passed) and 3 new Playwright hermetic tests (14 passed). Full detail: handoff §68.

- [x] §67 Full-repo audit: bug fixes, doc completion, memory-bank update (2026-10-08):
      12 bugs found and fixed across backend and frontend. Backend: dexscreener
      buys_6h/sells_6h zero-as-None, 4× "the the" typos, dead API_HOST env var.
      Frontend: risk posture safety bug (liveBook error bypass), help overlay toggle,
      RefusalFunnel copy-paste label, useApi unmount guard, LiveFeed ARIA roles.
      Stale memory-bank files (productContext, projectbrief, systemPatterns,
      techContext) updated from paper-trading to live-trading reality. Handoff
      metadata refreshed. Verified: 791/791 backend tests green, clean Vite build
      (206.16 kB JS, 30.77 kB CSS), 11/11 hermetic Playwright tests passing. Full detail: handoff §67.

- [x] §66 Frontend design polish: typography, case hierarchy, and motion keyframes (2026-09-22):
      Lowercase mono hierarchy across `.panel-title`, `.stat-label`, `.th`, `.tab`,
      Hero stats, AlertStrip header, and LiveFeed arrival marker; Tailwind `@keyframes`
      restored in CSS distribution bundle for `row-flash`, `sweep`, and `blink`;
      redundant dot-chained panel subtitles stripped across Journal, Holdings,
      MarketRegimePanel, and LiveBook; E2E title assertions updated. Verified:
      clean Vite/TypeScript build (203.18 kB JS, 30.35 kB CSS), hermetic Playwright
      suite 11/11 passing, 789/789 backend tests green. Full detail: handoff §66.


- [x] §65 Keyboard vocabulary + mint-history drill-down (2026-09-21):
      operator staged prompt with 3 confirm-gates answered (single `g`,
      dedicated route, separate affordance). Global guarded j/k/Enter/g/G
      dispatcher + registry (lib/shortcuts.ts); `?` overlay from the shared
      SHORTCUTS table (cannot drift); GET /api/mint/{mint}/history + db
      twins in both dialects (422 malformed / empty unknown / same row shape
      as /api/feed); MintHistory.tsx stack level with all five §3 states;
      `history` button beside CopyText in feed/journal/holdings/live-book.
      Verified: 789 Python passing, tsc+vite clean, 7/7 new hermetic Playwright
      tests passing (built frontend via vite preview + BASE_URL, mocked
      API/WS; engine NOT started — REAL-MONEY ARMED). No new
      dependencies. Mobile redesign remains deferred; drill-down shows feed
      events only (cross-surface stitching out of scope). Full detail:
      handoff §65.

- [x] §64 September audit remediation (2026-09-17): read-only safety panel and
      all-view alerts, cross-process crowd observation, persisted funnel trend,
      classified tape/journal drill-down, session seen marker, App extraction.
      Disclosure now respects the writer's `tripped` key; corrupt blocklist
      display is unknown and read-only. 772 Python passing; tsc/Vite clean;
      4/4 new hermetic browser tests. No new dependencies. Live-backed E2E and
      deployment not run; §2.1 excluded. Mobile redesign remains deferred;
      bounded history / freshness / deployment details in handoff §64.

- [x] §63 Frontend operator upgrades (2026-09-16): operator directive —
      staged prompt. Stage 1 premise correction (journal tables already
      guarded; sweep clean, no code); Stage 2 responsive commitment
      (min-width in-container table scroll); Stage 3: hand-rolled read-only
      ⌘K palette (no new dep, no break-state toggle), /api/funnel +
      RefusalFunnel panel, tape j/k/enter/esc, perf_report --since.
      Verified: tsc -b + vite build clean, 547 backend green, E2E 11 → 16
      (operator.spec.ts; NOT run — engine DOWN, REAL-MONEY ARMED). Open:
      ~~operator post-§57 baseline~~ **Stage 0 RUN (2026-09-16): post-§57
      sample is EMPTY — 0 closes, 0 commits since 2026-09-06; 66 in-window
      feed events, all gate refusals. §57 remediation unevaluated until the
      engine runs and accumulates a real sample. Full detail: handoff §63.
- [x] §62 Full-repo code-quality cleanup (2026-09-14): operator directive —
      senior-engineer quality review of the whole codebase, "aggressive but
      safe", then implement the safe parts. Shipped (Batches A–D): dead code
      deleted — 8 zero-caller DB functions from BOTH db.py/db_pg.py
      (adjust_cash, trim_position_row, count_trades, count_closes_since,
      delete_trade_row, get_last_closed_at_for_mint,
      get_recent_closed_reasons, set_trade_thesis — paper-book mechanics
      superseded by the ExecutionLedger), dead config (assert_paper_trading_only,
      WS_POLL_INTERVAL_SECONDS, MAX_EXIT_PROCEEDS_MULT, API_HOST,
      SUPABASE_URL/SERVICE_ROLE_KEY/ANON_KEY, AUTO_BLOCK_CONSECUTIVE_STOPS),
      legacy check_exit_conditions + its 3 tests, frontend Stat/signedPct,
      orphaned pyc + 3.6 MB logs + .bak-dust; two paper-era truth bugs fixed
      (/api/admin/reset paper_trading_only: true; start.sh disarmed banner
      "PAPER TRADING" → "DISARMED — NO LIVE TRADING"); prevention — NEW
      tests/test_db_surface_parity.py (db_pg must mirror db.py's surface;
      closes the silent SQLite-impl-on-Postgres failure mode),
      backend/pytest.ini deleted (silently skipped 300+ live_execution
      tests), tsconfig noUnusedLocals on; legacy — main.py fail-fast router
      imports, Ollama residue purged. Verified: 736 passing, tsc -b clean,
      py_compile/bash -n green; engine/money paths untouched. Batch E
      (structural) deferred with a written plan: handoff §62.
- [x] §60 Dashboard freshness (2026-09-10): operator directive — scan the
      wallet on-chain so manually-closed trades leave the dashboard, fix
      audit bugs, uppercase tabs/headings. Shipped: /api/live/portfolio
      chain-scan via existing A2 machinery (get_token_balances +
      reconcile) TTL-cached at WALLET_SCAN_TTL_SECONDS (300s default) —
      vanished positions excluded from positions/equity + reported in
      chain_excluded + a UI warn line; chain<journal clamps displayed
      tokens; ledger never mutated by a read; outage = stale reuse ≤3x
      TTL then unchecked. system-status LLM health re-probes every
      LLM_HEALTH_TTL_SECONDS (the client's probe cache was forever — §56
      item). Tabs + panel titles uppercased. +6 tests
      (test_dashboard_freshness.py) → 709 passing; build clean (174.58
      kB JS). Read-path only; API restart loads routes, cycle untouched.
      Full detail: handoff §60.
- [x] §59 SIGNAL frontend shipped (2026-09-10): operator directive —
      implement the chosen demo_2_signal world into the main frontend,
      plus (1) money ledger = closed trades only, (2) scroll-bounded order
      decisions, (3) complete contract addresses shown, (4) docs updated,
      demos deleted, clean commit+push. Shipped: SIGNAL tokens (ultra-black
      ladder, cyan `live` structure accent, pass/fail/warn semantics,
      `reject` verdict neutral) in tailwind.config.js + index.css; full-
      height shell — hero band (verbatim EQUITY + Spark equity curve +
      WIN RATE/PF/DRAWDOWN + [● LIVE · real money] tag), five views
      (live/holdings/journal/market/system), 3-column mission-control live
      grid with independently scrolled columns, statusline footer; the
      decisions tape and the journal's decisions/ledger tables scroll
      inside bounded viewports (65vh/60vh, sticky headers); Journal ledger
      filters records to `kind === 'close'` with a Closed-trades stat;
      CopyText primitive shows the COMPLETE mint address click-to-copy in
      Holdings/dashboard positions/Journal ledger+proofs/feed; new ui
      primitives CopyText + Spark; tones renamed pass/fail/warn/live/dim;
      DESIGN.md rewritten as-built; E2E updated in-commit (hero EQUITY +
      Open value pin, five-tab walk → 9 tests). Demos deleted
      (frontend/demos/ + root demo_2_signal.html). Verified: build clean
      (173.97 kB JS / 54.35 kB gzip), frontend-only (engine untouched,
      703 backend passing); E2E to be re-run against the live backend
      before the next deploy. Full detail: handoff §59.
- [x] §58 Terminal dashboard redesign (2026-09-09): operator directive —
      use the repo's impeccable design skill (pbakaus/impeccable, vendored
      at `.clinerules/awesome-design-skills/`) to re-skin the frontend as
      a terminal: minimal, no charts, complete info, good type/colors,
      restrained animation, nothing vibecoded. Shipped: design system
      (Inter body + JetBrains Mono numerics with tnum alignment; near-black
      surface; green/red only for P&L direction; gold only for structure)
      in tailwind.config.js + index.css; tabbed shell
      (Dashboard/Holdings/Journal); panels LiveBook, LiveFeed
      (aria-expanded expand/collapse, Enter-operable, SKIP fail rows),
      Journal, Holdings, **Performance (new)**, MarketRegime, SystemStatus;
      WS exponential-backoff + offline banner; documented empty states.
      Data honesty: Performance's headline is **"track equity"** — the
      ANCHORED book (initial cash + closed realized P&L) with a tooltip
      pointing to the Live Book for the real wallet — resolving the
      earlier $1,000-vs-$6.42 confusion (two books, both correctly
      labeled). Verified: build clean (168.41 kB JS / 53.29 kB gzip),
      8/8 Playwright E2E, screenshots at both viewports; frontend-only
      (engine/ledger/prompts untouched; 703 backend tests unaffected).
      Open operator step: the trades mirror is still empty —
      `scripts/backfill_trades_mirror.py --apply` (§57) unblinds
      stats/calibration/promotion-gate.
- [x] §57 Loss-shape & refusal-layer remediation (2026-09-09): the audit
      follow-up. Root causes QUANTIFIED from the ledger (the §50 baseline
      decomposed): (a) 14/21 losses cluster at −18%…−24% = the −20% NET
      hard stop firing as configured (2%+1% simulated cost model is
      conservative vs real Jupiter routing, so the stop triggers at ~−17.5%
      price); (b) 5/21 losses realized −28%…−65% because §52 RETIRED the
      paper engine's dedicated 15s exit scanner with the paper book — the
      live book was left with once-per-cycle (60s) exit checks, the exact
      §5.2/§20 failure mode documented once before ("stops realize −40%
      avg when checked once/tick"); (c) the refusal funnel measured ~0
      model refusals among gate-passers during the Aug 28–31 trading
      window (23/23 buys; the §52 brain wiring postdates every trade, so
      the per-candidate Thinker passed everything the gate let through —
      vs omo's 74% decline rate). Shipped: (1) **fast exit scanner
      restored** — `_exit_scan_loop` in run_live_cycle.py runs the SAME
      `_manage` (per-position body extracted to
      `_scan_exits_for_position`, shared verbatim) every
      EXIT_SCAN_INTERVAL_SECONDS (15s), price-only, zero LLM, serialized
      with the cycle by `_EXIT_LOCK` (double-sell impossible), book
      re-derived from a FRESH ledger read each pass + chain-reconciliation
      flags overlaid; decimals memoized (`_DECIMALS_CACHE`) so the scanner
      doesn't re-issue getTokenSupply 4×/min (immutable value, non-None
      only cached — unknown stays unknown, the decimals lesson). (2)
      **refusal discipline** in both prompts: THINK_PROMPT gains the
      counter-case bar ("buy is the exception, not the default" — JSON
      contract unchanged); LLM_SYSTEM strengthened ("PASS MOST OF THEM …
      never the default"). (3) **measurable funnel**: perf_report now
      splits GATE refusals vs MODEL refusals (`model_refusal_rate_of_gate_passers`)
      — first run reveals 72 gate-passed rows the model DID decline (60% of
      gate-passers, all deepseek:deepseek-v4-flash, recent journal window)
      — the §2 metric was structurally unmeasurable before. (4)
      **promotion gate answered**: evaluated against the REAL ledger
      history — 3/5 criteria FAIL (25/40 trades, 12% win rate, PF 0.26) —
      the strategy is NOT ready by its own gate; and the /api/promotion-gate
      route was blind (0 rows — §52's mirror postdates all closes), fixed
      by (5) **the §56-deferred trades backfill shipped as a tool**:
      `scripts/backfill_trades_mirror.py` (dry-run by default, `--apply`
      to write; honest basis-pairing identical to perf_report; idempotent
      INSERT OR IGNORE / ON CONFLICT keyed by trade_id; new
      `db.insert_closed_trade_row` in both sqlite+pg backends) — operator
      runs `--apply` to unblind stats/calibration/promotion-gate.
      (6) **the 2 sandbox test failures fixed**: the /api/* JSON-404
      catch-all was registered only `if FRONTEND_DIST.exists():` — a
      buildless checkout got a bare 404 for unknown /api/* paths (the
      reproduced failure); now registered unconditionally at module level
      (+ `/api` bare prefix) and the SPA-shell test skips with an
      explicit reason when no build is present (containment itself stays
      pinned buildless). **703 passing** (backend 529 + live 174; +8 new
      §57 tests: lock serialization, fresh-book/chain-flags overlay,
      backfill pairing/skips/idempotency, prompt pins, 404
      build-independence). Restart required for the running cycle.
- [x] §56 Full-repo consistency audit + Batch A remediation (2026-09-06):
      operator directive "Audit the repo and find any bugs or
      inconsistencies". 18 findings; Batch A (safe fixes) executed:
      dead `import main`/`TICK_LOOP_IN_PROCESS` crash path removed (README
      documented a startup-crashing command); `paper_trading_only` unified
      to `false` across system-status/root/stats/holdings; unknown `/api/*`
      GETs now JSON-404 instead of the 200 SPA shell (+ regression test);
      frontend WS reconnects with exponential backoff (1s→15s); LiveFeed
      fail rows render SKIP (was a red "PASS"); `SystemStatusResponse`
      aligned to the live contract (`main_llm_reachable`/`main_llm_provider`)
      + 6 dead paper-era types deleted; `.env.example` rebuilt (Supabase +
      wallet-pin + provider keys in, Ollama-era keys out);
      `MAIN_LLM_PROVIDER` default deepseek everywhere; docs renumbered
      (`13_ORACLE_DEPLOY_GUIDE`); stray root `trading_bot.db` deleted;
      deprecated X-XSS-Protection header dropped. **695 passing + 8 E2E.**
      Deferred (operator-gated): trades-mirror history backfill, the two
      chain-excluded positions, LLM health TTL, `/api/*.json` naming.
- [x] §53 Security hardening & vulnerability remediation (2026-09-03):
      operator directive "audit the repo and find security flaws and make a
      implementation plan for them". Remediated 7 vulnerabilities:
      (1) SEC-01: blind transaction signing guard in live execution with
      `APPROVED_SWAP_PROGRAM_IDS` whitelist and fee payer verification;
      (2) SEC-02: live book access control (`LIVE_BOOK_PUBLIC: bool = False`,
      gating `/api/live/portfolio` and `/api/live/executions` to loopback or
      `X-Admin-Token`);
      (3) SEC-03: unbounded break duration clamped to `[5, 60]` minutes in
      liveness/thinker/llm_brain, mitigating prompt injection DoS;
      (4) SEC-04: CSWSH origin validation + `MAX_WS_CLIENTS = 32` capacity ceiling;
      (5) SEC-05: CORS `allow_headers` updated to include `X-Admin-Token`;
      (6) SEC-06: in-memory 3s TTL caching on `/api/verify.json` and `/api/binding.json`;
      (7) SEC-07: Solana Base58 mint address sanitization in discovery;
      (8) 20-point security checklist: sliding-window auth brute-force lockout
      (429) in auth.py; FORCE_HTTPS 301 redirection + HSTS/Permissions-Policy/
      X-XSS-Protection headers in main.py; pip-audit + npm-audit clean.
      **669 passing.**
- [x] §52 Single-book restructure — paper retired, live retains everything


      (2026-09-03): operator directive "remove paper trade completely, but
      make sure live execution retains all features" + audit gaps 1.3/1.8.
      (A) `backend/sizing.py` — the money math both books shared, extracted
      verbatim; every importer re-pointed. (B) Retention ports into the live
      cycle: trades-table mirror (ledger→DB, one-way, idempotent — fixes the
      discovered starvation bug: live never wrote trades rows, so calibration/
      learning/perf_report would have died with paper), the role-routed brain
      with LIVE portfolio context, §49 loss-memory recall into the thinker
      (was written-never-read), closed-trade reflections, daily learning.
      (C) Deleted main.py, paper_trading_engine.py + 27 paper-only tests;
      stats/holdings routes read the live book (mirrored trades + chain USDC);
      disclosure single_book:true. (D) 1.3 skin-in-the-game: 80% already
      existed (dumped-thesis weight 0.0 → effective_total → heat); closed the
      last delta — thinker tags closed-at-profit authors as exit-liquidity
      marketing. (E) 1.8 chain-first book: 90% already existed (A2 reconcile);
      verified complete. **658 passing.**
- [x] §51 Free social stack — Brave→SearXNG search chain + staged social
      read (2026-09-02): operator directive "make the social part free,
      good success rate + performance". Crowd feed already free (§47).
      Shipped (A) the free-first web-search chain in `web_research.py`:
      Brave (`freshness=pd`, free $5 monthly credit ≈ 1k searches/mo) →
      self-hosted SearXNG (`format=json&time_range=day`; public instances
      403 json — the sidecar in `deploy/searxng/` + compose is REQUIRED) →
      Firecrawl last-resort; identical {title, description} rows (prompts
      byte-unchanged); §34 bench ported (402/422 long, 429 short, 2
      transport errors → bench); empty answer falls through to next hop.
      (B) staged social read: `read_social_one` + usage queue, stage 5 of
      `gate_candidate_staged` (all-passed population only — Groq free 1k
      req/day covers it; un-staged was ≈11k/day), reuse via existing
      social_interest, both books drain the queue into llm_call_usage.
      `enrich_candidates` social-free (returns []). Config knobs
      BRAVE_SEARCH_API_KEY/SEARXNG_URL/SEARCH_BENCH_SECONDS/
      SEARCH_THROTTLE_BACKOFF_SECONDS; docker-compose searxng sidecar;
      docs/12 runbook; README + handoff §51. +16 tests net:
      `test_social_staging.py` (9) + `test_search_chain.py` (6: bench
      semantics) + web-staging extended (+1; fixture now clears
      Brave/SearXNG — the operator's real .env leaks via load_dotenv).
      **676 passing.** Follow-ups: Brave key + SEARXNG_URL + social-key
      .env flips; empty paid scrape keys after the §47 shadow week.
- [x] §50 Exit-engine overhaul Phase 0 — forensics + live sell gate +
      the honest scorecard (2026-08-31): the omo re-audit (live public
      disclosure — source unchanged since 48a86f9) proved our avg win
      (+57.88% vs their +59.71%) and hit rate (12.5% vs 9.4%) are ALREADY
      at/above theirs; the whole expectancy gap is the LOSS SHAPE (−26.05%
      vs −7.4%) + the missing LLM refusal layer (they decline 74%). Shipped
      Phase 0: (a) ledger exit-rule forensics — `ExecutionRecord.rule_id`
      threaded reduce_position→place_sell/place_order→_manage, outofband
      stamped; `tranches_taken/last_close_ts/closes_since` reads kill the
      latent re-trim-every-60s bug and make the exit-rule mix computable;
      (b) the reference sell gate ON live — cooldown + 8/24h + min clip
      floored at the §45 equity-proportional live ticket (paper $25 kept
      bit-identical via the None default; risk-off bypasses inside the
      gate); (c) `scripts/perf_report.py` — read-only scorecard in omo's
      disclosure format, backend-agnostic (sqlite + Supabase), baseline
      recorded; (d) docs/09 §F ground truth + §B.2 superseded, no omo
      cache/clone existed to clean. +12 tests (`test_50_exit_forensics`) +
      jump-guard fixture on a real tmp ledger. **660 passing** (495 backend +
      165 live). Live cycle stays off while the wallet is empty; Phase 0
      loads on next start.
- [x] §49 anti-churn v2, one memory, both books (2026-08-30): audit vs
      the reference found the LIVE book had NO loss memory (auto-block
      existed only in the paper engine and only counted exit_stop_loss
      rule IDs; the reference has no automatic per-mint loss counter at
      all). blocklist.py is now the single source of truth:
      `record_close_outcome()` (PnL-based, any exit rule, both books,
      newest-first closes history capped at 10) feeds `maybe_autoblock()`
      (N=2 consecutive loss closes → block until a human clears it) +
      a 24h re-entry cooldown enforced in `filter_candidates` (read
      stage, both books, self-expiring, zero quota). Both books wired
      (paper close path AND `run_live_cycle._manage`): record →
      autoblock → (on loss) soft memory (upsert_memory + loss_close
      event — reference layer 5). Disclosure gains `anti_churn` truths.
      Audit-surfaced hardening: conftest now isolates BLOCKLIST_STATE_FILE
      (mock-tick tests had been polluting the operator's real sidecar
      and auto-blocking mock mints; file cleaned, backup kept);
      `_load()` fails OPEN on unreadable paths like on corrupt state.
      10 new tests. **647 passing** (495 backend + 152 live). Live
      cycle restarted; first ticks clean. NOTE: Firecrawl /v1/search is
      returning 402 (credits exhausted) — fail-soft handled it, but
      paid search evidence is empty until refilled.
- [x] §48 web-search spend discipline — staged + cached (2026-08-30): the
      Firecrawl `/v1/search` evidence stage left the unconditional read
      chain and became stage 4 of `gate_candidate_staged` (only
      all-passed/thinker-bound candidates get searched; §44-style
      `web search skipped … (no quota spent)` logs), behind a two-tier
      mint-keyed cache (hits 7200s / misses 1800s; same-ticker
      different-mint never share). `search_web` never-raises now.
      8 new tests; read-stage call pinned must-NOT-run. **637 passing.**
      Live: 3 searches in first ~2 min vs ~4–6/tick before (>95% cut).
      Deferred (designed): Brave→DDG→Firecrawl free-transport chain,
      provider counter, 1-week shadow.
- [x] §47 Scrapling local stealth transport — IMPLEMENTED & LIVE
      (2026-08-30): Phase-0 drill 6/6 per hop (curl-cffi p50 0.42s,
      browser p50 1.00s vs httpx 0/6 always-430) proved fomo's block is
      TLS-fingerprint, not IP. New `stealth_browser.py` (curl hop + warm
      AsyncStealthySession with CF solver, idle close 600s, recreate after
      3 failures) + `crowd.py` chain (curl-first direct, `_scrape_scrapling`
      before the paid providers, same bench machinery) + config knobs
      (`SCRAPLING_ENABLED=0` reverts) + pinned dep + Dockerfile browser
      layer + 9 new tests. **629 passing.** LIVE: every fomo read 200 via
      scrapling, ZERO paid /v1/scrape, ZERO 430s; Firecrawl credits now
      only fund the web-search stage. README credits added (GitHub only).
      Follow-ups: ~1-week shadow then optionally empty paid scrape keys;
      re-run spike on Oracle (IP-reputation check).
- [x] §46 Oracle Always Free deployment decision + Scrapling deferred
      (2026-08-30): engine deploys as the existing single Docker image
      (state on named volumes); sizing corrected to the VERIFIED Always
      Free allowance — **2 OCPU + 12 GB** always-on (1,500 OCPU-h +
      9,000 GB-h/month), NOT the 4/24 previously documented (oversized A1
      instances are disabled→deleted 30 days after trial end on free
      tenancies; PAYG upgrade keeps the free allowance); docs/11 updated.
      Frontend ships single-origin (baked SPA on :8000) or Vercel Hobby
      (`VITE_API_BASE_URL` + `FRONTEND_ORIGIN`). Scrapling analysis
      complete and DEFERRED to post-deploy: `scrapling[fetchers]` pip dep;
      `AsyncStealthySession` (`solve_cloudflare=True`, Privy bearer via
      `extra_headers`) first in the `crowd.py` chain; `_direct_get`
      upgraded to curl-cffi impersonation; Groq social reads off + Groq
      main code deleted; candidates 20→12, research 8→4, web-search 8→4;
      paid keys as 1-week failover; BSD-3 credit in the GitHub README
      ONLY (never the website); Phase-0 gate ≥80% solve success from the
      Oracle IP. Fresh Firecrawl credits added 2026-08-30 (paid chain
      primary until Scrapling lands).
- [x] §45 equity-proportional live minimum ticket (handoff §45) (2026-08-30):
      fixed the "site says ENTER but nothing executes" incident — TREE passed
      the staged gate every cycle, then sizing refused the $0.4962 ticket
      (cash $3.31 × 0.15) against the hardcoded $0.50 `MIN_LIVE_TICKET_USD`.
      New formula `live_execution/config.py::min_live_ticket_usd(equity) =
      max($0.10, at-cost equity × 0.10)` (operator chose 10% of 3.5%/5%/10%);
      ONE function drives the `_live_cash_available` gate rule, the sizing
      refusal, AND the risk_budget floor threading, so the gate and sizing can
      never disagree on the threshold (that disagreement was the root cause).
      Below-floor refusals now record in `outcome["entries"]` + a 4dp log
      line instead of vanishing. At-cost equity (cash + Σ position cost) —
      never price-dependent, fails closed to the $0.10 dust floor. Paper $25
      floor frozen; isolation intact; hardcoded never-env-settable. At the
      $4.59 incident book the floor is $0.4586 → the TREE ticket places.
      **620 tests green (468 backend + 152 live: §45 formula suite — scaling,
      dust clamp, fail-closed inputs, incident regression; cash-rule rewrite
      incl. over-deployed-book failure).** Live cycle needs a restart to load
      the new constants.
- [x] §44 staged gate: rules → fomo scrape → crowd rules → LLM (handoff §44)
      (2026-08-30): `decision_pipeline.gate_candidate_staged()` sequences the
      decision per candidate — every cheap (non-crowd) rule first
      (unconditionally), the fomo.fun scrape ONLY if they all passed, the
      crowd rule last against whatever the scrape returned. A candidate that
      fails any cheap rule is never scraped (`crowd scrape skipped for SYM:
      failed liquidity_floor (no quota spent)` in the log) and never costs an
      LLM call: the once-per-tick brain is unchanged, but the per-candidate
      thinker runs only when `gate.all_passed` (and now sees REAL crowd
      theses), while a rule-refused candidate gets the deterministic template
      write-up (`source="template:rules-refused"`, verdict forced "pass") so
      its journal row stays complete. `cheap_rules()`/`crowd_rules()`
      partition the injected list on `__name__` (incl. `LIVE_ACTIVE_RULES`);
      `gate.py::decision_from_results()` is the single definition of
      `all_passed`/`failed_rule_ids`/`not_evaluated_rule_ids`; assembled
      breakdowns keep the DECLARED rule order. Per-tick spend is now 1 brain
      call + N scrapes + N thinker calls, N = candidates passing all nine cheap
      rules, and the saving compounds within a tick (cash/slot exhaustion makes
      later candidates fail cheaply). Preserved: fail-closed skips, honest
      rejection stats, mock hermeticity (no scrape off live), dead-feed
      fail-soft. **615 tests green (464 backend + 151 live: staged-gate
      ordering/proxy/defer tests, both pipelines gate-before-think).**
- [x] §43 crowd-feed quota: shortlist-only `crowd_heat` lookups (handoff §43)
      (2026-08-30): the metered fomo.fun board read left the unconditional
      enrichment chain — `decision_pipeline.enrich_crowd_for_shortlist()` runs
      the same `evaluate_gate` on the same injected rule list MINUS the crowd
      rule (`cheap_rules()`) and fetches only for candidates that already
      cleared everything else; the rest get `crowd_lookup_deferred=True` and
      `crowd_heat` reports `evaluated=False` / "not evaluated" instead of a
      presence-proxy number. New `RuleResult.evaluated` +
      `GateDecision.not_evaluated_rule_ids`; the gate FAILS CLOSED on skips
      (`all_passed` = `passed AND evaluated`) and keeps them out of
      `failed_rule_ids`. Deliberate, scoped trade-off: rejects no longer show a
      real crowd number for that one field; everything else (all nine other
      rules always evaluated, breakdown completeness, honest rejection stats,
      mock hermeticity, dead-feed proxy fallback) preserved. Both books wired;
      dashboard shows a neutral `SKIP`. **610 tests green (460 backend + 150
      live: +6 rule/gate semantics, +6 pipeline behavior, +1 live delegation).**
- [x] §42 deployable restructure (handoff §42) (2026-08-30): `live_execution/`
      → `backend/live_execution/` + `run_live_cycle.py` → `backend/` (git mv,
      history kept); cross-package sys.path juggling removed everywhere;
      Dockerfile (SPA baked in) + docker-entrypoint.sh (armed+wallet gate) +
      docker-compose.yml (persistent state volumes) + .dockerignore;
      `WALLET_KEYPAIR_JSON` env channel + keypair log redactor + identity pin
      on both channels; `FRONTEND_ORIGIN` comma-list CORS; `solders` pinned
      in requirements; frontend `VITE_API_BASE_URL` + .env.example;
      docs/11_DEPLOYMENT.md; §42b root-cause fix of the live-state paths:
      `config.LIVE_STATE_DIR`/`BREAK_STATE_FILE`/`KILL_SWITCH_FILE` as the
      single source of truth, readers resolve per call, suite retargeted to a
      tmp dir (it had been reading the operator's REAL break state — 6 tests
      failed whenever the bot took a break); entrypoint fixes (missing
      shebang, unchecked `cd`, single-process `wait`, hardcoded health port).
      **597 tests green (448 backend + 149 live: +11 wallet-secrets, +6
      state-path co-location, +12 docker-entrypoint).**
- [x] §38 security audit + hardening (handoff §38) (2026-08-28): 20-rule
      checklist → 11 pass / 3 partial / 2 gaps / 4 N-A. Zero secrets in full
      git history; RLS ON everywhere; parameterized queries; npm+pip audits
      clean. Fixed: `.env`+keypair → 600; admin/ingest endpoints token-gated
      fail-closed (`api/auth.py`, X-Admin-Token); ingest 200k char cap;
      security-headers middleware; CORS narrowed to GET/POST+Content-Type.
      E2E made deterministic. 527 unit + 8 E2E green; live 403/200 verified.
- [x] §37 stale-holdings dust fix + FIRST REAL FILL + items 1 & 3 (handoff
      §37) (2026-08-28): one ledger bug → three symptoms (holdings ghost,
      ENTER blocked by MAX_OPEN_POSITIONS, journal/book disagree). Fix:
      `full_close` threaded `reduce_position`→`place_sell`→`place_order`→
      `_manage`; full close realizes PnL on full cost + one-time repair of the
      stuck dust row. Live proof: PINK buy gate=PASS → **FILLED $0.66 → 491.15
      tokens** (first real fill). Item 3 `CommitLog.reconcile_orphaned` healed
      7 memo-only orphans (0 ambiguous `published` left). Item 1 narrator
      anti-repetition rotation. 516 unit + 8 E2E green.
- [x] §36 live execution UNBLOCKED + Journal/Holdings restored (handoff §36)
      (2026-08-28): three stacked bugs fixed — quote verb (POST→GET via new
      `_get_json`, buy AND sell paths), `ExecutionError` import NameError,
      solders `from_bytes` (no `.deserialize` in 0.29); every post-memo
      failure now journalled via `logc.fail` + network phase catches all
      exceptions fail-closed; live proof: GTA6 sealed→memo→quote 200→blocked
      at 2.5% impact floor (5.30%) with honest journal reason; new read-only
      `/api/live/executions`; Journal + Holdings pages restored (three-page
      tab bar, proof-expand rows with solscan links); 506 unit + 8 E2E green.
- [x] §35 frontend rebuild + STATE_DIR fix (handoff §35) (2026-08-28):
      terminal design system (`frontend/DESIGN.md`, token-only
      `tailwind.config.js`, shared `lib/format.ts` + `components/ui.tsx`);
      all four live panels rebuilt (LiveBook / LiveFeed / MarketRegime /
      SystemStatus with real reasoning model + llm_usage_recent); feed
      REST-hydrates 50 rows then WS live-append (deduped); `term-*` tokens
      retired; fonts self-hosted; Playwright `npm run test:e2e` 5/5 (console-
      clean load, data/empty never blank, aria-expanded + Enter operability,
      offline banner). FOUND + FIXED: empty `LIVE_EXECUTION_STATE_DIR=` →
      `Path("")` = CWD → live CommitLedger at repo root; now or-fallback to
      `live_execution/state/`, ledger moved, gitignore entries added, +3
      tests. vite build clean; **501 backend tests + 5 E2E passing**.
- [x] §34 live-cycle hardening (handoff §34) (2026-08-28): two operator-reported
      issues with the ARMED live cycle. (1) 403-rejection benching
      (`backend/data_providers/crowd.py`): `_CONSECUTIVE_REJECTIONS` — a stealth
      provider whose proxy keeps getting refused by the origin (HTTP 403) is
      benched after two, exactly like a 402, instead of being re-tried on every
      candidate every tick; own counter (a 403 is a completed response so it
      must not clear the transport streak), reset on any 200. (2)
      Micro-bootstrap live cash rule (`run_live_cycle.py`): `LIVE_ACTIVE_RULES`
      swaps the paper `cash_available` (checks $100 `INTENDED_POSITION_SIZE_USD`)
      for `_live_cash_available` (checks `MIN_LIVE_TICKET_USD` $0.50); every
      other rule verbatim, paper rules calibration-frozen. Both live-verified
      ARMED: scrapingdog benched after 2× 403 (ScrapeOps served all 20), no
      `cash_available` gate failures, several `gate=PASS`, $5 book sizes $0.75
      ≥ floor. 11 new tests → **498 combined passing**.
- [x] §33 armed state committed + pushed (handoff §33) (2026-08-28):
      operator-directed ("push config as armed, no questions asked").
      `live_execution/config.py` committed ARMED (`LIVE_TRADING_ENABLED=True`,
      `REQUIRE_MANUAL_CONFIRMATION=False`); canary test re-purposed to pin the
      committed state; disclosure `armed` fixed to read the real flag (it
      previously always said False); README/handoff/report aligned with a
      clone-warning. 486 passing, fully green. §27 is now fully COMPLETE.
- [x] §32 cash-corruption fix + bad-quote guards + final omo audit (handoff
      §32) (2026-08-28): phantom-cash incident root-caused (bad quote ~2,960×
      → poisoned high-water → TP trim credited ≈$94k); two hardcoded
      fail-closed guards (`EXIT_PRICE_JUMP_MAX=50` scan-level skip/no-ratchet;
      `MAX_EXIT_PROCEEDS_MULT=200` proceeds backstop on close AND trim);
      book cash repaired; identical jump guard in the live `_manage` loop
      (3 tests on the exact incident numbers); 9 paper-guard tests. Live cash
      verified accurate by construction (chain USDC balance re-read every
      cycle, never accumulated). Final full-coverage omo audit (docs/09 §F):
      exit module exists-but-unpublished (their tests import it), calibration
      factor still unwired in their sizing (grep proof), wash-trade parity;
      no trading-critical gap remains; remaining deltas = UX polish / scale
      custody / hosting plumbing. Operator ARMED this machine (§27 human-only
      flags flipped by hand) — armed config deliberately uncommitted; the
      ships-disarmed canary test is red by design while armed. **486 tests;
      485 pass while armed**
- [x] §27 pre-flight + DEVNET DRILL PASSED (handoff §31) (2026-08-28): first
      real keypair load exposed + fixed two latent fail-closed bugs (wallet
      from_json path-vs-content → from_bytes on validated array + 64-u8
      check; drill.py undefined log + logging order) — commit d8e426f, +4
      regression tests. Drill 5/5 on devnet incl. real signed+broadcast+
      confirmed dust transfer and the REF-R11 commit-memo path. Arm flags
      untouched (still DISARMED); remaining steps are operator-only: mainnet
      wallet funding, `.env` re-point, the two hand-edited flags, supervised
      --once
- [x] A11 thesis re-authoring (handoff §30): `backend/thesis_restate.py` —
      the module the original omo audit missed (`thesis-author.server.ts`),
      found in the 2026-08-27 re-read (full local clone). Once per tick/cycle,
      rewrites open write-ups that are stale (>6h) or not model-authored
      against the position's current numbers (≤2/pass, oldest first, under-60-
      word contract). Narrative-only (thesis text/author/updated_at only;
      retired-mid-pass rows guarded out); reuses the tick's own price marks;
      fail-closed validation; peak-window skip; never raises. Wired into the
      paper tick + live cycle, both DB layers, disclosure.json, llm_call_usage
      (task thesis_restate), `did` events. 26 new tests; **470 combined
      passing**; isolation grep clean; live-verified first tick advanced both
      stale open write-ups. Ships with live execution still DISARMED — arming
      is §27 (2026-08-27)
- [x] omo-audit code queue A7/A6/A3/A2/A4 (handoff §29): wash-trade "fake
      chart" filter (all 13 omo thresholds, READ-stage before think/gate),
      hardcoded symbol blocklist (omo's list + `^404`, enforced in
      `filter_candidates`), venue attribution (`live_execution/venue.py` →
      `decision_commits.venue` → /api/binding.json), chain book reconciliation
      (`solana.get_token_balances` + `live_execution/reconcile.py`; journal
      never mutated, exit sizing clamped to chain truth, vanished positions
      excluded + flagged), own-basis read-back (`crowd.read_own_basis` +
      `FOMO_OWN_HANDLE`, cross-checked vs journal cost each live cycle,
      observability only). 65 new tests since REF-R11; **444 combined
      passing**; isolation grep clean; live smoke disarmed all endpoints 200
      with `venue` surfaced. Ships DISARMED — arming is §27 (2026-08-27)
- [x] REF-R11 on-chain precommit memo (commit–reveal) + micro-bootstrap (handoff
      §26): every armed order seals `sha256(nonce|canonical_payload)`, publishes
      the hash on-chain as a Solana memo (`commit:v1:`, SPL Memo program) BEFORE
      the fill, fail-closed (unconfirmed memo blocks the fill). New
      `live_execution/memo.py`; `commit_log.py` `sealed→published→bound`;
      `executor.py` memo-before-quote ordering + `OrderResult` carries seal+memo;
      `solana.get_usdc_balance()`; `run_live_cycle.py` real-USDC cash + journals
      seal+memo into `decision_commits`. Verifier: `memo_signature`/`memo_slot`
      columns (SQLite + PG self-heal + `003_commit_memos.sql`),
      `bind_commit_memo()`/`get_commit_id_by_hash()`, `/api/verify.json` memo
      checks, `/api/disclosure.json` `commit_memo` block. Micro-bootstrap:
      `MIN_SOL_RESERVE` env (default 0.01 SOL), `MIN_LIVE_TICKET_USD=0.5`,
      sizing floor threading (paper bit-identical). Devnet drill sends a real
      memo. 41 new tests; **379 combined passing**; isolation grep clean; live
      smoke disarmed all endpoints 200. Ships DISARMED — arming is §27 (2026-08-27)
- [x] Fresh scraper keys activated + ScrapingDog bearer-forwarding (handoff
      §25): new Firecrawl/ScrapingBee/ScrapingDog/ScrapeOps keys in root .env
      (ZenRows unchanged/exhausted); backend restarted to load them + clear
      benches. `_scrape_scrapingdog` now appends `custom_headers=true` and
      forwards the Privy bearer (ScrapingDog docs: same mechanism as ScrapeOps
      keep_headers, no extra cost). Live-verified: Firecrawl 15× 200 OK (real
      crowd heat restored), ScrapeOps 1× 200 OK failover, 0 tracebacks. +1 test
      (2026-08-27)
- [x] Dead-provider fail-fast + reference fomo-path audit (handoff §24):
      transport-error benching (`_CONSECUTIVE_ERRORS`, 2-in-a-row → bench like
      a 402, any response resets the streak) so a dead scraper can never stall
      a tick; `_scrape_firecrawl` wrapped in try/except (was uncaught);
      `_FIRECRAWL_TIMEOUT(45s)` → `_STEALTH_TIMEOUT(25s)` reference parity;
      `_direct_get` 2 transport attempts. Reference audit confirmed its fomo
      fallback is the same Firecrawl stealth-proxy we already call (no free
      mechanism). 6 new tests (23 in test_crowd.py); 337 combined passing;
      live-verified: ScrapingBee benched after exactly 2 timeouts, crowd stage
      degrades in seconds not ~15 min (2026-08-27)
- [x] REF-R8 + REF-R9 reference-parity batch 2 (handoff §22→§23): drawdown-
      adaptive risk budget (`compute_risk_budget`, verbatim reference port,
      fail-closed to $25, published formula) × closed-loop conviction factor
      (`calibration.py`, bounded 0.6–1.2, confidence-pulled, FLAT on no data);
      `SIZING_MODE="risk_budget"` opt-in branch (default `"fixed"` unchanged);
      derived daily ceiling enforced + journaled; `patch_daily_stats()`
      key-merge persistence in db.py + db_pg.py; disclosure.json surfaces both
      blocks; learning_loop + disarmed run_live_cycle wired. 42 new hand-
      computed tests; 331 combined passing; live smoke verified (tick persists
      real budget, endpoint serves it, 0 tracebacks) (2026-08-27)
- [x] De-brand + rename brain module (2026-08-27): brain module renamed to
      `backend/llm/llm_brain.py` and every identifier de-branded (brain class → `LLMBrain`,
      parser → `parse_llm_tick`, system prompt → `LLM_SYSTEM`, config → `LLM_BRAIN*`,
      requirement ids → `REF-R#`); test files → `test_llm_brain`/`test_ref_r*`; removed
      scratch `verify_reference_commit.py`. Repo-wide upstream-branding → "the
      reference"/"reference" across 54 files (407/407 balanced). Kept `fomo`/`promotion`/
      token tickers (not the brand). 289 tests pass; backend restarted clean on the
      renamed module (0 import errors).
- [x] Reference-style brain (handoff §21): ported the reference repository's LLM *reasoning layer*
      into `backend/llm/llm_brain.py` — role-based router (honest resolution +
      fallback + unsupported-model bench), brain tick prompt (decision buckets,
      ground-truth + price-talk rules, minified-JSON contract), wallet mimicry,
      strict parse/validate, `LLMBrain.tick()` grades ≤8 candidates fail-closed.
      `main.py` uses brain verdicts in live mode (per-candidate thinker fallback);
      the reference `buying`→`buy` is NECESSARY only (gate still ANDs). Fixed pre-existing
      `reused_if_stable` `KeyError: 'stats'` tick-crash (writer stores stats +
      reuse fails closed). Live-verified: brain ping 8/10 graded (`DELTA=buying`,
      2268 tokens, no truncation). 27 new tests, 289 total (2026-08-27)
- [x] DeepSeek main-provider swap (handoff §18→§19): `MAIN_LLM_PROVIDER`
      selector + `DeepSeekClient` + `build_main_client()` factory;
      peak/off-peak/cache cost model; provider-aware timeouts;
      non-thinking mode enforced; thinker/narrator/reflections live on
      `deepseek-v4-flash`; shadow replay gate 8/8; first full DeepSeek
      tick verified; 30 new tests (2026-08-27)
- [x] Stealth-scrape chain 429/402 split: rate-limit (429) → short
      `STEALTH_THROTTLE_BACKOFF_SECONDS` backoff instead of the 30-min credit
      bench; credit exhaustion (402) keeps the long bench; provider error body
      logged (self-diagnoses e.g. ZenRows AUTH004). Restores a healthy
      Firecrawl after a transient throttle. +1 regression test, 262 total
      (2026-08-27)
- [x] Supabase schema-drift fix: self-healing `_SCHEMA_SYNC_SQL` in `db_pg.init_db()` (events/memories/theses/llm_call_usage + versioning columns + RLS + migration bookkeeping); db_pg surface fixes (`status` param, `::text` casts, feed-event versioning parity); live-verified: first full tick completed, all endpoints 200 (2026-08-27)
- [x] DB maintenance: prune_feed_events/prune_market_regime + reset_book in both db.py + db_pg.py (2026-08-27)
- [x] POST /api/admin/reset (confirm=yes required; reset_book + prune_only modes) (2026-08-27)
- [x] REF-R1–R7 audit: all routes confirmed correct and well-tested (2026-08-27)
- [x] REF-R1 Independent verifier + binding report: `/api/binding.json`, 4-check binding (2026-08-26)
- [x] REF-R6 Public disclosure + reasoning: `/api/disclosure.json` + `/api/reasoning.json` (2026-08-26)
- [x] REF-R7 Retro audit-log signature matching: `retro_matcher.py` post-cycle (2026-08-26)
- [x] REF-R4 bug fix: `liveness.set_break` call site wrong positional args fixed (2026-08-26)
- [x] Root pytest.ini restored: `asyncio_mode=auto` for full combined suite (2026-08-26)
- [x] REF-R2 FOMO crowd intel upgrade: theses WITH author P&L (2026-08-26)
- [x] REF-R5 durable events + weighted memory recall, prompt context,
      stage event hooks, `/api/events.json` (2026-08-26)
- [x] REF-R4 Self-regulating break system wired into thinker and gate loops (2026-08-26)
- [x] REF-R3 Durable thesis book hooked into live and paper cycles + /api/theses.json (2026-08-26)
- [x] 10 deterministic rules, both branches tested, no short-circuit (B1–B12)
- [x] Candidate model with all fields incl. decimals + None semantics (B13)
- [x] Market regime: computed once/tick, own table, API endpoint (C1–C5)
- [x] Atomic paper engine: open/close/scale_in, decide_and_act, exits,
      PAPER_TRADING_ONLY runtime asserts (E1–E7) — double-open/double-close/
      crash-replay proven by tests
- [x] Money math with hand-computed expectations, raise-on-invalid (E5/E6)
- [x] Providers live+mock: Birdeye memepool/security, Dexscreener full
      enrichment, Jupiter decimals-aware, retry/429/counters (A1–A9)
- [x] LLM narration pre-decided verdicts + grounding validation + Ollama
      health + reflections (D1–D6); template fallback for offline runs
- [x] Knowledge base: static + ingest CLI/API + digests + budgeted context
      + bucket stats (F1–F9)
- [x] Learning loop daily stats + rejection breakdown (G1–G3)
- [x] Read-only promotion gate, 5 criteria (G4–G6)
- [x] Full API surface + WS feed broadcaster (H1–H5)
- [x] Dashboard panels incl. persistent safety banner (I1–I10; build passes)
- [x] Tests J1–J5 → **222 passing** (full suite: backend + live_execution)
- [x] One-click launcher verified start/stop/restart
- [x] the reference bot comparison + commit-reveal verification (docs/06)
- [x] Task C: live_execution/ seven-file safety model at repo root,
      offline-tested (**48 passing**; 182 combined via root pytest.ini)
- [x] reference-mimicry rebuild in progress: exit engine + fast scan loop,
      entry gate = the reference rules verbatim, old logic purged, crowd conviction
      feed (fomo.fun board) live-validated → **backend 136 / combined 184**
- [x] Security audit: secret scans CLEAN, stale files removed, gitignore
      hardened (*.db-journal, wallet-keypair.json)
- [x] Supabase Postgres backend (optional): migrations/supabase/001_init.sql
      applied; api/db_pg.py asyncpg twin of db.py (identical surface,
      §5.1 atomicity preserved); db.py backend selection with pytest
      SQLite guard; live smoke passed all atomicity checks against real
      Supabase + uvicorn boot serving PG data on all endpoints
- [x] Stealth-scrape chain upgraded: scrapeops keep_headers + zenrows
      custom_headers+premium_proxy forward the Privy bearer — verified
      pulling REAL fomo board data through Cloudflare; scrapingbee
      keyless-only (platform limitation); _json_from_body statusCode≥400
      bugfix (success envelopes were silently discarded)
- [x] Live execution wiring verified: root bridge, Jupiter quote/swap,
      local signing, rotating RPC send/confirm, commit binding, ledger
      journal, live manage/sell path, and fail-closed preflight are connected;
      disarmed by hardcoded `LIVE_TRADING_ENABLED=False`; 45 focused tests
      pass. Funded throwaway-keypair devnet drill remains required.

## Deliberately not built (per spec sequencing)
- E8/E9 partial scaling + rolling history (post-calibration)
- D7 advisory LLM layer (post-calibration)
- crowd_heat rule (needs a fomo-index source)

## Known issues / watch items
- Birdeye free tier: token_security 401 → security fields UNKNOWN
- Ticks take 40–90s with 20 candidates (LLM-bound); acceptable
- Regime/rule thresholds are placeholders — calibration will move them
- ScrapingBee fallback is keyless-only (platform consumes Authorization);
  it also ReadTimeouts on stealth reads — now benched after 2 consecutive
  transport errors so it can't stall ticks (handoff §24)
- ZenRows premium tier costs ~10–25 credits/request (required for prod-api)
- ⚠ ZenRows is 402 credit-exhausted (benched; optional renewal). Firecrawl +
  ScrapeOps got fresh keys 2026-08-27 and now serve REAL crowd heat (handoff
  §25). ScrapingBee ReadTimeouts and ScrapingDog 403 are harmless backups.
- Supabase pooler cert self-signed → fingerprint pin (.supabase_fp.txt);
  delete the file to re-pin after a legitimate cert rotation

- LLM API migration: DONE — main path (thinker/narrator/reflections) live
      on DeepSeek V4 Flash (`MAIN_LLM_PROVIDER=deepseek`, non-thinking mode,
      shadow-replay-gated 8/8); Groq remains the rollback main provider and
      powers the evidence-only social reads. Usage/outcome accounting,
      shadow replay, and canary gates are implemented.
- Live execution is fully wired (incl. the REF-R11 on-chain commit memo) and
      this repo is committed **ARMED** (§31/§33: devnet drill 5/5, then the
      operator's own hand-edit + explicit direction). Arming remains hardcoded
      and human-edit-only — no env bypass. Since §42 the engine is packaged
      for deployment (Docker + entrypoint double-gate: ARM flag AND a wallet
      secret), with wallet secrets resolvable from a mounted file or the env
      JSON channel, both fail-closed and identity-pinned.
      *(This bullet previously said DISARMED / arming-is-the-final-task —
      superseded on 2026-08-28.)*

## Status
**As of 2026-08-30 (§42/§42b): the repo is DEPLOYABLE.** `live_execution/`
and `run_live_cycle.py` moved inside `backend/`, so the engine is one
Docker-packaged module (entrypoint starts the live cycle only when armed AND
a wallet secret is configured, and exits non-zero if either half dies); the
dashboard deploys separately via `VITE_API_BASE_URL` or is served
same-origin. Wallet secrets resolve via `WALLET_KEYPAIR_PATH` (preferred) or
`WALLET_KEYPAIR_JSON` (in-memory), fail-closed, identity-pinned, redacted
from logs. §42b made `config` the single source of truth for live-state
paths after finding that two readers composed them off the old repo root —
which also revealed the suite had been reading the operator's REAL break
state (now a tmp dir, hermetic). Four entrypoint defects fixed (missing
shebang = image could not exec, unchecked `cd`, single-process `wait`,
hardcoded health port). **597 tests green (448 backend + 149
live_execution)**; engine restarted and verified on the new layout, all
endpoints 200, disclosure `armed: true` with a clear kill switch. Remaining
work is operator-side deployment only — `docs/11_DEPLOYMENT.md`.
Previous status (§39): roadmap items #1/#3/#6 shipped — security_clear is
the live 10th gate rule (KNOWN-bad-only), crowd heat discounts dumped thesis
authors (`FOMO_DUMPED_THESIS_WEIGHT`), and the paper + live pipelines run the
same read/think/gate core (`backend/decision_pipeline.py`; three live-side
drifts fixed: fake-chart filter, thinker template fallback, set_break arity).
550 tests + Playwright 8/8 green; live cycle restarted on the new code and
verified (security_clear in every /api/feed breakdown, 0 tracebacks).**
Previous status: Live calibration day ~2. A11 thesis re-authoring shipped (2026-08-27, handoff
§30): the omo re-read (full local clone) found `thesis-author.server.ts` —
missed by the original audit — and it is now ported: open write-ups stale >6h
are advanced against current position numbers (≤2/tick, narrative-only,
fail-closed). Live-verified on the first tick (two write-ups advanced via
DeepSeek, retired row skipped, 0 tracebacks). The same re-read resolved both
remaining audit caveats verbatim (placeOrder guards: our executor is a strict
superset; their calibration factor: still unwired in their public code). The
code queue is EMPTY again — everything left is operator-gated (§27 arming),
post-calibration, or needs external credits/keys.
Dead-provider fail-fast shipped (2026-08-27, handoff
§24): a provider that times out / fails to connect twice in a row is now
benched exactly like a 402, so a dead scraper can never stall a tick again
(was ~15 min/tick when ScrapingBee ReadTimeouts went un-benched). Stealth
timeout cut 45s→25s and direct read gets 2 transport attempts (reference
parity). Reference audit confirmed its fomo fallback is the same Firecrawl
stealth-proxy we already call — no free scrape mechanism exists. Firecrawl +
ZenRows are credit-exhausted (402, benched); refilling Firecrawl restores real
crowd heat with zero code changes. Main LLM now DeepSeek V4 Flash (2026-08-27,
handoff §19): shadow-replay-gated, flipped via MAIN_LLM_PROVIDER, first
full tick verified (all thinker calls success, peak-window cost accounting
correct, zero tracebacks). Fresh $1,000 book. Supabase schema-drift incident
FIXED (2026-08-27): live book was missing events/memories/theses/llm_call_usage
(every tick died, system-status 500'd); `db_pg.init_db()` now self-heals via
idempotent `_SCHEMA_SYNC_SQL` — verified live: first full tick completed, all
endpoints 200. DB maintenance endpoint added
(2026-08-27): prune_feed_events, prune_market_regime, reset_book, POST /api/admin/reset.
REF-R1–R7 audited and confirmed correct. Dashboard v2 shipped
(2026-08-25): ENTER/PASS feed labels, verbatim model answers + contract
address in feed detail, five-number portfolio stats panel; knowledge tab +
paper banner removed. App runnable via ./start.sh (rebuilds frontend/dist).
**Tests: 527 backend + 8 Playwright E2E — fully green** (backend 399 +
live_execution 128; +11 security-hardening tests this batch; 8 E2E in
frontend/e2e/dashboard.spec.ts via `npm run test:e2e`).
The flag-state canary now pins the committed ARMED state — green while armed
(handoff §33/§34).

