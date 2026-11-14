# Documentation index

The current software references below were reviewed against repository source
on **2026-11-14**. They complement the numbered design, deployment and audit
guides. Start here for the supported application contract, then follow the
source/test links when implementing changes.

## Current software references

| Document | Read it for |
|---|---|
| [REQUIREMENTS.md](REQUIREMENTS.md) | Product scope, actors, acceptance requirements and test traceability. |
| [APPFLOW.md](APPFLOW.md) | Process boundaries, decision/fill/exit sequences and dashboard journeys. |
| [API_CONTRACT.md](API_CONTRACT.md) | Endpoints, access rules, requests/responses, pagination, errors and WebSocket behavior. |
| [DATA_MODEL.md](DATA_MODEL.md) | SQL entities, logical relationships, live state, accounting authority and migrations. |
| [CONFIGURATION.md](CONFIGURATION.md) | Configuration ownership, environment groups, paths, roles and frontend origins. |
| [DEVELOPMENT.md](DEVELOPMENT.md) | API-only local setup, source map, change workflow and documentation maintenance. |
| [TEST_PLAN.md](TEST_PLAN.md) | Coverage map, hermetic tests, frontend/browser checks, CI and release evidence. |
| [OPERATIONS.md](OPERATIONS.md) | Routine diagnosis, pending signatures, upgrade/rollback and recovery. |

Suggested reading: requirements → application flow → API/data contracts →
development/test plan. Operators can start with configuration → deployment →
operations → security.

## Design and product context

| Document | Purpose |
|---|---|
| [00_BLUEPRINT.md](00_BLUEPRINT.md) | Original system blueprint and design intent. |
| [01_ARCHITECTURE.md](01_ARCHITECTURE.md) | Detailed rule, provider, model and execution architecture. |
| [02_FEATURE_LIST.md](02_FEATURE_LIST.md) | Feature inventory. |
| [03_GANTT_CHART.md](03_GANTT_CHART.md) | Historical implementation schedule. |
| [05_VERIFICATION_APPENDIX.md](05_VERIFICATION_APPENDIX.md) | Recorded verification background. |
| [06_REFERENCE_COMPARISON.md](06_REFERENCE_COMPARISON.md) | Reference-system comparison. |
| [07_PROJECT_REPORT.md](07_PROJECT_REPORT.md) | Project report and historical results. |
| [Frontend design](../frontend/DESIGN.md) | Dashboard design and interaction states. |

## Deployment, integrations and security

| Document | Purpose |
|---|---|
| [11_DEPLOYMENT.md](11_DEPLOYMENT.md) | Deployment roles, volumes, shutdown, preflight and alerts. |
| [13_ORACLE_DEPLOY_GUIDE.md](13_ORACLE_DEPLOY_GUIDE.md) | Oracle VM setup and deployment. |
| [14_SIGNING_GUARD.md](14_SIGNING_GUARD.md) | Transaction inspection/simulation design and diagnostics. |
| [15_BACKUP_RESTORE.md](15_BACKUP_RESTORE.md) | Backup/restore runbooks and format evolution. |
| [18_SECURITY.md](18_SECURITY.md) | Canonical security route matrix, controls, incidents, rotation and residual risks. |
| [19_ROOTFS_AUDIT.md](19_ROOTFS_AUDIT.md) | Read-only-root runtime write inventory and evidence. |
| [08_LLM_API_MIGRATION_AND_FEEDBACK_PLAN.md](08_LLM_API_MIGRATION_AND_FEEDBACK_PLAN.md) | LLM migration and feedback context. |
| [12_FREE_SEARCH_STACK.md](12_FREE_SEARCH_STACK.md) | Search transport chain. |
| [FOMO_INTEGRATION.md](FOMO_INTEGRATION.md) | Crowd-feed integration. |
| [16_JUPITER_API_MIGRATION.md](16_JUPITER_API_MIGRATION.md) | Jupiter endpoint/client migration. |

## Change history and evidence

Read the latest relevant [handoff](../handoff.md) sections together with the
[behavior-change map](20_BEHAVIOUR_CHANGES_SINCE_3FEA445.md),
[mutation evidence](17_MUTATION_LOG.md),
[security remediation](SECURITY_REMEDIATION.md),
[signing-policy remediation](SIGNING_POLICY_REMEDIATION.md), and
[provider-number remediation](PROVIDER_NUMBER_REMEDIATION.md).
[memory-bank](../memory-bank/) contains historical working context.

Source and executable tests determine implemented behavior. Older numbered
guides/handoff entries can describe retired paper flows, earlier migration
ranges, backup formats or historical test counts; use the current references
and later dated corrections for present behavior. In particular, this checkout
is single-book and source-armed, Supabase setup includes `001`–`007`, and new
backups use the v2 format. A documented local result is not a current CI,
production or funded-wallet verification.

When a contract changes, update its reference, affected tests and this index
in the same change. Preserve dates and limitations on historical evidence.
