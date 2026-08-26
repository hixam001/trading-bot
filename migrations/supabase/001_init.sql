-- ============================================================================
-- migrations/supabase/001_init.sql
-- Full Postgres schema for trading-bot on Supabase.
-- Run ONCE in the Supabase SQL Editor (Dashboard -> SQL -> New query ->
-- paste -> Run). Idempotent: safe to re-run.
--
-- Translation notes vs the SQLite schema (backend/api/db.py):
--   INTEGER AUTOINCREMENT  -> BIGINT GENERATED ALWAYS AS IDENTITY
--   TEXT ISO timestamps    -> TIMESTAMPTZ (app sends ISO strings; they cast)
--   JSON-in-TEXT columns   -> JSONB (indexable, validated)
--   REAL                   -> DOUBLE PRECISION
--
-- App access: the backend connects with the SERVICE ROLE key / direct
-- pooler URL, which bypasses RLS. RLS is still enabled + locked below so
-- a leaked anon key can read nothing.
-- ============================================================================

-- 0. Migration bookkeeping ---------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version      TEXT PRIMARY KEY,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations (version) VALUES ('001_init')
ON CONFLICT (version) DO NOTHING;

-- 1. feed_events — every GateDecision, pass or fail ---------------------------
CREATE TABLE IF NOT EXISTS feed_events (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                      TIMESTAMPTZ NOT NULL,
    symbol                  TEXT        NOT NULL,
    mint_address            TEXT        NOT NULL,
    candidate_snapshot      JSONB       NOT NULL,
    verdict                 TEXT        NOT NULL CHECK (verdict IN ('pass','fail')),
    thesis                  TEXT,
    rule_breakdown          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    failed_rule_ids         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    regime_ok               BOOLEAN     NOT NULL DEFAULT FALSE,
    grounding_flags         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    narration_source        TEXT        NOT NULL DEFAULT '',
    led_to_trade_id         TEXT
);
CREATE INDEX IF NOT EXISTS idx_feed_events_ts ON feed_events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_feed_events_id ON feed_events (id DESC);
CREATE INDEX IF NOT EXISTS idx_feed_events_mint ON feed_events (mint_address);

-- 2. trades — paper positions -------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
    trade_id                TEXT PRIMARY KEY,
    symbol                  TEXT        NOT NULL,
    mint_address            TEXT        NOT NULL,
    opened_at               TIMESTAMPTZ NOT NULL,
    entry_price_usd         DOUBLE PRECISION NOT NULL,
    position_size_usd       DOUBLE PRECISION NOT NULL,
    quantity                DOUBLE PRECISION NOT NULL,
    candidate_snapshot      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    thesis                  TEXT        NOT NULL DEFAULT '',
    closed_at               TIMESTAMPTZ,
    exit_price_usd          DOUBLE PRECISION,
    exit_reason             TEXT,
    realized_pnl_usd        DOUBLE PRECISION,
    realized_pnl_pct        DOUBLE PRECISION,
    is_open                 BOOLEAN     NOT NULL DEFAULT TRUE,
    high_water_usd          DOUBLE PRECISION,
    tranches_taken          INTEGER     NOT NULL DEFAULT 0,
    reflection_text         TEXT
);

-- HARD backstop for open-position idempotency: at most one OPEN position
-- per mint, enforced by Postgres itself. Requires: CREATE EXTENSION btree_gist;
-- (run that once if this statement errors). Portable alternative below.
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS btree_gist;
    ALTER TABLE trades ADD CONSTRAINT trades_one_open_per_mint
        EXCLUDE USING gist (mint_address WITH =) WHERE (is_open);
EXCEPTION WHEN others THEN
    -- Fallback for hosts without btree_gist:
    EXECUTE 'CREATE UNIQUE INDEX trades_one_open_per_mint
             ON trades (mint_address) WHERE is_open';
END $$;

CREATE INDEX IF NOT EXISTS idx_trades_is_open   ON trades (is_open);
CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades (closed_at);

-- 3. market_regime — one row per tick -----------------------------------------
CREATE TABLE IF NOT EXISTS market_regime (
    id                       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    computed_at              TIMESTAMPTZ NOT NULL,
    candidate_count          INTEGER     NOT NULL,
    pct_candidates_green_1h  DOUBLE PRECISION NOT NULL,
    median_volume_1h_usd     DOUBLE PRECISION NOT NULL,
    avg_buy_sell_ratio       DOUBLE PRECISION NOT NULL,
    regime_ok                BOOLEAN     NOT NULL,
    regime_detail            TEXT        NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_regime_computed_at ON market_regime (computed_at DESC);

-- 4. provider_call_counters — daily rate-limit ledger --------------------------
CREATE TABLE IF NOT EXISTS provider_call_counters (
    provider                 TEXT    NOT NULL,
    day                      TEXT    NOT NULL,
    call_count               INTEGER NOT NULL DEFAULT 0,
    error_count              INTEGER NOT NULL DEFAULT 0,
    rate_limit_429_count     INTEGER NOT NULL DEFAULT 0,
    last_call_at             TIMESTAMPTZ,
    PRIMARY KEY (provider, day)
);

-- 5. kb_documents — knowledge base --------------------------------------------
CREATE TABLE IF NOT EXISTS kb_documents (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filename     TEXT        NOT NULL UNIQUE,
    content      TEXT        NOT NULL,
    digest       TEXT        NOT NULL DEFAULT '',
    ingested_at  TIMESTAMPTZ NOT NULL
);

-- 6. daily_stats ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_stats (
    date              TEXT PRIMARY KEY,
    open_positions    INTEGER NOT NULL DEFAULT 0,
    closed_trades     INTEGER NOT NULL DEFAULT 0,
    stats_json        JSONB   NOT NULL DEFAULT '{}'::jsonb
);

-- 7. decision_commits — omo 'seal' parity audit trail --------------------------
-- sha256(nonce|canonical payload) written BEFORE the bot acts on a decision;
-- plaintext payload stored alongside so anyone can recompute the hash.
CREATE TABLE IF NOT EXISTS decision_commits (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL,
    tick_ts         TIMESTAMPTZ NOT NULL,
    symbol          TEXT        NOT NULL,
    mint_address    TEXT        NOT NULL,
    verdict         TEXT        NOT NULL,            -- think verdict: buy | pass
    entry_allowed   BOOLEAN     NOT NULL,            -- both layers agreed
    nonce           TEXT        NOT NULL,
    payload_json    TEXT        NOT NULL,            -- canonical reveal payload
    payload_hash    TEXT        NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_decision_commits_created
    ON decision_commits (created_at DESC);

-- 8. OMO-R5 durable event stream and weighted memories ----------------------
CREATE TABLE IF NOT EXISTS events (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('thought','did','refused','read','trade')),
    symbol       TEXT NOT NULL DEFAULT '',
    mint_address TEXT NOT NULL DEFAULT '',
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind);

CREATE TABLE IF NOT EXISTS memories (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic      TEXT NOT NULL,
    note       TEXT NOT NULL,
    weight     DOUBLE PRECISION NOT NULL DEFAULT 1.0 CHECK (weight > 0),
    hits       INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_topic_weight ON memories (topic, weight DESC);

-- 9. portfolio_state — singleton cash row (id always 1) -------------------------
CREATE TABLE IF NOT EXISTS portfolio_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    cash_usd     DOUBLE PRECISION NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO portfolio_state (id, cash_usd)
VALUES (1, 1000.0)
ON CONFLICT (id) DO NOTHING;

-- 10. theses — durable write-up for each position (OMO-R3) -----------------
CREATE TABLE IF NOT EXISTS theses (
    trade_id          TEXT PRIMARY KEY,
    mint_address      TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    author            TEXT NOT NULL,
    thesis            TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL,
    closed_at         TIMESTAMPTZ,
    realized_pnl_usd  DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_theses_mint ON theses (mint_address);

-- 11. Lock down RLS --------------------------------------------------------------
-- Backend uses service role (bypasses RLS). Anon/authenticated keys get NOTHING.
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'feed_events','trades','market_regime','provider_call_counters',
        'kb_documents','daily_stats','decision_commits','events','memories',
        'portfolio_state','theses',
        'schema_migrations'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    END LOOP;
END $$;

-- Done. Verify with:
--   SELECT tablename FROM pg_tables WHERE schemaname='public';
--   SELECT version FROM schema_migrations;