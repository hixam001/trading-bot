# Security remediation — 2026-11-10–11

Status: source remediation is committed as `04e1feb`. All canonical local pre-push stages now pass under split isolation after the user permitted advisory-only lookups. Stage 9 retains its existing missing-gitleaks notice; no local gitleaks/Docker or new remote-CI result is claimed. No production all-clear is claimed.

Source baseline: `028d4e07375946d6c51072fb624342cc83616b77`. Historical audit records remain unchanged. Original confirmed fingerprints: `backend/api/auth.py:require_admin_token:cookie-before-disabled`, `backend/api/db_pg.py:_tls_context:unverified-no-pin-fallback`, `live_execution/executor:partial-sell-retry-after-single-rpc-absence`. B1/B2 remain preventive source invariants with native approved-DEX exploitability unproven. Rejected WebSocket connection-cap race is not treated as a vulnerability.

## Implementation and verification policy

```json
{"planning":"deep","context_depth":"dependency","verification_depth":"impact","delegation":"disjoint implementation plus independent security review","reason_codes":["security","multiple-files","durable-state","signing-policy"]}
```

Read source, callers, tests and audit traces before each change. Preserve unrelated untracked `.agents/` and `skills-lock.json`. Do not read real credentials/state. All target-controlled execution requires network-disabled OS isolation, empty allowlisted environment, read-only tools/source, bounded scratch and explicit resource/time limits. No launchers, provider calls, real signing, deployment or quota spending. Push is requested by the user after verified completion; do not bypass required checks.

## Remediation matrix (recorded before edits)

| ID | Source locations | Intended invariant | Planned change | Acceptance check | Final disposition |
|---|---|---|---|---|---|
| A1 | `backend/api/auth.py; backend/api/routes/auth.py` | Disabled operator auth denies every credential | Move configuration gate before authentication | Restarted dedicated-secret cookie reset/ingest denial and enabled rotation | Implemented; real ASGI reset/ingest, disabled status, rotation and denial controls pass. `test_s2_auth_hardening.py`. |
| A2 | `backend/api/db_pg.py` | Authenticate database peer before application bytes | Remove unverified fallback; retain handshake pin | CA/probe failure refusal, strict CA and pin MemoryBIO | Implemented; refusal before pool/auth, strict CA, known pin, MemoryBIO mismatch/positive and unwritable persistence pass. `test_audit_93_r2.py`. |
| A3 | `backend/live_execution/executor.py; models.py; commit_log.py` | Unresolved same-intent sells never reauthorize | Strict endpoint resolution and durable sell journal | Timeout/null/later success, restart, exactly-once reconciliation | Implemented; durable pre-send sell marker, strict native status resolution, restart hold, signature-idempotent reductions. `test_security_sell_recovery.py`. |
| B1 | `backend/live_execution/sim_guard.py` | Output minimum uses net wallet gain | Aggregate signed output deltas and separate native/rent | 1000→0 plus 0→100 refusal; positive/control fixtures | Preventive invariant implemented; signed aggregate output with control continuity and native/rent separation. Native DEX consequence still unproven. `test_security_signing_policy.py`, `test_remediation_output_quorum.py`. |
| B2 | `backend/live_execution/sim_guard.py; solana.py` | All safety facts participate in consensus | Conservative monitored-account agreement | Extra-account disagreement, malformed/missing, pre/post paths | Preventive invariant implemented; every monitored account participates in pre/post consensus, malformed responses refused. Strict pool-data comparison may reduce availability. Same signing suites. |
| H01 | `backend/api/websocket.py` | Slow consumers cannot stall healthy recipients | Bound concurrent sends and cleanup | Slow and healthy clients; cancellation | Implemented; parallel bounded sends, disconnect/close deadline and cancellation cleanup. `test_remediation_hardening.py`. |
| H02 | `docs/11_DEPLOYMENT.md; docs/18_SECURITY.md` | Auth lifecycle documentation matches authorization | Document persistent revocation and rotation distinction | Compare documented lifecycle to A1 tests | Implemented documentation correction in deployment/security docs; persistent logout and nonempty rotation/disable distinctions match A1 tests. |
| H03 | `backend/api/routes/proof.py` | Identical misses share bounded work | Single-flight with cancellation/failure cleanup | Concurrent identical misses invoke fetch once | Implemented; one bounded shared task per fixed route, shielded waiters and exception cleanup; original three-second TTL retained. `api/singleflight.py`, hardening tests. |
| H04 | `backend/api/main.py` | Mounted and SPA assets share sensitive-file policy | Guard mounted assets and SPA paths | Allowed built assets and denied fixture files | Implemented; mounted assets and SPA use shared policy; built JS/CSS/fonts/images pass, sensitive/dotfile/map fixtures refuse. Hardening tests. |
| H05 | `backend/live_execution/wallet.py; executor.py` | Every production signer checks expected identity | Trace legacy/manual signer and enforce identity | Wrong dummy wallet refusal; drill compatibility | Implemented; expected identity checked at production signing boundary and legacy/manual entry. Drill policy retained. `test_remediation_signing_policy.py`. |
| H06 | `backend/data_providers/stealth_browser.py` | Credentials stay at allowed origin | Restrict headers and cross-origin redirects/fallbacks | Dummy same/cross-origin redirect fixtures | Implemented origin/redirect protections for browser, curl and direct fallback; retained explicitly configured credential-forwarding proxy intermediary trust. Policy tests and signing policy notes. |
| H07 | `backend/data_providers/crowd.py` | Latest rotated refresh token survives persistence failure in memory | Retain rotated credential before persistence | Repeated refresh and restart failure fixtures | Implemented; latest per-app rotation stored before encrypted persistence; no plaintext fallback. Restart cannot recover an unpersisted rotation and may require re-login. Hardening/crowd tests. |
| H08 | `backend/data_providers; backend/rule_engine/rules.py` | Numeric authority is finite and non-bool | Normalize provider numeric boundaries | NaN/inf/bool/zero and final gate fixtures | Implemented finite/nonbool provider parsers and required deterministic numeric gate guards; meaningful zero retained. `test_remediation_provider_numbers.py`, hardening/rule tests. |
| H09 | `backend/live_execution/executor.py; data_providers` | Decimals are native-domain integers with chain authority | Validate decimals and trace buy construction | Zero decimals, bool/fraction/disagreement | Implemented; integer 0..255 excluding bool, chain fact before buy construction, unavailable/disagreement refuses. Chain decimal read retains existing RPC trust. Policy tests. |
| H10 | `backend/data_providers/dexscreener.py` | Enrichment binds chain and base mint | Require Solana chain during pair selection | Mixed-chain and correct-mint fixtures | Implemented; exact base mint plus Solana chain required for enrichment; mixed-chain dummy response test passes. |
| H11 | `backend/data_providers/new_listings.py` | Malformed listings do not poison valid siblings | Validate nested shapes per record | Mixed valid/malformed batch | Implemented; per-record/nested shape checks and valid sibling survival in listing, trending, discovery and research. Provider/hardening tests. |
| H12 | `backend/llm/narrator.py; backend/thesis_restate.py` | Narrative data is bounded untrusted text | Reuse shared wrapping/sanitization | Untrusted restatement input/output fixtures | Implemented; shared untrusted fences on narrator/restatement/reflection inputs; bounded sanitized persisted outputs; deterministic authority unchanged. Narrative and hardening tests. |
| H13 | `backend/llm/thinker.py` | Malformed model shapes fail closed | Validate collection/object shapes | List/null/scalar JSON and bounded output | Implemented; JSON object/field/nested collection checks before access; malformed verdict returns fail-closed result. Hardening and LLM suites. |
| H14 | `backend/live_execution/executor.py; trade_intent.py` | Manual approval binds canonical operation | Bind side/wallet/mints/raw amount/decimals/expiry | Enabled fixture mismatch without consuming approval | Implemented; bound side/wallet/mints/exact raw quantity/decimals/slippage/minimum plus existing expiry/one-use, mismatch never consumes. Unbound legacy approvals must be reproposed. Policy tests. |
| H15 | `backend/bounded_http.py; backend/data_providers; backend/llm; backend/live_execution/solana.py` | Response bytes bounded before parsing | Review shared/provider limits and compatibility | Streaming/compression/timeout boundaries | Implemented streaming wire/decoded caps and total timeout for HTTPX surfaces; stealth parser cap added. Retained browser/Scrapling internal allocation limitation, explicitly not a complete transport guarantee. Hardening tests and policy notes. |
| H16 | `backend/llm/spend_guard.py; docs/18_SECURITY.md` | Budget guarantees reflect actual accounting | Reserve where sound or document stop thresholds | Concurrency/failure/zero cap and retained policy | Retained documented policy: per-service call reservations; token/estimated-cost stop thresholds are post-response and may overshoot/undercount. No invented price/token bound or merged API/live scope. Existing spend tests pass. |
| H17 | `backend/run_live_cycle.py; backend/live_execution/executor.py` | Unknown settlement retains exposure and intent | Review strict outcome/finality across recovery | Conflicting/unreachable recovery remains pending | Implemented strict pending buy/sell and ambiguous/negative/malformed immediate settlement; local signature identity preserved. Only valid immediate positive single-provider trust retained. Pending/recovery tests. |
| H18 | `backend/live_execution/models.py; commit_log.py` | No cross-process lost read-modify-write | Add locks or enforce verified single writer contract | Concurrency, crash release, lock ordering | Implemented POSIX process-safe complete RMW locks; restore acquires sorted authority locks and preserves lock inode. Single production decision-maker remains required. `test_security_state_integrity.py`. |
| H19 | `backend/live_execution/models.py; scripts/restore_state.py` | Restored authority validates every record | Validate identities/statuses/finite record values | Malformed authenticated restored fixtures | Implemented ledger/commit/wallet-snapshot validation, runtime/ack writer guards, and canonical nonsymlinked restore dispatch before adoption; history preserved. Invalid record/overflow/duplicate/authenticated alias fixtures pass. |
| H20 | `backend/api/db_pg.py` | Same invariant as A2 | Map to A2, not a separate vulnerability | A2 refusal and handshake tests | Mapped to A2 implementation/docs/tests; not a separate vulnerability. |
| H21 | `scripts/backup_state.py; scripts/restore_state.py` | New backup dirs private, plaintext snapshots 0600 and cleaned | Exclusive private temporary files and finally cleanup | Permissions and exceptional paths | Implemented private new directories/exclusive 0600 SQLite/staging snapshots and exceptional cleanup; existing directory permissions unchanged. Hardening/backup/restore tests. |
| H22 | `start.sh; backend/requirements.lock` | Launcher uses locked dependency strategy | Install requirements.lock | Inspect launcher/Docker/CI parity | Implemented launcher parity using existing requirements.lock; no dependency installation/upgrades performed. Docker/CI already use that lock. |
| H23 | `.dockerignore` | Nested private material stays out of build context | Extend nested secret/state patterns | Synthetic ignore/context markers | Implemented nested secret/key/env/state exclusions; synthetic wildmatch-compatible marker regression passes. Docker daemon/build unavailable; native Docker context validation not claimed. |
| H24 | `.github/workflows/gitleaks.yml` | Workflow token has minimum permissions | Declare contents: read | Review pinned scanner behavior and triggers | Implemented explicit contents: read; existing pinned actions and safe triggers unchanged. No remote workflow/token behavior verification claimed. |

## Changes and compatibility

A1 now checks the disabled configuration before either authentication branch. A
separate session secret still supports deliberate nonempty operator-token
rotation; clearing the operator token disables existing sessions. Logout
revocation remains persistent. Existing arming, confirmation defaults, unknown
security policy, limits, loss breaker and wallet configuration were preserved.

A2 refuses pool creation when CA validation and certificate probing cannot
establish authenticated TLS. Configured, persisted and in-memory pins retain
mismatch refusal, and the actual TLS handshake checks the pin before application
authentication. TOFU still trusts the first successfully probed certificate;
an independently verified operator pin is preferable. An unwritable pin file
keeps enforced in-memory pinning, but that continuity ends at process restart.

A3 journals each locally signed sell before send and checks every distinct
configured endpoint before retry authorization. Native `solders` status parsing
rejects malformed error enums, slots and conflicting legacy status mirrors.
Established success reconciles once using a signature accounting key; unknown,
absent, unreachable or conflicting observations leave a durable hold. An invalid
blockhash is not evidence of historical non-execution. Failed signed attempts
remain in commit history. Existing quote-snapshot accounting when actual fill
amounts cannot be read is retained; exactly-once reconciliation is not an
independent proof of actual chain quantities. Pending buys, ambiguous sends and negative/malformed
immediate confirmations now share strict resolution; a receipt naming another
signature cannot settle the locally signed transaction. The existing immediate
well-formed single-provider positive confirmation policy is retained. No actual
duplicate fills or financial loss were demonstrated by the original audit or
these tests: the original result was unsafe retry authorization.

Deploy over an interrupted pre-fix sell only after operator reconciliation:
older unjournaled signatures cannot be reconstructed automatically. Do not run
multiple production decision-makers. POSIX locks serialize ledger/commit
read-modify-write operations and restores, not whole asynchronous trading
strategies. Restore acquires authority locks in sorted order, preserves lock
inodes, and rejects reserved paths, lexical aliases and symlinked child
directories before schema dispatch. The owner-selected root directory remains
an operator trust boundary. Invalid ledger/commit/snapshot records are refused
without rewriting historical files. Invalid wallet-monitor snapshots produce an
actionable skipped monitor result; this is not a fabricated zero balance or a
new strategy halt. Legacy wallet snapshots have no wallet identity binding;
optional identity metadata is validated rather than inventing a migration.

B1/B2 repair preventive source invariants. Native/WSOL output minima are
conservatively net of fees and rent paid; rent refunds cannot satisfy output.
All monitored account identity/control/state participates in pre/post consensus.
Opaque pool data is compared exactly, while existing explicit token/lamport
amount tolerances remain. Pool-state drift can therefore cause more refusals.
The documented single-RPC sell fallback remains available and validates response
shape. Neither change establishes the still-unproven approved-DEX signing
consequences described in the historical audit.

Manual approvals now bind the canonical operation before consumption; old
unbound approvals must be reproposed. Chain decimal unavailability/disagreement
refuses buys rather than substituting six or nine decimals. See
[SIGNING_POLICY_REMEDIATION.md](SIGNING_POLICY_REMEDIATION.md) and
[PROVIDER_NUMBER_REMEDIATION.md](PROVIDER_NUMBER_REMEDIATION.md).

## Retained tradeoffs and native validation

- H06: explicitly configured forwarding proxies remain trusted intermediaries
  for origin credentials. Browser/curl/direct-origin confinement is not a claim
  that proxy providers never receive credentials. Redirects do not forward them.
- H15: HTTPX provider, RPC, Jupiter, LLM, search, CAPTCHA and alert calls now use
  separate 8 MiB wire/decoded limits and a 60-second total deadline in addition
  to existing request timeouts. Unsupported encodings and concatenated/truncated
  compressed streams refuse. Scrapling/Playwright materialize responses before
  exposing their body: their 4 MiB parser cap does **not** establish an internal
  allocation limit. Retaining these transports preserves existing feed
  availability; use independently verified streaming interception or process
  limits for a complete allocation guarantee. This item is partial protection
  with an explicit retained limitation, not a global byte-limit claim.
- H16: calls are reserved before sending within each supported single-writer
  counter scope. Token and estimated-cost values remain post-response stop
  thresholds; in-flight usage can overshoot, billed failures/missing usage can
  undercount, and provider pricing can change. No conservative input-token or
  price bound was established, so no hard token/cost maximum is promised. Zero
  caps and separate API/live scopes remain; exit-critical RPC is not newly capped.
- H17/H18: immediate well-formed positive provider confirmations retain the
  existing trust policy; ambiguity and negative outcomes require strict native
  resolution. Multiple live decision-makers and power-loss durability beyond
  existing atomic-file semantics are not certified.
- H23/H24: synthetic ignore-rule fixtures cover the compatible wildcard subset;
  no native Docker context/image or remote workflow/token verification was run.
- B1/B2: a pinned offline approved-DEX program, valid transaction and initial
  account fixture are still required to prove adverse native program effects.
  Mocked balances and native status/layout parsers do not supply that evidence.

## Verification policy and observed evidence

All target-controlled execution used the inspected parent-side
[bounded sandbox runner](security-remediation/sandbox_runner.py), or equivalent
scratch-only baseline/mutation wrappers. No real environment file, wallet,
execution state, database, retained audit directory or host socket was mounted.
Only source/templates and installed toolchains/dependencies were copied/mounted.
The sandbox copies the allowlisted source into disposable scratch before tests
write next to source; its synthetic Git index contains that copy only.

Base isolation: `bwrap --unshare-all`, dropped capabilities, private PID/network
namespaces (isolated loopback for browser/API tests), read-only root, source,
toolchains, `/proc` and `/dev`; writable scratch tmpfs 512 MiB plus `/tmp` tmpfs
128 MiB. Limits: CPU 240 seconds/process, file 16 MiB, descriptors 256, core 0,
wall 300 seconds, captured output 8 MiB. Python checks additionally use a 2 GiB
address-space limit and 64-process limit. The browser profile uses a newly
provisioned delegated cgroup with aggregate memory 2 GiB, swap 0 and 256 tasks;
Chromium's large virtual reservations make the Python address-space limit
unsuitable. The cgroup is populated before target execution and removed after
all descendants stop. No limit/network assertion was removed from a test to
obtain a pass. An independent stdout-closure probe confirmed the wall deadline
still terminates a process that closes its output streams.

The environment starts empty. Base allowlist: `PATH=/usr/bin`,
`HOME=/scratch/home`, `TMPDIR=/tmp`, `XDG_CACHE_HOME=/scratch/home`,
`PYTHONDONTWRITEBYTECODE=1`, `PYTHONHASHSEED=0`,
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`. Command mode adds the asyncio pytest plugin,
scratch-only `PYTHONPATH` and disabled pip version checks. Browser mode adds only
the runner's explicit dummy auth values, mock backend, scratch DB/state/log paths,
local browser toolchain path, loopback base URL and test-only rate ceilings.
Those fixtures do not change committed production configuration.

Commands below ran from the repository root. Exit 0 denotes only the stated
check, not production verification or advisory cleanliness.

| Command/check | Observed result |
|---|---|
| `python3 docs/security-remediation/sandbox_runner.py --with-frontend -q` | 2,227 passed; 102 existing deprecation warnings; no failures/skips. TypeScript `tsc -b`, Vite build and built-bundle secret scanner passed in the same disposable source copy. |
| `python3 docs/security-remediation/sandbox_runner.py --e2e` | Direct check: 41/41 passed, 15.9 seconds; newly started isolated **mock** API and installed Chromium only. No ordinary/live runner started. |
| `python3 docs/security-remediation/sandbox_runner.py --command /usr/bin/python3 -m pip check` | `No broken requirements found.` |
| `python3 docs/security-remediation/sandbox_runner.py --command /usr/bin/python3 scripts/audit_runtime_writes.py --check` | Passed; new lock and private snapshot write homes classified explicitly; full suite also enforces this check. |
| `git diff --check` | Passed after source changes. |
| Independent final immediate-settlement/sell/snapshot/state suites | 105 passed; reviewers resolved auth/TLS, signing-policy, retry/settlement, restore and sandbox lifecycle findings. |
| Three retargeted mutation checks in disposable source | 3/3 caught by actual assertion failures, with source restored: `81A_EXECUTOR_LANDED_FAILED`, `93R2_MEMORY_PIN_FORGOTTEN`, `93R2_KNOWN_PIN_DOWNGRADE`. This is not a claim that the entire mutation campaign was rerun. |
| Auth baseline overlay of reviewed auth/routes/TLS source against current regressions | Dedicated-session disabled-auth cases: 4 failed before fix, pass in final suite. Reproducible with runner `--baseline-auth-tls -q backend/tests/test_s2_auth_hardening.py -k disabled_admin_rejects`. |
| TLS baseline overlay | CA/probe refusal cases: 2 failed before fix, pass in final suite. Use runner `--baseline-auth-tls -q backend/tests/test_audit_93_r2.py -k ca_and_probe_failure`. Original code reached the dummy pool boundary. |
| Signing policy baseline overlay | Earlier B1/B2 policy subset: 26 failed, 2 passed before source repair; current policy and neighboring suites passed. No approved-DEX exploit execution occurred. |
| Provider/snapshot/immediate-settlement baseline checks | Each new focused regression failed on reviewed source (`True` treated as numeric; invalid snapshot written; immediate negative outcome released unresolved state), then passed on repaired modules. |
| Runner wall probe: `--wall-seconds 2 --command /usr/bin/python3 -c 'import os,time; os.close(1); os.close(2); time.sleep(20)'` | Exit 124, `Sandbox wall limit exceeded`; independently reproduced. |

Early broad checks exposed missing source examples/migrations, executable modes,
subprocess dependency paths and `/tmp` assumptions in sandbox provisioning;
these were corrected inside isolation. Legacy HTTP doubles now emit actual
streams through the production reader. Dummy archive/account fixtures now use
valid real-module schemas. An initial browser attempt could not launch under
the Python virtual-memory profile; the bounded cgroup profile above passed.
Final results supersede those failed setup/fixture runs; assertions and network
isolation were preserved.

## Initial offline advisory limitation — resolved with explicit permission

The initial no-network runs could not resolve PyPI/npm advisory services. An
exploratory npm offline audit printed zero vulnerabilities because the installed
npm skips advisory collection in offline mode; that output was excluded from
security evidence. The user subsequently explicitly authorized read-only
advisory lookups only. Actual lookup and staged gate results follow below.
No dependency installations, provider requests or relaxed test isolation were
used to resolve this limitation. Docker and a local gitleaks executable remain
unavailable; passing unit tests are not substituted for those checks.

## Historical input trace

The source review used these immutable external audit inputs from
`/home/hixam/security-audit-skill/trading-bot/run-1`; they were not rewritten.

| Input | SHA-256 |
|---|---|
| `REPORT.md` | `988a068eaad1f8569cc7d4a527c31fa47c3c8dc5a058769f8e91230316376a1a` |
| `findings.json` | `a3a10cb09dfaa5eddd17762151300f3a22e4b5949f943115403395666e5b855f` |
| `FINDINGS-DETAIL.md` | `4df070cd5ee879ff5573317a6ae385f72a49de6eddfd84dce3cb3128fb3f00bc` |
| `NEEDS-VALIDATION.md` | `0eec51140b7e32b33829b1314b75eb3348e7e82c5acd61ab94dd416e9b4f51d0` |
| `coverage-ledger.json` | `764ac43a130a91d76d73e8f6033f9d84dabbfb83d7c5ee6f9e80ea9272b73867` |

## Advisory permission and staged gate follow-through — 2026-11-11

The user explicitly permitted read-only PyPI/npm advisory lookups only, with no
installs or trading-provider access. The existing trusted audit tools read copied
pinned manifests from a clean scratch working directory and empty allowlisted
environment. Python audited all 75 locked packages with `--disable-pip --no-deps`
(no dependency installation); `pip check` separately verifies installed dependency
consistency. npm audited the production lock graph with scripts disabled. Both
returned exit 0: no known Python advisories, zero npm production vulnerabilities.

Decision: add a stage selector to the existing canonical pre-push script. Default
execution still runs every check in order and stops on failure; no stage is
removed. Stages 1/2/4/5/6/8/9 stay in the network-disabled OS sandbox. Only stages
3/7 run the inspected advisory tools with the user's narrow network exception,
using copied manifests and no application execution or installs. Validate stage
selection/failure propagation with dummy command executables, review independently,
and execute every actual stage before push. This is isolation provisioning, not
a claim that cached outputs or unexecuted commands passed.

## Final canonical pre-push stage results

The existing gate now accepts `--stage 1..9` so every actual check can execute
in its required environment. Its default remains all nine stages in order and
stops on the first failure. Selected mode reports only that stage's result;
it does not claim the entire gate passed. Sixteen executable regressions check
stage selection, invalid arguments, default ordering and failure propagation;
independent review found no blocking issue in the selector, browser profile or
advisory helper. No existing security assertion was weakened.

All nine actual stages were executed successfully, with the original stage-9
missing-tool notice explicitly retained:

| Stage | Exact invocation from repository root | Observed result |
|---|---|---|
| 1 | `python3 docs/security-remediation/sandbox_runner.py --with-frontend --command /bin/bash scripts/prepush_check.sh --stage 1` | **2243 passed**, no failures/skips, 102 existing warnings; includes the 16 new gate-dispatch tests. |
| 2 | `python3 docs/security-remediation/sandbox_runner.py --command /bin/bash scripts/prepush_check.sh --stage 2` | No broken requirements found. |
| 3 | `python3 -I docs/security-remediation/advisory_stage.py 3` | Actual canonical pip-audit invocation passed; **75 locked packages, no known vulnerabilities** at lookup time. |
| 4 | `python3 docs/security-remediation/sandbox_runner.py --command /bin/bash scripts/prepush_check.sh --stage 4` | API keypair isolation: **1 passed**, 3 deselected. |
| 5 | `python3 docs/security-remediation/sandbox_runner.py --with-frontend --command /bin/bash scripts/prepush_check.sh --stage 5` | Installed TypeScript `tsc --noEmit` passed. |
| 6 | `python3 docs/security-remediation/sandbox_runner.py --with-frontend --command /bin/bash scripts/prepush_check.sh --stage 6` | Actual production build passed; setup build/bundle scanner also passed. |
| 7 | `python3 -I docs/security-remediation/advisory_stage.py 7` | Actual npm production audit passed; **0 vulnerabilities**. |
| 8 | `python3 docs/security-remediation/sandbox_runner.py --gate-e2e` | Actual gate mock-API/browser stage: **41/41 passed**, 15.2 seconds, bounded private cgroup. |
| 9 | `python3 docs/security-remediation/sandbox_runner.py --command /bin/bash scripts/prepush_check.sh --stage 9` | Existing missing-local-gitleaks notice; stage exits 0 by its existing policy. **No clean local secret-scan claim.** |

For stages 3/7 the inspected advisory helper copies only the current gate and
pinned manifests into scratch, uses existing trusted tool entry points, an empty
allowlisted environment and private empty npm configuration. It admits only
selectors 3/7. Python uses `--disable-pip --no-deps` against the complete lock to
avoid installs; stage 2 separately checks installed dependency consistency.
npm uses `--ignore-scripts`. HOME/cache/temp paths are scratch-local, npm's
registry is explicit and update notices are disabled. The test/build runner
remains network-disabled. No cached audit output is replayed as a tool result.

The source commit remains `04e1feb`; this follow-through commit contains the
staged gate interface and final report/memory updates. Publication is requested
by the user; observed push/CI status belongs in the final delivery message.
Advisory results are point-in-time and scoped to the pinned Python lock and npm
production graph, not a guarantee of future CVE status or production safety.
