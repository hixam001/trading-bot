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

## CI browser bootstrap follow-through — 2026-11-11

Observed failure on `f4868ac`: GitHub's Ubuntu 26.04 (`resolute`) runner could
not resolve the Azure/Ubuntu APT mirror hostnames during Playwright's native
dependency installation. APT exited 100; browser tests had not started. This
is an observed hosted-runner DNS failure, not a failed application assertion.

Implementation policy: direct scoped CI change; preserve mandatory native
dependency installation and all browser assertions. Pin only the backend/frontend
test job to the explicitly supported `ubuntu-24.04` image instead of the moving
`ubuntu-latest` label, and bound Chromium setup to ten minutes. The successful
Docker job and pinned actions/permissions remain unchanged. This removes runner
version drift but does not promise immunity to a future DNS outage. Verify YAML
structure and existing workflow/security checks inside the no-network sandbox;
then verify the actual browser step on the newly provisioned CI runner. No
local package installation or manual change to resolver/TLS policy is involved.

Primary references: [GitHub runner images](https://github.com/actions/runner-images),
[Ubuntu 26 runner migration](https://github.blog/changelog/2026-09-17-ubuntu-26-generally-available-and-latest-migration/),
[Playwright CI installation](https://playwright.dev/docs/ci).

Local affected check: `python3 docs/security-remediation/sandbox_runner.py -q
backend/tests/test_s8_supply_chain.py backend/tests/test_audit_81_f.py
backend/tests/test_prepush_stage_selection.py` — **32 passed**, no failures.
The failed old run is [43050187110](https://github.com/hixam001/trading-bot/actions/runs/43050187110);
its Docker/ROFS job passed. New hosted setup/E2E verification remains necessary.


## §94 addendum — source baseline `ea0a899` (2026-11-12)

Original audit findings and earlier measurements above are preserved. This
addendum supersedes the earlier assertion that expired attempts must remain
pending indefinitely: all-endpoint historical absence **combined with**
finalized expiry, signing age and delayed reobservation can release a sell.
Neither absence nor expiry alone suffices. See the §94 runbook in
`docs/18_SECURITY.md`; correlated dishonest/lagging RPC views remain a risk.

### S1 — H1, expired sell recovery

| ID | severity | file:line | evidence (failing → passing) | fix | test |
|---|---|---|---|---|---|
| H1 | High | `backend/live_execution/executor.py:563`; `commit_log.py:160` | Before command below: 28 failed, 1 passed; after core/CLI command: 39 passed | One shared resolver; signing time; all-endpoint explicit finalized expiry; persisted reobservation; append-only release history; no authority in recovery call | `test_section94_sell_release.py` |
| H1 operator hold | High | `scripts/resolve_pending_sell.py:47` | Before CLI command: 10 failed; same command after: included in 39 passed | Read-only list; typed release attestation; chain-confirmed signature-idempotent adoption; unknown-signature refusal | `test_section94_sell_cli.py` |

Commands run (scratch output under `/tmp/trading-bot-section94`):

```sh
env -u ADMIN_TOKEN .venv/bin/python -m pytest -q backend/tests/test_section94_sell_release.py
# 28 failed, 1 passed in 0.23s (S1-before.log)
env -u ADMIN_TOKEN .venv/bin/python -m pytest -q backend/tests/test_section94_sell_cli.py
# 10 failed in 0.09s (S1-cli-before.log)
env -u ADMIN_TOKEN .venv/bin/python -m pytest -q backend/tests/test_section94_sell_release.py backend/tests/test_section94_sell_cli.py -o faulthandler_timeout=15
# 39 passed in 0.20s (S1-after.log)
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_sell_release.py backend/tests/test_section94_sell_cli.py backend/tests/test_security_sell_recovery.py
# 76 passed in 0.43s (S1-sandbox.log)
```

Failing-first excerpts:

```text
FAILED backend/tests/test_section94_sell_release.py::test_dropped_exit_releases_after_two_observations_and_next_call_sells
E KeyError: 'release_candidate_since'
FAILED backend/tests/test_section94_sell_release.py::test_blockhash_commitment_is_explicit_and_default_unchanged
E TypeError: is_blockhash_valid() got an unexpected keyword argument 'commitment'
FAILED backend/tests/test_section94_sell_cli.py::test_list_is_read_only_and_has_operator_evidence
E ImportError: cannot import name 'resolve_pending_sell' from 'scripts'
```

Pending-buy reproduction: **no wedge reproduced**. The dropped-buy fixture
returns `expired`, clears `pending_buys()` and frees its reserved exposure;
a restarted resolver remains empty. No buy-recovery behavior was changed.

```sh
env -u ADMIN_TOKEN .venv/bin/python -m pytest -q backend/tests/test_pending_buy_resolution.py::test_section94_dropped_buy_does_not_wedge_exposure
# 1 passed in 0.08s (S1-buy-probe.log)
```

The outer sandbox denied nested bubblewrap's NETLINK_ROUTE setup. The trusted
existing runner was then run with elevated launch permissions; target execution
remained inside its network-disabled, secret-excluding scratch sandbox. Earlier
plain pytest attempts were interrupted after transport waits; they are not
full-suite passes. A scratch-only external-HTTP guard produced this additional
affected-suite result:

```sh
PYTHONPATH=/tmp/trading-bot-section94/offline-bootstrap env -u ADMIN_TOKEN .venv/bin/python -m pytest -q backend/tests/test_section94_sell_release.py backend/tests/test_section94_sell_cli.py backend/tests/test_security_sell_recovery.py backend/tests/test_pending_buy_resolution.py backend/tests/test_audit_81_a.py
# 134 passed in 0.50s (S1-final.log)
```

Final regression, mutation, frontend, deployment and live validation: not yet run
by author at this phase. No CI status is claimed.


### S2 — H2, already-closed confirmed sell

| ID | severity | file:line | evidence (failing → passing) | fix | test |
|---|---|---|---|---|---|
| H2 | High | `backend/live_execution/executor.py:691`; `models.py:28` | 9 failing reproductions → affected suite 131 passed; final affected suite 102 passed | Catch only the typed missing-position error as already booked; bind and settle without another close or filled result; other accounting exceptions remain unresolved and converge after repair | `test_section94_sell_settlement.py` |
| H2 operator confirm | High | `scripts/resolve_pending_sell.py:47` | 1 failed → included in final affected suite | CLI uses the same confirmed-sell settlement helper | `test_cli_confirm_already_closed_settles_without_second_close` |

```sh
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_sell_settlement.py
# 9 failed in 0.49s (S2-before.log)
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_sell_settlement.py backend/tests/test_section94_sell_release.py backend/tests/test_section94_sell_cli.py backend/tests/test_security_sell_recovery.py backend/live_execution/tests/test_reconcile.py backend/tests/test_audit_81_a.py
# 131 passed in 0.82s (S2-after.log)
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_sell_cli.py::test_cli_confirm_already_closed_settles_without_second_close
# 1 failed in 0.11s (S2-cli-before.log)
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_sell_settlement.py backend/tests/test_section94_sell_cli.py backend/tests/test_security_sell_recovery.py backend/live_execution/tests/test_reconcile.py backend/tests/test_audit_81_a.py
# 102 passed in 0.45s (S2-final.log)
```

Failing-first excerpts:

```text
FAILED backend/tests/test_section94_sell_settlement.py::test_confirmed_sell_after_oob_close_settles_once_without_pnl_duplication
E ValueError: no open position for So11111111111111111111111111111111111111112
FAILED backend/tests/test_section94_sell_settlement.py::test_accounting_failure_is_unresolved_then_converges[error1-True]
E OSError: fixture write failure
FAILED backend/tests/test_section94_sell_cli.py::test_cli_confirm_already_closed_settles_without_second_close
backend/live_execution/models.py:498: NoOpenPosition
```

`close_out_of_band` still closes independently, now warning and appending the
pending signature to the existing note. The regression checks three recovery
calls, one close record and unchanged realized PnL. Both immediate and recovered
confirmed accounting failures return unresolved and succeed after repair.


### S3 — H3, consumed-field simulation consensus

| ID | severity | file:line | evidence (failing → passing) | fix | test |
|---|---|---|---|---|---|
| H3 | High availability, unmeasured | `backend/live_execution/solana.py:218`; `sim_guard.py:226` | 6 failed, 27 passed → affected suite 84 passed | Structural checks everywhere; exact token controls; ownership discovery using agreed token owner; wallet tolerances; opaque pool bytes/lamports excluded; compare guard F verdict when it consumes vault amounts | `test_section94_consensus.py` |
| H3 observability | High availability, unmeasured | `solana.py:145`; `backend/api/main.py:389` | Same failing/passing commands | Structured refusals; fixed category/side counters with saturated integers; process-local health surface; pre-state attempt log | `test_consensus_refusals_are_structured_and_bounded` |

```sh
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_consensus.py
# 6 failed, 27 passed in 0.12s (S3-before.log)
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_consensus.py backend/tests/test_security_signing_policy.py backend/tests/test_audit_81_c.py backend/tests/test_audit_78_b.py
# 84 passed in 0.49s (S3-after.log)
```

Failing-first excerpts:

```text
FAILED backend/tests/test_section94_consensus.py::test_hot_pool_one_slot_apart_agrees_despite_market_data_and_vault_move
E AssertionError: assert False
FAILED backend/tests/test_section94_consensus.py::test_transfer_fee_withheld_amount_is_the_only_exempt_extension_payload
E AssertionError: assert False
FAILED backend/tests/test_section94_consensus.py::test_real_quorum_accepts_hot_pool_snapshots
E TypeError: simulate_transaction() got an unexpected keyword argument 'wallet_address'
```

Consumed-field evidence (baseline `ea0a899` line references, retained so these
remain stable through later edits):

| Account/field | Consumer at baseline | Consensus rule |
|---|---|---|
| Payer existence/lamports | `sim_guard.py:329`, `377`, `522` | Presence exact; lamports existing tolerance |
| Token program owner | `sim_guard.py:339`, `388`, `430` | Exact all accounts |
| Token mint/token owner | `sim_guard.py:91`, `349`, `402`, `428`, `482`, `499`, `535` | Exact all token accounts; discover wallet ownership only after agreement |
| Wallet token amount | `sim_guard.py:470`, `481`, `499` | Existing amount tolerance, floor one raw unit; aggregate output rule untouched |
| Wallet token lamports | `sim_guard.py:347`, `396`, `513` | Existing lamport tolerance |
| Delegate | `sim_guard.py:403`, `433` | Exact, including foreign token accounts |
| Close authority | `sim_guard.py:408`, `440` | Exact, including foreign token accounts |
| Delegated amount | `sim_guard.py:413`, `447` | Exact, including foreign token accounts |
| Token state | `sim_guard.py:418`, `454` | Exact, including foreign token accounts |
| Account/ATA existence | `sim_guard.py:292`, `369`, `456` | Exact presence and account list shape |
| Foreign token amount | `sim_guard.py:531`–`539` | Check F compares positive foreign increase only when wallet input exceeds intent; comparator checks that predicate on both post snapshots using the guard's chosen pre-state |
| Executable/data length | `solana.py:196` structural consensus | Exact |
| is_native/extension bytes | `solana.py:229`; not individually decoded by guard | Preserve exact control bytes; exempt only TransferFeeAmount u64 payload |
| Raw non-token/non-wallet data and lamports | No guard decision consumes them | Structural facts only |

Protocol layout reference: Token-2022 interface `extension/mod.rs` and
`extension/transfer_fee/mod.rs`: base account length 165, account discriminator
2, TransferFeeAmount TLV type 2, payload length 8. The task forbids network
access: the current upstream spec was **not fetched by author**. Tests use real
base decoding and explicit TLV layouts; unknown extensions stay byte-exact and
malformed TLVs refuse. Upstream-version confirmation remains an offline
validation limit, alongside native approved-DEX exploitability.

The hot-pool fixture has identical wallet facts, different pool bytes/lamports
and a foreign vault amount move; it agrees. Wallet amount, any token control,
pool owner/executable/length and missing-account variants refuse. A one-unit
foreign move that flips F refuses even though a magnitude tolerance could have
accepted it. No raw opaque market-state bytes influence a guard decision.
This repairs the design; actual buy refusal rate is **UNVERIFIED on the VM**.
The log query and process-local counter limitation are in `docs/18_SECURITY.md`.


### S4 — M1, verification honesty

| ID | severity | file:line | evidence (failing → passing) | fix | test |
|---|---|---|---|---|---|
| M1 close authority | Medium | `solana.py:218`; `mutation_check.py:81C_SOLANA_AGREE_CLOSE_AUTH` | Named mutant SURVIVED on S3 → existing control regression fails when active range is removed | Remove redundant decoded comparison; target exact close-authority base-layout range | `test_named_control_field_mutation_is_caught_by_existing_regression` |
| M1 delegated amount | Medium | `solana.py:218`; `mutation_check.py:81C_SOLANA_AGREE_DELEGATED_AMT` | Named mutant SURVIVED on S3 → existing control regression fails when active range is removed | Remove redundant decoded comparison; target exact delegated-amount base-layout range | Same parametrized executable mutation test |
| M1 historical claims | Medium | `handoff.md`; `memory-bank/*`; `docs/20_BEHAVIOUR_CHANGES_SINCE_3FEA445.md` | Fresh baseline run 135/137; S4 full run 149/149 | Append dated corrections without rewriting records; enumerate actual money-path and operational changes | Full no-flags mutation checker and generated log |

Commands/results:

```sh
# Fresh ea0a899 source copy, no .env, keys or real state:
python3 docs/security-remediation/sandbox_runner.py -q
# 2240 passed, 3 skipped, 102 warnings in 37.27s (baseline-sandbox-clean.log)
python3 docs/security-remediation/sandbox_runner.py --command /usr/bin/python3 scripts/mutation_check.py
# All restorations verified clean. Mutations: 137, Caught: 135, Survivors: 2
# (baseline-mutation-sandbox-clean.log; exit 2)
# Fresh a77a8af source copy + new mutation regression test:
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_mutation_evidence.py
# 2 failed in 1.33s (S4-before.log)
# Fixed working source:
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_mutation_evidence.py backend/tests/test_section94_consensus.py backend/tests/test_audit_82_e.py
# 37 passed in 1.42s (S4-after.log)
python3 docs/security-remediation/sandbox_runner.py --command /usr/bin/python3 -c 'import base64,pathlib,subprocess,sys; result=subprocess.run([sys.executable,"scripts/mutation_check.py"]); print("SECTION94_MUTATION_ARTIFACT="+base64.b64encode(pathlib.Path("docs/17_MUTATION_LOG.md").read_bytes()).decode()) if result.returncode == 0 else None; sys.exit(result.returncode)'
# All restorations verified clean. Mutations: 149, Caught: 149, Survivors: 0
# (S4-mutation.log; exit 0)
```

Failing-first excerpts:

```text
E AssertionError: 81C_SOLANA_AGREE_CLOSE_AUTH SURVIVED: it must target the active enforcement
E AssertionError: 81C_SOLANA_AGREE_DELEGATED_AMT SURVIVED: it must target the active enforcement
2 failed in 1.33s
```

The first draft of this new executable test omitted the async plugin in its
child pytest process and failed for infrastructure reasons; that draft is
**not** evidence of caught mutants (`S4-before-invalid-env.log`). The corrected
failing-first run above explicitly enabled the plugin and reproduced both real
survivors. The harness now ensures the async plugin remains available when a
scratch runner disables plugin autoload. Passing mutation regressions require
the existing `test_c2_responses_agree_control_fields_differ` to be the failure,
so missing-plugin errors cannot satisfy them.

The author baseline without frontend produced an additional build-dependent
skip relative to the auditor's stated measurement; no build-dependent skip is
accepted as final verification. Final runs build the existing frontend inside
the sandbox. Early interrupted plain runs and contaminated scratch snapshots
were discarded and are not represented as baseline passes.


### S5 — M2, read-only state and migration preflight

| ID | severity | file:line | evidence (failing → passing) | fix | test |
|---|---|---|---|---|---|
| M2 state validation | Medium | `scripts/validate_state.py:235` | 44 failed before implementation → final affected suites 194 passed | Read-only known-file dispatch; pure runtime validators; per-row errors with safe field labels; duplicate identity detection; exit codes for invalid/unreadable | `test_section94_state_validation.py` |
| M2 precise error labels | Medium | `scripts/validate_state.py:71` | Review reproduction 9 failed → same final affected suites pass | Decode grouped runtime validator messages into specific attempt/mint field labels without values | `test_invalid_sell_attempt_reports_precise_field`; `test_invalid_signed_sell_mint_reports_precise_field` |
| M2 deployment preflight | Medium | `scripts/preflight_deploy.py:844`; `docs/11_DEPLOYMENT.md:208` | Missing check failed in initial suite → valid/invalid/unreadable tests pass | Existing state directories validated before upgrade; copied-state and rollback runbook; legacy sell review | `test_preflight_runs_same_validators`; `test_preflight_unreadable_state_is_a_failure` |

```sh
python docs/security-remediation/sandbox_runner.py backend/tests/test_section94_state_validation.py -q
# 44 failed in 0.27s (S5-before.log)
python docs/security-remediation/sandbox_runner.py backend/tests/test_section94_state_validation.py -q -k 'invalid_sell_attempt or invalid_signed_sell_mint'
# 9 failed, 61 deselected in 0.12s (S5-review-before.log)
python docs/security-remediation/sandbox_runner.py backend/tests/test_section94_state_validation.py backend/tests/test_preflight_deploy.py backend/tests/test_audit_93_r1.py backend/tests/test_security_state_integrity.py backend/tests/test_remediation_wallet_snapshot.py -q
# 194 passed in 3.97s (S5-after.log)
```

Failing-first excerpts:

```text
E ImportError: cannot import name 'validate_state' from 'scripts'
FAILED backend/tests/test_section94_state_validation.py::test_preflight_runs_same_validators[False]
FAILED backend/tests/test_section94_state_validation.py::test_invalid_signed_sell_mint_reports_precise_field
E AssertionError: assert 'index=0 field=payload.mint reason=' in 'commits.json records=1 INVALID\n  index=0 field=record reason=invalid type, shape, or numeric value\n'
```

Read-only evidence: `test_reads_all_sidecars_without_locks_creation_or_writes`
uses read-only file/directory modes, snapshots names/bytes, forbids store save
methods, process locks and Path writes/mkdir, then verifies identical contents.
`test_cli_does_not_create_python_import_caches` removes the bytecode environment
flag in a subprocess and verifies no cache files appear. Unknown/secret
sidecars are not opened; recognized symlinks refuse as unreadable. The tool
never invokes a serialized write or a network API.

Historical shapes found through source history:

| Revision | Written shape / compatibility fixture |
|---|---|
| `fc5505d` | Ledger records envelope; kinds buy/close; required kind/key/mint/usd_size; optional token amount, price, signature, timestamp, PnL; default recorded, buy confirmed, closed buy closed; open set also includes sent |
| `8204741` | Bare commit list; kind/nonce/payload/hash/sealed_at/signature/status; sealed/bound |
| `16c9f73` | Nullable memo fields; published/failed and fail_reason |
| `4c6788e` | Partial/full sell close defaults to recorded with empty signature |
| `3828ec0` | OOB close: closed, empty signature, summed tokens, zero price; unknown proceeds zero usd_size and None PnL; optional note |
| `dc52b35` | Move into backend/live_execution; same persisted schema |
| `2f5e040` | Optional rule_id, including outofband |
| `218e270` | Wallet snapshot ts/usdc/sol plus sol_price_usd/total_usd |
| `13b226a` | Snapshot ts/usdc/sol; acknowledgment metadata reason/acknowledged_by in same file |
| `92397cf` | Commit notes with text/at |
| `4c29aa0` | Pending-buy blockhash/commit/memo/reason metadata; pending/failed/expired/confirmed settlement |
| `fd7641a` | Confirmed replacement preserves pending timestamp/metadata |
| `7e6e6c3` | Confirmed replacement clears reason; proven adoption may bind failed commit |
| `04e1feb` | Sell attempt terms/signature/blockhash/status and append-only history, without signed_at; signature-keyed sell close records |

Investigation commands actually run:

```sh
git log --all -p -- backend/live_execution/models.py
git log --all -p -- backend/live_execution/commit_log.py backend/live_execution/reconcile.py
git log --follow --format='%h %s' -- backend/live_execution/models.py
git log --follow --format='%h %s' -- backend/live_execution/commit_log.py
git log --all -S 'ExecutionRecord' --format='%h %s' -- backend/live_execution/models.py
git log --all -S 'wallet_snapshot' --format='%h %s' -- backend/live_execution/reconcile.py
git log --all -S 'rec["status"]' --format='%h %s' -- backend/live_execution/commit_log.py
git log --all -S 'kind=' --format='%h %s' -- backend/live_execution/models.py
git log --all -S 'status=' --format='%h %s' -- backend/live_execution/models.py
git log --all -p -- scripts/restore_state.py scripts/backup_state.py
git show fc5505d:live_execution/models.py
git show 8204741:live_execution/commit_log.py
git show 16c9f73:live_execution/commit_log.py
git show 218e270:backend/live_execution/reconcile.py
git show 13b226a:backend/live_execution/reconcile.py
```

No runtime validator loosening was necessary. Missing optional price/timestamp/
token fields, missing/empty signature, memo-less commits, old snapshot extras,
OOB unknown PnL and unsigned-time legacy attempts already load; tests now prove
it. Present malformed types and nonfinite required numeric fields still refuse.
Dataclass writers always serialize status, including its default, so no
historical writer lacking status was found. Restore-only operator aliases
OPEN/PARTIAL/open/partial are not an observed repository execution-writer
schema and remain refused: accepting them without normalization would silently
omit exposure, while normalization would exceed the permitted behavior scope.

The real VM's state was not read. Its acceptance is **UNVERIFIED**; the operator
must validate a retained scratch copy before recreating containers. Container
source paths were checked in compose source, without running Docker.


### S6 — M3/M4, explicit HTTP policy and CWD consistency

| ID | severity | file:line | evidence (failing → passing) | fix | test |
|---|---|---|---|---|---|
| M3 | Low | `backend/bounded_http.py:22`, `:56` | 8 failing HTTP/CWD reproductions → 47 affected tests pass, then integrated 68 pass | Refuse explicit redirects/incompatible explicit encoding; case-insensitive identity header; warning once per host/hour with bounded memory and no URL secrets | `test_section94_http_policy.py`; streamed body/timeout neighbors |
| M4 docker context | Low | `backend/tests/test_remediation_hardening.py:327` | FileNotFoundError after CWD change → passing regression from root and backend | Locate .dockerignore via Path(__file__) | `test_dockerignore_regression_is_independent_of_working_directory` |
| M4 subprocess source scan | Low | `backend/tests/test_audit_77_a.py:70` | 3 failing real grep regressions → root/backend affected suites 55 pass each | Absolute source path; grep errors fail instead of passing without a scan | `test_captcha_source_scan_executes_from_backend_cwd` clean/hostile/missing variants |
| Hold test consistency | Low | `backend/tests/test_audit_79_b.py` | First full root integration: 2 failed, 2411 passed → final root/backend each 2425 pass | Assert actionable missing-blockhash/age reasons and unresolved state; strengthen unchanged-accounting and no-blockhash-RPC assertions; preserve all hold cases | Existing double-fill tests and new S1 release dimensions |

Commands and results:

```sh
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_http_policy.py
# 8 failed in 0.12s (S6-before.log)
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_http_policy.py backend/tests/test_remediation_hardening.py
# 47 passed in 0.54s (S6-after.log)
python3 docs/security-remediation/sandbox_runner.py --command /bin/bash -c 'cd backend && /usr/bin/python3 -m pytest -q tests/test_section94_http_policy.py tests/test_remediation_hardening.py'
# 47 passed in 0.51s (S6-backend-cwd.log)
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_http_policy.py -k captcha_source_scan
# 3 failed, 8 deselected, 1 warning in 0.19s (S6-scan-before.log)
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_http_policy.py backend/tests/test_remediation_hardening.py backend/tests/test_audit_77_a.py
# 55 passed, 1 warning in 0.57s (S6-final-after.log)
python3 docs/security-remediation/sandbox_runner.py --command /bin/bash -c 'cd backend && /usr/bin/python3 -m pytest -q tests/test_section94_http_policy.py tests/test_remediation_hardening.py tests/test_audit_77_a.py'
# 55 passed, 1 warning in 0.57s (S6-final-backend-cwd.log)
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_http_policy.py backend/tests/test_remediation_hardening.py backend/tests/test_audit_77_a.py backend/tests/test_audit_79_b.py
# 68 passed, 1 warning in 0.66s (S6-integrated.log)
python3 docs/security-remediation/sandbox_runner.py --with-frontend -q -rs
# 2425 passed, 102 warnings in 35.56s (full-root-final.log); no failures/skips
python3 docs/security-remediation/sandbox_runner.py --with-frontend --command /bin/bash -c 'cd backend && /usr/bin/python3 -m pytest -q -rs'
# 2425 passed, 102 warnings in 35.23s (full-backend-cwd-final.log); no failures/skips
```

Failing-first excerpts:

```text
FAILED backend/tests/test_section94_http_policy.py::test_explicit_follow_redirects_true_is_refused_before_request
E Failed: DID NOT RAISE <class 'ValueError'>
FAILED backend/tests/test_section94_http_policy.py::test_dockerignore_regression_is_independent_of_working_directory
E FileNotFoundError: [Errno 2] No such file or directory: '.dockerignore'
FAILED backend/tests/test_section94_http_policy.py::test_captcha_source_scan_executes_from_backend_cwd[marker]
```

Whole-tree read/subprocess audits used:

```sh
rg -n 'Path\(["\x27]|open\(["\x27]|os\.(getcwd|chdir)|cwd=' backend/tests backend/live_execution/tests -g '*.py'
rg -n '(read_text|read_bytes|open)\(' backend/tests backend/live_execution/tests -g '*.py'
rg -n 'subprocess\.(run|check_output|check_call|Popen)' backend/tests backend/live_execution/tests -g '*.py'
rg -n -i 'accept.encoding|follow_redirects' backend scripts -g '*.py' -g '!test_*.py'
rg -n 'http://' .env.example docker-compose.yml backend/config.py backend/live_execution/config.py backend/llm backend/data_providers
```

The production-call AST audit recorded **30 calls, zero explicit
follow_redirects keywords** (`S6-callsite-audit.log`). The command parsed each
backend/scripts production Python AST, selected bounded_request call nodes and
printed location plus explicit keyword names; tests were excluded. No affected
production caller was found. Other test-relative literals are synthetic
fixtures or use an explicit repository CWD; subprocess scans now check real
source from either invocation directory. Early full runs copied in-progress
state-label code and obsolete reason assertions, so their failures are retained
as diagnostic evidence and superseded by the final complete runs above.

**Ancillary HTTP API-policy behavior change:** explicitly requesting redirects
or non-identity encoding now raises ValueError; mixed-case identity is
canonicalized. This is the requested S6 policy clarification, in addition to
the four trading/preflight behavior items listed in S7. No production caller
explicitly requested those options in the source audit.

**UNVERIFIED providers:** documented SearXNG bases `http://searxng:8080` and
`http://127.0.0.1:8888` in `.env.example:277`–`278` (compose uses the service
URL). No source-known redirect was identified; actual custom RPC, LLM, Jupiter,
alert, scrape/research proxy and SearXNG URLs were neither read from secrets nor
contacted. Their redirect behavior remains operator validation.


### S7 — operational record and final review

| ID | severity | file:line | evidence (failing → passing) | fix | test |
|---|---|---|---|---|---|
| H1 operator evidence detail | High finding, observability follow-up | `scripts/resolve_pending_sell.py:47`; `backend/live_execution/executor.py:563` | Final review: 1 failed → 88 affected checks pass | List every release condition as met, unmet or explicitly unverified; no extra RPC, write or release decision | `test_list_is_read_only_and_has_operator_evidence` |
| M1 final record | Medium | `docs/17_MUTATION_LOG.md`; handoff/memory addenda | Full final no-flags run 149 caught, zero survivors; final root/backend suites each 2425 passed | Generated log copied as data out of disposable sandbox; command-backed counts only; dated historical corrections retained | Full mutation checker, canonical gate stage and backend-CWD suite |
| M2 upgrade documentation | Medium | `docs/11_DEPLOYMENT.md`; `docs/18_SECURITY.md`; `.env.example` | Historical fixtures and read-only tests pass; actual VM not run by author | Preserve upgrade copy; review old unsigned-time sells; conservative environment example; mark external validation limits | State validation suite and final source review |

Final review caught a reporting omission in CLI list: terminal rows previously
printed `conditions=None`, without enumerating met/unmet facts. The read-only
listing now includes distinct hosts, all-ABSENT, finalized expiry, signing age
and delayed reobservation; a skipped RPC check is labeled unverified. This
changes operator diagnostics only and adds no release decision or network call.

```sh
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_sell_cli.py::test_list_is_read_only_and_has_operator_evidence
# 1 failed in 0.09s (S7-list-before.log)
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_sell_release.py backend/tests/test_section94_sell_settlement.py backend/tests/test_section94_sell_cli.py backend/tests/test_security_sell_recovery.py backend/tests/test_audit_82_e.py
# 88 passed in 0.47s (S7-list-after.log)
```

```text
E           AssertionError: assert 'distinct_hosts=' in 'mint=So11111111111111111111111111111111111111112 signature=known-signature age=1794453629s status=confirmed conditions=None\nhttps://solscan.io/tx/known-signature\n'
FAILED backend/tests/test_section94_sell_cli.py::test_list_is_read_only_and_has_operator_evidence
1 failed in 0.09s
```

Final commands after that source change:

```sh
python3 docs/security-remediation/sandbox_runner.py --with-frontend --command /bin/bash scripts/prepush_check.sh --stage 1
# 2425 passed, 102 warnings in 34.84s; PRE-PUSH STAGE 1 PASSED
# gate-stage1-final.log; root invocation; no failures or skips
python3 docs/security-remediation/sandbox_runner.py --with-frontend --command /bin/bash -c 'cd backend && /usr/bin/python3 -m pytest -q -rs'
# 2425 passed, 102 warnings in 34.70s; no failures or skips
# backend-cwd-last.log
python3 docs/security-remediation/sandbox_runner.py --command /usr/bin/python3 -c 'import base64,pathlib,subprocess,sys; result=subprocess.run([sys.executable,"scripts/mutation_check.py"]); print("SECTION94_MUTATION_ARTIFACT="+base64.b64encode(pathlib.Path("docs/17_MUTATION_LOG.md").read_bytes()).decode()) if result.returncode == 0 else None; sys.exit(result.returncode)'
# All restorations verified clean. Mutations: 149, Caught: 149, Survivors: 0
# S7-mutation-final.log; checker receives NO flags
python3 docs/security-remediation/sandbox_runner.py --command /usr/bin/python3 scripts/audit_runtime_writes.py --check
# exit 0; inventory clean (runtime-writes.log)
python3 docs/security-remediation/sandbox_runner.py --with-frontend --command /usr/bin/python3 -c 'import subprocess,sys; stages=[2,4,5,6,9]; results=[(stage,subprocess.run(["/bin/bash","scripts/prepush_check.sh","--stage",str(stage)]).returncode) for stage in stages]; print("SECTION94_GATE_RESULTS",results); sys.exit(any(code for stage,code in results))'
# SECTION94_GATE_RESULTS [(2, 0), (4, 0), (5, 0), (6, 0), (9, 0)]
# gate-local-stages.log
python3 docs/security-remediation/sandbox_runner.py --gate-e2e
# 41 passed (14.9s); PRE-PUSH STAGE 8 PASSED (gate-stage8.log)
git diff --check
# exit 0
```

These are the actual required pre-push stage dispositions:

| Stage | Result from author execution |
|---|---|
| Backend | PASS, final root suite above; separate backend-CWD suite also passes |
| Python dependency consistency | PASS: No broken requirements found |
| Python advisories | **UNAVAILABLE / RED**: initial missing console script; scratch wrapper for installed module then failed resolving PyPI; no clean advisory status |
| API keypair AST isolation | PASS |
| TypeScript | PASS: actual `npx tsc --noEmit` stage |
| Production build | PASS: actual npm build and setup bundle secret scanner |
| npm advisories | **UNAVAILABLE / RED**: npm registry DNS/network unavailable; no clean advisory status |
| Playwright | PASS against isolated mock backend and existing Chromium |
| Secret scan dispatch | Stage exits successfully under its existing missing-gitleaks policy; **local secret scan not run by author** |

Advisory attempts, all with networking disabled:

```sh
python3 docs/security-remediation/sandbox_runner.py --command /bin/bash scripts/prepush_check.sh --stage 3
# exit 127: pip-audit: command not found (gate-stage3.log)
python3 docs/security-remediation/sandbox_runner.py --command /usr/bin/python3 -c 'from pathlib import Path; import subprocess,sys; bin=Path(".venv/bin"); bin.mkdir(parents=True); (bin/"python").symlink_to("/usr/bin/python3"); tool=bin/"pip-audit"; tool.write_text("#!/bin/sh\nexec /usr/bin/python3 -m pip_audit \"$@\"\n"); tool.chmod(0o755); sys.exit(subprocess.run(["/bin/bash","scripts/prepush_check.sh","--stage","3"]).returncode)'
# exit 1: requests.exceptions.ConnectionError / NameResolutionError: pypi.org
# gate-stage3-adapted.log
python3 docs/security-remediation/sandbox_runner.py --with-frontend --command /bin/bash scripts/prepush_check.sh --stage 7
# exit 1: getaddrinfo EAI_AGAIN registry.npmjs.org (gate-stage7.log)
```

**The complete pre-push gate is RED. No push was performed.** The user's
no-network rule prevents completing the registry advisory lookups. The author
requested a specific exception for the existing manifest-only advisory runner;
without approval, leave commits local. No new hosted CI status is claimed.

Independent S1/S2 source review and regression command:

```sh
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_sell_release.py backend/tests/test_section94_sell_settlement.py backend/tests/test_section94_sell_cli.py backend/tests/test_security_sell_recovery.py
# 86 passed in 0.46s (review-S1-S2-targeted.log); no concrete correctness blocker found
```

The final source diff was re-read for release evidence, double accounting,
signature identity, policy changes and information disclosure. Existing tests
`test_unresolved_negative_or_ambiguous_send_never_releases_intent`,
`test_receipt_signature_mismatch_resolves_only_locally_signed_identity`,
`test_enabled_executor_approval_mismatch_before_memo`,
`test_output_minimum_is_net_wallet_gain`,
`test_overlapping_process_writes_preserve_every_record` and
`test_process_crash_releases_lock_and_nested_same_file_is_reentrant` are in the
passing full suite. The diff of existing sizing/signing/exit/cap/container
configuration files showed only the new release-age configuration in live
config; the existing risk constants and core decision modules were not edited.
Native approved-DEX exploitability remains unproven; source invariants and
dummy-layout fixtures do not establish a native-program exploit or its absence.

Evidence-scope correction: early plain pytest attempts were interrupted after
unmocked read-only transport waits and do not prove OS isolation or external
delivery. They are not full-suite passes. All retained complete regression,
mutation, build, browser and gate results use the existing trusted source-copy
runner with external networking disabled and real state/environment files
excluded. The discarded contaminated scratch baselines were rebuilt from the
requested revision before measurement. No historical result is reused as a
current verification claim.

**UNVERIFIED / not run by author:**

- Actual VM state acceptance and restart/reconciliation after deployment; run the copied-state pre-upgrade procedure.
- Docker image/runtime behavior on the VM; source compose controls and isolated mocks were checked only.
- Supabase connectivity, certificate/pin setup and deployed permissions.
- Real RPC buy refusal rate and correlated dishonest/lagging endpoint risk; use the burn-in log query.
- Provider redirects/custom URLs, including documented HTTP SearXNG local-service examples.
- Current upstream Token-2022 extension specification/version alignment; public spec was not fetched under the no-network rule.
- Python/npm advisory status and a local gitleaks scan; dependency consistency and tests do not prove CVE cleanliness.
- Current hosted CI, production wallet/fill behavior, native approved-DEX exploitability.

Behaviour changes:

- (a) expired sell attempts are released on all-endpoint positive evidence
- (b) a confirmed sell on an already-closed position settles instead of looping
- (c) simulation consensus no longer requires byte-equal pool state
- (d) preflight now validates state files

**Additional requested HTTP policy clarification:** explicit redirect following
and incompatible explicit Accept-Encoding now raise ValueError; returned
redirects are observable. This ancillary API policy change is disclosed in S6
and the behavior map; the source audit found no production caller requesting
those options. Operator tooling and list diagnostics implement the requested
recovery runbook without changing trade sizing, gates, the exit ladder, caps,
kill switch, arming or signing authorization.

Source history: `ea0a899` → S1 `5177a77` → S2 `17a26ca` → S3 `a77a8af` →
S4 `abfe549` → S5 `2db2070` → S6 `4edcb7f` → S7 this commit. Original audit
findings remain above. Overall release/push completion remains blocked by the
red advisory stages; implementation/test evidence does not waive that gate.

VERDICT: FAIL -- complete pre-push gate unavailable under the no-network constraint.


## §94 S8 — approved advisory verification and publication follow-up (2026-11-12)

The user explicitly authorized the existing manifest-only Python/npm advisory
runner and publication if the gate is green. This supersedes the earlier
publication hold and unavailable-advisory dispositions; their historical
outputs remain intact above. This forward-only documentation follow-up
preserves the seven S1–S7 phase commits without amending existing history.
No runtime source or dependency was changed for this follow-up.

| ID | severity | file:line | evidence | disposition | verification |
|---|---|---|---|---|---|
| Advisory gate, Python | Operational verification | `docs/security-remediation/advisory_stage.py:1` | Approved stage exits successfully; `No known vulnerabilities found` | PASS for the pinned requirements at lookup time | Actual canonical pre-push stage through manifest-only scratch runner |
| Advisory gate, npm | Operational verification | `docs/security-remediation/advisory_stage.py:1` | Approved stage exits successfully; `found 0 vulnerabilities` | PASS for production dependency advisories at lookup time | Actual canonical pre-push stage through manifest-only scratch runner |

Exact commands and observed output:

```sh
python3 docs/security-remediation/advisory_stage.py 3
# gate-stage3-approved.log; exit 0
=== [3/9] Auditing Python dependencies for vulnerabilities (pip-audit) ===
=== PRE-PUSH STAGE 3 PASSED ===
WARNING:pip_audit._cli:--no-deps is supported, but users are encouraged to fully hash their pinned dependencies
WARNING:pip_audit._cli:Consider using a tool like `pip-compile`: https://pip-tools.readthedocs.io/en/latest/#using-hashes
No known vulnerabilities found

python3 docs/security-remediation/advisory_stage.py 7
# gate-stage7-approved.log; exit 0
=== [7/9] Auditing frontend production dependencies (npm audit --omit=dev) ===
found 0 vulnerabilities
=== PRE-PUSH STAGE 7 PASSED ===
```

The runner copies only the canonical gate and dependency manifests into a
disposable directory, supplies an empty allowlisted environment and scratch
HOME/cache/config paths, invokes installed trusted tools, and disables npm
scripts. It installs no dependencies and exposes no live state, wallet, keys
or real environment file. Only these approved registry lookups used external
network access; test/build/browser execution remains in the network-disabled
scratch sandbox. The hash warnings above are retained, not suppressed.

All required local stages now have observed successful exits. The earlier
root/backend runs remain 2425 passed each with no failures/skips, the no-flags
mutation run remains 149 caught out of 149 with no survivors, and Playwright
remains 41 passed. Their exact commands and outputs are in S7; no runtime code
changed afterward. The existing secret-scan stage still follows its documented
missing-local-gitleaks policy: **no clean local secret-scan claim**. Dependency
advisories are a separate executed check, not an inference from unit tests.

Publication is now authorized by the green local staged gate. Git and the
final delivery message record whether the push succeeds; no current hosted CI
status is claimed. Actual VM state/restart, Docker deployment, Supabase, real
RPC refusal rate, provider redirects, upstream Token-2022 spec alignment and
native approved-DEX exploitability remain unverified as described above.


Documentation-dependent verification after this addendum:

```sh
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_docker_entrypoint.py backend/tests/test_audit_82_e.py
# 29 passed in 6.26s; publication-doc-checks.log
```

`git diff --check` passed. `git diff 00f9397 -- backend scripts frontend .env.example`
was empty, confirming this follow-up changes documentation only.

VERDICT: PASS


## §94 S9 — hosted gitleaks and test-runner follow-up (2026-11-12)

The user reported failures after publication of `38683c6`. Actual GitHub
results were inspected, rather than inferring CI status from local checks:

- [gitleaks run 43218098584](https://github.com/hixam001/trading-bot/actions/runs/43218098584) failed on the public mutation-test identifier at the exact fingerprint below.
- [Tests & Security Audit run 43155049403](https://github.com/hixam001/trading-bot/actions/runs/43155049403) failed in the hermetic pytest step. Its two failed cases were the mutation evidence tests; the Docker build/compose/ROFS job passed.

| ID | severity | file:line | evidence (failing → passing) | fix | test |
|---|---|---|---|---|---|
| Hosted plugin discovery | Test infrastructure | `scripts/mutation_check.py:1646`; `backend/tests/test_section94_mutation_evidence.py:8` | CI: 2 failed; local reproduction: 2 failed, 2 passed → 6 affected checks passed; complete suites 2427 passed each | Explicitly disable plugin auto-discovery only in mutation subprocesses while retaining the explicit required asyncio plugin; parent/CI test collection is unchanged | Both incoming auto-discovery modes execute real mutants and require the exact control-field assertion to fail |
| Hosted secret-scan false positive | Scanner finding, public identifier | `.gitleaksignore`; historical `backend/tests/test_section94_mutation_evidence.py:7` | CI-pinned full-history scan: one finding before, zero after; unrelated synthetic-secret control still exits with a finding | Add only the exact historical commit/file/rule/line fingerprint; no rule, path or broad pattern exclusion | Actual checksum-verified gitleaks history scans and canonical stage, not source-search assertions |

The historical finding is:

```text
abfe549b416c7ea29b9d322c1f2ac1d895f0e0b7:backend/tests/test_section94_mutation_evidence.py:generic-api-key:7
```

That line contains public names of mutation cases used by the executable
regression; it does not contain a credential. Existing scanner rules are
unchanged. The added exclusion is intentionally limited to that fingerprint.
An unrelated synthetic credential was committed only to a disposable Git
clone and remained detectable after the exclusion. No probe value is included
in the project history or this report.

Root cause of the test failure: CI normally discovers pytest plugins through
entry points. The mutation helper also explicitly requested pytest-asyncio,
registering the same plugin again under another name. Earlier local scratch
runs disabled auto-discovery and missed this combination. The new regression
exercises both incoming environments. It still demands the existing named
control-field test, so a startup error cannot satisfy the expected catch.
The direct startup reproduction reported:

```text
ValueError: Plugin already registered under a different name: pytest_asyncio.plugin=<module 'pytest_asyncio.plugin' from '/usr/lib/python3.14/site-packages/pytest_asyncio/plugin.py'>
```

Observed commands/results (logs in `/tmp/trading-bot-section94-ci`):

```sh
env -u GH_DEBUG gh run view 43218098584 --repo hixam001/trading-bot --json headSha,status,conclusion,url,jobs
env -u GH_DEBUG gh run view 43155049403 --repo hixam001/trading-bot --json headSha,status,conclusion,url,jobs
# Both inspected runs concluded failure on head 38683c6; specific failed steps above.
# Failed logs retrieved with gh run view <id> --log-failed, stored privately; only
# fingerprint metadata/test errors were printed, not potential secret matches.

python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_mutation_evidence.py
# Before helper fix: 2 failed, 2 passed in 1.54s (plugin-before.log).
python3 docs/security-remediation/sandbox_runner.py --command /bin/bash -c 'unset PYTEST_DISABLE_PLUGIN_AUTOLOAD; /usr/bin/python3 -m pytest --collect-only -q backend/tests/test_audit_81_c.py'
# Direct bad-environment reproduction: duplicate-plugin ValueError, exit 1.
python3 docs/security-remediation/sandbox_runner.py -q backend/tests/test_section94_mutation_evidence.py backend/tests/test_audit_82_e.py
# After fix: 6 passed in 2.34s (plugin-after.log).

# Exact scanner version from the failed CI log: 8.24.3.
gh release download v8.24.3 --repo gitleaks/gitleaks --pattern gitleaks_8.24.3_linux_x64.tar.gz --pattern gitleaks_8.24.3_checksums.txt --dir /tmp/trading-bot-section94-ci/scanner
# Archive SHA256 verified against the official checksums file before extraction.
python3 /tmp/trading-bot-section94-ci/scan_history.py --label gitleaks-before
# exit 1, one finding: the exact historical fingerprint above.
python3 /tmp/trading-bot-section94-ci/scan_history.py --label gitleaks-after
# exit 0, no findings after the narrow exclusion.
python3 /tmp/trading-bot-section94-ci/scan_history.py --label gitleaks-unrelated-secret --probe
# EXPECTED exit 1, exactly one finding in scanner_probe.py (synthetic control).
python3 /tmp/trading-bot-section94-ci/scan_history.py --label gate-stage9 --gate
# Actual canonical scripts/prepush_check.sh --stage 9 exits 0 with the pinned scanner.
```

The trusted scratch scanner helper clones Git history locally without a
checkout, copies only the current scanner config/ignore file and canonical
gate, and invokes the checksum-verified binary inside a network-unshared,
read-only OS sandbox. Output is redacted and summaries include fingerprint
metadata only. Its ordinary scan command is:

```sh
gitleaks git . --config=.gitleaks.toml --gitleaks-ignore-path=.gitleaksignore --redact=100 --no-banner --log-opts=--all --report-format=json --report-path=/scratch/report.json
```

The actual gate uses an output-redaction wrapper around the same pinned
binary; all finding/error exit codes remain authoritative. No repository
security rule or pre-push gate was disabled.

Final fresh verification, with ordinary CI plugin discovery enabled for the
complete root/backend suites:

```sh
python3 docs/security-remediation/sandbox_runner.py --with-frontend --command /bin/bash -c 'unset PYTEST_DISABLE_PLUGIN_AUTOLOAD PYTEST_PLUGINS; /bin/bash scripts/prepush_check.sh --stage 1'
# 2427 passed, 102 warnings in 42.13s; no failures/skips; stage passed.
python3 docs/security-remediation/sandbox_runner.py --with-frontend --command /bin/bash -c 'unset PYTEST_DISABLE_PLUGIN_AUTOLOAD PYTEST_PLUGINS; cd backend && /usr/bin/python3 -m pytest -q -rs'
# 2427 passed, 102 warnings in 42.23s; no failures/skips.
python3 docs/security-remediation/sandbox_runner.py --command /usr/bin/python3 -c 'import base64,pathlib,subprocess,sys; result=subprocess.run([sys.executable,"scripts/mutation_check.py"]); print("SECTION94_CI_MUTATION_ARTIFACT="+base64.b64encode(pathlib.Path("docs/17_MUTATION_LOG.md").read_bytes()).decode()) if result.returncode == 0 else None; sys.exit(result.returncode)'
# No-flags checker: 149 caught out of 149, zero survivors; restoration verified.
python3 docs/security-remediation/sandbox_runner.py --with-frontend --command /usr/bin/python3 -c 'import subprocess,sys; stages=[2,4,5,6]; results=[(stage,subprocess.run(["/bin/bash","scripts/prepush_check.sh","--stage",str(stage)]).returncode) for stage in stages]; print("SECTION94_CI_GATE_RESULTS",results); sys.exit(any(code for stage,code in results))'
# SECTION94_CI_GATE_RESULTS [(2, 0), (4, 0), (5, 0), (6, 0)].
python3 docs/security-remediation/advisory_stage.py 3
# exit 0; no known vulnerabilities found; existing hash warnings retained.
python3 docs/security-remediation/advisory_stage.py 7
# exit 0; found 0 production vulnerabilities.
python3 docs/security-remediation/sandbox_runner.py --gate-e2e
# 41 passed (17.2s); actual canonical browser stage passed.
git diff --check
# exit 0.
```

Only development verification infrastructure, the exact scanner fingerprint
exclusion and evidence records change. Trading/signing/runtime modules,
dependency manifests, workflow requirements and original audit findings are
preserved. All target tests and scanner execution use disposable state/source
with external networking disabled; only authorized GitHub inspection/tool
retrieval, manifest advisory queries and publication require external access.

The reported hosted failures are confirmed. Fresh local checks pass. The new
commit's hosted outcomes must be read after publication and will be reported
by the delivery message; they are not inferred here. Real VM state, real-money
execution and the previously listed native-exploitability limits remain
unverified by these CI repairs.

VERDICT: PASS
