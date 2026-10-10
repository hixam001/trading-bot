-- ============================================================================
-- migrations/supabase/006_rls_policies.sql
-- Enforce Row-Level Security (RLS) and user-scoped data access policies.
--
-- Contract:
--   1. RLS is explicitly ENABLED and FORCED on every table in the schema.
--   2. User-scoped tables gain a `user_id` column linked to `auth.users(id)`
--      with default `auth.uid()`.
--   3. Strict granular policies ensure authenticated users can ONLY read and
--      modify rows where `user_id = auth.uid()`.
--   4. No `USING (true)` or allow-all policies are used.
--   5. System/telemetry/singleton tables maintain fail-closed default-deny
--      for non-service-role clients.
-- ============================================================================

-- 0. Migration tracking
INSERT INTO schema_migrations (version) VALUES ('006_rls_policies')
ON CONFLICT (version) DO NOTHING;

-- 1. Ensure RLS is enabled on ALL 14 public tables
DO $$
DECLARE
    tbl text;
BEGIN
    FOR tbl IN
        SELECT unnest(ARRAY[
            'schema_migrations',
            'portfolio_state',
            'market_regime',
            'provider_call_counters',
            'llm_call_usage',
            'funnel_snapshots',
            'trades',
            'theses',
            'feed_events',
            'decision_commits',
            'events',
            'memories',
            'daily_stats',
            'kb_documents'
        ])
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', tbl);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY;', tbl);
    END LOOP;
END $$;

-- 2. Add user_id column to user-scoped tables
ALTER TABLE trades ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE DEFAULT auth.uid();
CREATE INDEX IF NOT EXISTS idx_trades_user_id ON trades(user_id);

ALTER TABLE theses ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE DEFAULT auth.uid();
CREATE INDEX IF NOT EXISTS idx_theses_user_id ON theses(user_id);

ALTER TABLE feed_events ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE DEFAULT auth.uid();
CREATE INDEX IF NOT EXISTS idx_feed_events_user_id ON feed_events(user_id);

ALTER TABLE decision_commits ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE DEFAULT auth.uid();
CREATE INDEX IF NOT EXISTS idx_decision_commits_user_id ON decision_commits(user_id);

ALTER TABLE events ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE DEFAULT auth.uid();
CREATE INDEX IF NOT EXISTS idx_events_user_id ON events(user_id);

ALTER TABLE memories ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE DEFAULT auth.uid();
CREATE INDEX IF NOT EXISTS idx_memories_user_id ON memories(user_id);

ALTER TABLE daily_stats ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE DEFAULT auth.uid();
CREATE INDEX IF NOT EXISTS idx_daily_stats_user_id ON daily_stats(user_id);

ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE DEFAULT auth.uid();
CREATE INDEX IF NOT EXISTS idx_kb_documents_user_id ON kb_documents(user_id);

-- 3. Granular User-Level Policies (auth.uid() = user_id)
-- Clean up existing user policies if re-run
DO $$
DECLARE
    tbl text;
    pol text;
BEGIN
    FOR tbl IN
        SELECT unnest(ARRAY[
            'trades',
            'theses',
            'feed_events',
            'decision_commits',
            'events',
            'memories',
            'daily_stats',
            'kb_documents'
        ])
    LOOP
        FOR pol IN
            SELECT unnest(ARRAY['select_own', 'insert_own', 'update_own', 'delete_own'])
        LOOP
            EXECUTE format('DROP POLICY IF EXISTS %I ON %I;', tbl || '_' || pol, tbl);
        END LOOP;
    END LOOP;
END $$;

-- Table: trades
CREATE POLICY trades_select_own ON trades
    FOR SELECT TO authenticated
    USING (auth.uid() = user_id);

CREATE POLICY trades_insert_own ON trades
    FOR INSERT TO authenticated
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY trades_update_own ON trades
    FOR UPDATE TO authenticated
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY trades_delete_own ON trades
    FOR DELETE TO authenticated
    USING (auth.uid() = user_id);

-- Table: theses
CREATE POLICY theses_select_own ON theses
    FOR SELECT TO authenticated
    USING (auth.uid() = user_id);

CREATE POLICY theses_insert_own ON theses
    FOR INSERT TO authenticated
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY theses_update_own ON theses
    FOR UPDATE TO authenticated
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY theses_delete_own ON theses
    FOR DELETE TO authenticated
    USING (auth.uid() = user_id);

-- Table: feed_events
CREATE POLICY feed_events_select_own ON feed_events
    FOR SELECT TO authenticated
    USING (auth.uid() = user_id);

CREATE POLICY feed_events_insert_own ON feed_events
    FOR INSERT TO authenticated
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY feed_events_update_own ON feed_events
    FOR UPDATE TO authenticated
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY feed_events_delete_own ON feed_events
    FOR DELETE TO authenticated
    USING (auth.uid() = user_id);

-- Table: decision_commits
CREATE POLICY decision_commits_select_own ON decision_commits
    FOR SELECT TO authenticated
    USING (auth.uid() = user_id);

CREATE POLICY decision_commits_insert_own ON decision_commits
    FOR INSERT TO authenticated
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY decision_commits_update_own ON decision_commits
    FOR UPDATE TO authenticated
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY decision_commits_delete_own ON decision_commits
    FOR DELETE TO authenticated
    USING (auth.uid() = user_id);

-- Table: events
CREATE POLICY events_select_own ON events
    FOR SELECT TO authenticated
    USING (auth.uid() = user_id);

CREATE POLICY events_insert_own ON events
    FOR INSERT TO authenticated
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY events_update_own ON events
    FOR UPDATE TO authenticated
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY events_delete_own ON events
    FOR DELETE TO authenticated
    USING (auth.uid() = user_id);

-- Table: memories
CREATE POLICY memories_select_own ON memories
    FOR SELECT TO authenticated
    USING (auth.uid() = user_id);

CREATE POLICY memories_insert_own ON memories
    FOR INSERT TO authenticated
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY memories_update_own ON memories
    FOR UPDATE TO authenticated
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY memories_delete_own ON memories
    FOR DELETE TO authenticated
    USING (auth.uid() = user_id);

-- Table: daily_stats
CREATE POLICY daily_stats_select_own ON daily_stats
    FOR SELECT TO authenticated
    USING (auth.uid() = user_id);

CREATE POLICY daily_stats_insert_own ON daily_stats
    FOR INSERT TO authenticated
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY daily_stats_update_own ON daily_stats
    FOR UPDATE TO authenticated
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY daily_stats_delete_own ON daily_stats
    FOR DELETE TO authenticated
    USING (auth.uid() = user_id);

-- Table: kb_documents
CREATE POLICY kb_documents_select_own ON kb_documents
    FOR SELECT TO authenticated
    USING (auth.uid() = user_id);

CREATE POLICY kb_documents_insert_own ON kb_documents
    FOR INSERT TO authenticated
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY kb_documents_update_own ON kb_documents
    FOR UPDATE TO authenticated
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY kb_documents_delete_own ON kb_documents
    FOR DELETE TO authenticated
    USING (auth.uid() = user_id);

