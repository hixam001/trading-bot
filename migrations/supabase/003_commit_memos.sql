-- 003_commit_memos.sql — REF-R11 on-chain precommit memo columns.
--
-- decision_commits gains the confirmed on-chain memo (signature + slot) that
-- preceded the fill. Nullable: paper commits never publish a memo. Idempotent
-- (IF NOT EXISTS) — safe to re-run; db_pg.init_db() also self-heals these
-- columns via _SCHEMA_SYNC_SQL for books created before this file existed.

ALTER TABLE decision_commits ADD COLUMN IF NOT EXISTS memo_signature TEXT;
ALTER TABLE decision_commits ADD COLUMN IF NOT EXISTS memo_slot BIGINT;

INSERT INTO schema_migrations (version) VALUES ('003_commit_memos')
ON CONFLICT (version) DO NOTHING;