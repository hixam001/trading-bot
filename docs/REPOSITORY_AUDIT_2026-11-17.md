# Repository audit — 2026-11-17

The audit reviewed the complete repository's material entry points and trust
boundaries, fixed the verified security and correctness issues below, and
rewrote the [Oracle deployment guide](05_ORACLE_DEPLOY_GUIDE.md). This is a
source and isolated local audit, not certification of a running deployment
or a guarantee that no undiscovered defects remain.

## Scope and evidence

Baseline: commit `1cfd515301ff17152c162d8ecf2ccda33569948f`. Pre-existing
untracked `.agents/` and `skills-lock.json` were preserved. The reviewed
working-tree repairs are not committed by this audit.

The standard audit covered 27 recorded surface/boundary/subsystem/class
units: API/auth/browser, providers and model action binding, signing and RPC,
pending recovery and accounting, operator recovery tools, encrypted storage,
deployment/CI, notifications, and durable blocklist/break state. Independent
coverage reviews added omitted boundaries and accepted the final source and
change review without remaining source-review assignments. Compatible prior
audit evidence was compared with current source; it was not treated as proof
that unchanged-looking areas were safe.

All target execution used disposable source and dummy state in a Linux
namespace sandbox with external networking disabled, empty allowlisted
environment, read-only tools/source and bounded scratch, CPU, memory,
processes, files, output and elapsed time. Browser tests used only an isolated
mock API and local Chromium. No deployed endpoint, real signing identity,
paid provider, production database or cloud control plane was tested.

## Confirmed security findings — repaired

Both findings concern the reviewed baseline and are repaired in this worktree.
Each received independent candidate validation and a separate final record
verification. Severity describes the demonstrated result, not hypothetical
financial loss.

| Severity | Finding | Observed local result | Repair |
| --- | --- | --- | --- |
| Medium | Manual execution trusted a foreign receipt or unproven failure | Mocked post-send responses produced confirmed/failed/confirmed rows where the locally signed attempt should remain pending. | Manual and automatic execution share transaction-identity/finality handling; unresolved outcomes retain the local pending reservation. |
| Medium | An unrelated receipt satisfied the pre-fill memo gate | A native fixture with two disposable signed memo transactions returned the foreign signature as publication of the current commitment. | Derive identity from the exact signed memo bytes, reject receipt mismatch, and require a complete strict successful confirmation. |

No actual swap, mainnet broadcast, duplicate fill, theft or financial loss was
observed. Static instruction checks, immutable intents and simulation remain
independent pre-sign controls. A failed buy still counts toward the existing
daily deployed budget; the manual finding does not claim that budget was
released.

Malformed quote impact was independently reproduced and repaired as a
risk-control bug. It is not reported as a separate security exploit: the
audit did not establish a token holder's control over the upstream measurement
or demonstrate an over-cap signed transaction.

## Other repairs

- Quotes require a known finite, nonboolean price impact before risk checks.
  Missing, malformed and NaN values no longer become zero or pass comparison.
- Partial sales reduce every open lot's token quantity and cost proportionally,
  preserve full-close behavior and deduplicate settlement by signature.
- Manual execution enforces the daily deploy cap, records pre-fill commit/memo
  provenance in pending rows, and accepts an explicit CLI reference price.
- Confirmation normalization preserves missing/error/type information rather
  than synthesizing successful null-error results from incomplete RPC data.
- The explicit devnet simulation option can use one RPC; ordinary signer
  paths retain their normal quorum. Drill receipts must match the local tx.
- Malformed or incomplete mint-extension data remains unknown, rotates to
  another RPC and is not cached as known safe. Observed dangerous extensions
  remain visible. Default live sizing now applies the unknown-security haircut.
- Non-ASCII credentials and malformed cookies fail authentication cleanly.
  Invalid, inaccessible or wrong-shaped revocation state cannot silently
  restore a session. Ledger, commit, approval, kill and break readers also
  distinguish absent state from unreadable/non-file state.
- Blocklist policy writes are serialized and atomic, schema-checked reads
  follow the existing documented recovery policy, and failed persistence no
  longer reports success. Kill/break updates and wallet-baseline writers use
  shared file locking. The intentional nonfatal blocklist recovery policy
  and human-owned arming/confirmation flags were preserved.
- Vanished-position repair verifies the expected wallet and rejects invalid
  or negative proceeds before work.
- Security-monitor IP registries and per-IP histories are bounded. Dropped
  alert queue entries complete their task accounting; refusal digests obey
  the global delivery cap. Alert verification fails when no recipient exists.
- Browser login supports internal PoW and configured Turnstile/hCaptcha
  widgets, with public site-key metadata and provider-specific CSP allowances.
  Server verification remains mandatory; provider secrets remain private.
- Mint history cancels obsolete requests and retries after closing/changing
  the view. Cached wallet scans report their actual successful scan time.
- Postgres paper-book maintenance is transactional. Backup offsite staging
  uses a private unique directory.
- Preflight checks runtime credential entropy, dedicated-secret separation,
  CAPTCHA prerequisites, rendered service environments, wallet bind selection
  and dotenv comments. Compose keeps the host wallet selector out of the API.
  Error diagnostics do not echo Compose's potentially secret-bearing output.
- Runtime-write inventory and the affected mutation declaration were updated
  to retain the existing verification gates after the repairs.

## Final local verification

| Check | Result |
| --- | --- |
| Baseline backend suite | 2,458 passed; 3 skipped |
| Final backend suite | **2,521 passed; 3 skipped** |
| New backend regression cases | **63** |
| Complete Chromium suite against isolated mock API | **43 passed** |
| TypeScript and Vite production build | Passed |
| Built frontend secret scan | Passed |
| Installed Python dependency consistency (`pip check`) | Passed |
| Deployment shell syntax and runtime-write inventory | Passed |
| Final structured findings and coverage validators | Passed |
| Independent source/change review | No remaining must-fix finding from that review |

The backend suite retains 102 existing Starlette test-client deprecation
warnings. Three tests remain skipped by their existing conditions. Updated
fixtures now carry real risk measurements/native signature identities;
expectations that explicitly preserved unsafe behavior were corrected.

## Deployment readiness and limits

The repository's local verification gates pass. **A production Oracle
deployment has not been validated or started.** The rewritten guide separates
API startup from the already-armed signer, removes plaintext whole-volume
backup instructions and stale secret paths, and documents ownership,
one-engine state handoff, HTTPS, mounted-state validation and rollback.

Before unattended execution, the operator must still observe:

1. A native ARM image build and actual read-only-root container smoke on the
   VM/CI; browser installation if that optional transport is enabled.
2. Current package/container advisories and CI security scans. Offline
   dependency consistency is not a fresh vulnerability-database audit.
3. Host preflight with the real private configuration, mounted-state validity,
   correct wallet identity, adequate reserves and exactly one live engine.
4. Actual Oracle/host firewall and proxy settings, HTTPS/private-route behavior,
   applied Supabase migrations/grants if used, independent RPC/provider
   availability and configured external CAPTCHA behavior if enabled.
5. A separate disposable-wallet devnet drill, observed notification delivery,
   encrypted offsite backup plus restore drill, and supervised stop/reboot.

No unresolved source-grounded security candidate remains in the final audit
records. These external deployment checks remain mandatory release work, not
assumed successes. The machine-readable evidence and detailed reproduction
records are retained outside the repository in the local security audit run.
