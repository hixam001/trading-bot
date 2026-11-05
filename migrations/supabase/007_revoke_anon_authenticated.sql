-- ============================================================================
-- migrations/supabase/007_revoke_anon_authenticated.sql
--
-- §92 S4: the bot is a single-operator system. It connects with the
-- privileged service role, which bypasses RLS. PostgREST nevertheless
-- exposes the `anon` and `authenticated` roles to anyone holding the
-- project's public key. Those roles must have NO table or sequence
-- privileges, regardless of the user-scoped policies left by 006.
--
-- Defence in depth:
--   1. Explicitly enable RLS on every table that exists in `public`.
--   2. Revoke all current and future table/sequence privileges from the
--      PostgREST roles.
--   3. Fail the migration if a public table still has RLS disabled.
--
-- This migration is idempotent.
-- ============================================================================

INSERT INTO schema_migrations (version) VALUES ('007_revoke_anon_authenticated')
    ON CONFLICT (version) DO NOTHING;

-- 1. Every table in the public schema must have RLS enabled, including any
--    table created outside these migrations.
DO $$
DECLARE
    tbl text;
BEGIN
    FOR tbl IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;', tbl);
    END LOOP;
END $$;

-- 2. PostgREST roles get nothing. The service role is unaffected.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON SCHEMA public FROM anon, authenticated;

-- Cover objects created after this migration by the migration/owner role.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON FUNCTIONS FROM anon, authenticated;

-- 3. Fail closed if any public table is still exposed by a future change.
DO $$
DECLARE
    tbl text;
BEGIN
    FOR tbl IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind = 'r'
          AND c.relrowsecurity = false
    LOOP
        RAISE EXCEPTION
            'S4: public table % has RLS disabled; refusing to finish the '
            'PostgREST lock-down', tbl;
    END LOOP;
END $$;

-- Verify with:
--   SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname='public';
--   SELECT grantee, table_name, privilege_type
--     FROM information_schema.role_table_grants
--    WHERE grantee IN ('anon','authenticated');
