# Behaviour changes since `3fea445`

Added 2026-11-12 during §94. Historical records remain unchanged. This is an
operator compatibility map, not a claim that production or CI was validated.

| change | commit | operator-visible effect | rollback/mitigation |
|---|---|---|---|
| §93 R1 writable runtime paths | `7c6f904` | Role-specific state/log homes work with read-only container roots; loss memory and auto-block control persist | Preserve mounted state volumes; correct mount ownership rather than disabling container controls |
| §93 R2 state-write diagnostics and TLS | `fbea790` | Persistence failures become visible; PostgreSQL identity is checked before authentication | Correct permissions and configure trusted CA/pin; retain fail-closed TLS |
| §93 R3 operator credential startup scope | `80813aa` | Invalid admin credentials stop API startup without stopping the live runner's independent entrypoint | Correct operator credentials and restart API |
| §93 R4 public identifiers and redaction | `f9f0469` | Public transaction identifiers remain visible; secret-bearing diagnostics are masked | Inspect synthetic redaction fixtures; never disable secret masking |
| §93 R5 ticker normalization | `a9015da` | Ticker identities normalize consistently; ambiguous short keys are excluded | Inspect normalized mappings; use explicit identity matches |
| §93 R6 body/spend limits | `db879ce` | Body cap defaults to 1 MiB; API-side LLM calls use their own spend counter; projected cap demand is visible | Tune documented body limit only if needed; preserve independent LLM accounting |
| §93 R7 authentication/test-host consistency | `b359e8f` | Concealed routes and host trust match the documented authentication matrix | Correct trusted-host configuration; preserve protected-route requirements |
| §93 R8 evidence migration | `b58de47`, `0a9b40a`, `028d4e0` | Historical audit/evidence documents and synthetic scanner fixtures updated | Read evidence dates and scope; historical CI/container reports are not current verification |
| Sell retry authorization | `04e1feb` | Durable signed-attempt marker blocks new exits while prior outcome is unresolved; native terminal parsing prevents unsafe replay | Resolve the known signature; §94 supplies positive-evidence release and operator tooling |
| Approval binding | `04e1feb` | Approval must match canonical side, mint, fraction/size and operation; legacy/mismatched approvals refuse | Create a fresh matching approval |
| Buy decimals refusal | `04e1feb` | Missing chain decimals refuse; metadata cannot authorize sizing | Restore chain decimals access; do not insert a default |
| Simulation consensus | `04e1feb` | Every monitored account compared, including additional wallet accounts; opaque pool bytes were over-constrained | §94 narrows market-state equality while retaining wallet/control protection |
| Ledger and commit validation | `04e1feb` | Invalid history refuses to load or overwrite; close signatures identify reductions idempotently | Run §94 read-only validation on a copied state directory before upgrade; retain rollback copy |
| Process locking | `04e1feb` | Read/modify/write accounting is serialized across processes | Fix supported filesystem/lock access; never hand-edit live JSON |
| HTTP response bounds | `04e1feb` | External bodies capped at 8 MiB and 60 seconds; redirects not followed; identity encoding requested | Correct configured provider endpoints; §94 adds explicit policy errors and redirect diagnostics |
| Restore dispatch | `04e1feb` | Restored authority is validated and dispatched by state type | Validate scratch copy; preserve original backup for rollback |
| Launcher and CI changes | `04e1feb` | Startup/child lifecycle checks and verification coverage expanded | Inspect exact launcher exit behavior; do not bypass red gates |
| Isolated staged verification/advisories | `f4868ac` | Pre-push checks can run as isolated stages, with advisory scope separately controlled | Run every required stage; unavailable advisory results remain unverified |
| Browser CI image/dependency bounds | `ea0a899` | Browser job uses Ubuntu 24.04 and bounded dependency setup | Inspect visible CI output; no current CI status is asserted here |
| §94 S1 expired-attempt release | `5177a77` | All-endpoint absence, finalized expiry, signing age and delayed second observation release a dead sell; legacy attempts need operator review | Inspect with resolve_pending_sell.py list; do not erase pending markers or weaken proof requirements |
| §94 S2 already-closed settlement | `17a26ca` | Confirmed sell settles after independent closure, returns blocked, and does not book proceeds twice; repairable accounting failures remain pending | Repair accounting and retry recovery; preserve signature idempotency |
| §94 S3 consumed-field consensus | `a77a8af` | Pool state may differ across slots; structural/token controls and wallet tolerances remain; F verdict disagreement refuses | Inspect structured refusal logs and process-local counters; measure actual refusal rate on VM |
| §94 S4 mutation evidence | `abfe549` | Equivalent mutants target the actual enforcement ranges; dated corrections supersede overstated claims | Use generated no-flags mutation log and current commands; do not infer advisory status from tests |
| §94 S5 state preflight | `2db2070` | Read-only state validator reports invalid rows before the live runner encounters them; deploy preflight fails on invalid copied state | Keep pre-upgrade state copy and validate before recreating containers |
| §94 S6 HTTP/test consistency | `4edcb7f` | Explicit redirect following or incompatible explicit Accept-Encoding raises; returned redirects log on bounded cadence; tests locate repository files independently of CWD | Use final provider URLs and identity encoding; no production caller explicitly requested redirects in the author audit |
| §94 S7 operational evidence | §94 S7 | Security/deployment/handoff records distinguish local proof from unverified VM/native behavior | Review command outputs and remaining validation items before deployment |
| §95 S1 private live counters | §95 S1 | Health returns only status; live writes best-effort `consensus_counters.json`; `scripts/consensus_status.py` reads counts and marks stale/missing data | Read the snapshot privately and cross-check logs; counters reset on live-process restart |
| §95 S2 payer identity | §95 S2 | A mismatch between intent wallet and payer refuses before RPC; check F consensus receives the explicit payer | Correct the caller's payer/intent pairing; missing payer with intent/pre-state fails closed |
