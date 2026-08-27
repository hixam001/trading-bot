-- 004_fill_venue.sql — A3 fill-venue attribution column.
--
-- decision_commits gains the executing-venue label (which program ran the bound
-- fill: pump.fun / raydium / jupiter router / …), read off the confirmed tx so
-- it is verifiable against the same signature in a block explorer. Nullable:
-- observability only, null until a live fill is bound and attributed. Idempotent
-- (IF NOT EXISTS) — safe to re-run; db_pg.init_db() also self-heals this column
-- via _SCHEMA_SYNC_SQL for books created before this file existed.

ALTER TABLE decision_commits ADD COLUMN IF NOT EXISTS venue TEXT;

INSERT INTO schema_migrations (version) VALUES ('004_fill_venue')
ON CONFLICT (version) DO NOTHING;