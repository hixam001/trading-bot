-- 005_funnel_snapshots.sql: read-only dashboard trend history.

-- §64: persisted funnel snapshots — the refusal-rate trend source.
-- The live cycle writes one throttled row per cycle; /api/funnel/snapshots
-- reads them. The rate stays NULL whenever gate_passed = 0 (never a zero).

CREATE TABLE IF NOT EXISTS funnel_snapshots (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                      TIMESTAMPTZ NOT NULL,
    candidates_seen         INTEGER NOT NULL,
    gate_refused            INTEGER NOT NULL,
    model_refused           INTEGER NOT NULL,
    gate_passed             INTEGER NOT NULL,
    model_approved          INTEGER NOT NULL,
    filled                  INTEGER NOT NULL,
    model_refusal_rate      DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_funnel_snapshots_ts ON funnel_snapshots(ts);

ALTER TABLE funnel_snapshots ENABLE ROW LEVEL SECURITY;

INSERT INTO schema_migrations (version) VALUES ('005_funnel_snapshots')
    ON CONFLICT (version) DO NOTHING;
