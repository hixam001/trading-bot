"""
backend/tests/test_audit_79_g.py — tests for gate funnel diagnostic report (§79 Phase G).

Validates:
1. Exact calculations of daily funnel counts (seen, gate-passed, think-passed, entered).
2. Per-rule fail rates and sole-blocker counts on hand-seeded rows.
3. Counterfactual analysis ("if rule X were removed, N candidates would have passed").
4. Distribution (min, median, p90) of failing metrics for top blocking rules.
5. Thinker / LLM refusal rate among gate-passers and verdict mix.
6. Date filtering via `since`.
7. CLI interface (human-readable text and --json).
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
from gate_funnel_report import generate_funnel_report, format_text_report


_SCHEMA = """
CREATE TABLE feed_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                      TEXT    NOT NULL,
    symbol                  TEXT    NOT NULL,
    mint_address            TEXT    NOT NULL,
    candidate_snapshot      TEXT    NOT NULL,
    verdict                 TEXT    NOT NULL,
    thesis                  TEXT,
    rule_breakdown          TEXT    NOT NULL DEFAULT '[]',
    failed_rule_ids         TEXT    NOT NULL DEFAULT '[]',
    regime_ok               INTEGER NOT NULL DEFAULT 1,
    grounding_flags         TEXT    NOT NULL DEFAULT '[]',
    narration_source        TEXT    NOT NULL DEFAULT '',
    led_to_trade_id         TEXT,
    model_version           TEXT,
    prompt_version          TEXT,
    risk_flags              TEXT    NOT NULL DEFAULT '[]'
);

CREATE TABLE trades (
    trade_id                TEXT    PRIMARY KEY,
    symbol                  TEXT    NOT NULL,
    mint_address            TEXT    NOT NULL,
    opened_at               TEXT    NOT NULL,
    entry_price_usd         REAL    NOT NULL,
    position_size_usd       REAL    NOT NULL,
    quantity                REAL    NOT NULL,
    candidate_snapshot      TEXT    NOT NULL DEFAULT '{}',
    thesis                  TEXT    NOT NULL DEFAULT '',
    closed_at               TEXT,
    exit_price_usd          REAL,
    exit_reason             TEXT,
    realized_pnl_usd        REAL,
    realized_pnl_pct        REAL,
    is_open                 INTEGER NOT NULL DEFAULT 1
);
"""


@pytest.fixture
def seeded_db(tmp_path):
    """Seed a SQLite database with 6 hand-crafted candidate rows across 2 days.
    
    Candidates:
    C1 (2026-09-01): passes gate (failed=[]), verdict='pass', led_to_trade_id='t1'
    C2 (2026-09-01): passes gate (failed=[]), verdict='fail' (model refused)
    C3 (2026-09-01): failed only rule_A (failed=['rule_A']), verdict='fail'
    C4 (2026-09-02): failed only rule_A (failed=['rule_A']), verdict='fail'
    C5 (2026-09-02): failed only rule_B (failed=['rule_B']), verdict='fail'
    C6 (2026-09-02): failed both rule_A and rule_B (failed=['rule_A', 'rule_B']), verdict='fail'

    Hand-calculated properties:
    - Total candidates: 6
    - Baseline gate passers: 2 (C1, C2)
    - Baseline think passers: 1 (C1)
    - Baseline entered: 1 (C1)
    
    Day 2026-09-01: seen=3, gate_passed=2, think_passed=1, entered=1
    Day 2026-09-02: seen=3, gate_passed=0, think_passed=0, entered=0

    rule_A:
      - fail_count: 3 (C3, C4, C6)
      - fail_rate: 3/6 = 0.50
      - sole_blocker_count: 2 (C3, C4)
      - counterfactual without rule_A: 2 (baseline) + 2 (C3, C4) = 4
      - failing metric values: [50.0, 60.0, 70.0] -> min=50.0, med=60.0, p90=68.0

    rule_B:
      - fail_count: 2 (C5, C6)
      - fail_rate: 2/6 = 0.3333
      - sole_blocker_count: 1 (C5)
      - counterfactual without rule_B: 2 (baseline) + 1 (C5) = 3
      - failing metric values: [10.0, 20.0] -> min=10.0, med=15.0, p90=19.0

    LLM funnel:
      - gate_passers: 2
      - model_approved: 1 (C1)
      - model_refused: 1 (C2)
      - llm_refusal_rate: 0.50 (50%)
    """
    db_file = tmp_path / "test_funnel.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(_SCHEMA)

    rows = [
        # C1: pass gate, pass think, entered trade
        (
            "2026-09-01T10:00:00+00:00", "SYM1", "mint1", "{}", "pass", "thesis1",
            json.dumps([
                {"rule_id": "rule_A", "passed": True, "value": 100.0, "detail": "val 100 vs floor 80"},
                {"rule_id": "rule_B", "passed": True, "value": 200.0, "detail": "val 200 vs min 50"},
            ]),
            json.dumps([]),
            "t1",
        ),
        # C2: pass gate, model declined
        (
            "2026-09-01T11:00:00+00:00", "SYM2", "mint2", "{}", "fail", "thesis2",
            json.dumps([
                {"rule_id": "rule_A", "passed": True, "value": 110.0, "detail": "val 110 vs floor 80"},
                {"rule_id": "rule_B", "passed": True, "value": 210.0, "detail": "val 210 vs min 50"},
            ]),
            json.dumps([]),
            None,
        ),
        # C3: sole blocker rule_A
        (
            "2026-09-01T12:00:00+00:00", "SYM3", "mint3", "{}", "fail", "thesis3",
            json.dumps([
                {"rule_id": "rule_A", "passed": False, "value": 50.0, "detail": "val 50 vs floor 80"},
                {"rule_id": "rule_B", "passed": True, "value": 220.0, "detail": "val 220 vs min 50"},
            ]),
            json.dumps(["rule_A"]),
            None,
        ),
        # C4: sole blocker rule_A
        (
            "2026-09-02T08:00:00+00:00", "SYM4", "mint4", "{}", "fail", "thesis4",
            json.dumps([
                {"rule_id": "rule_A", "passed": False, "value": 60.0, "detail": "val 60 vs floor 80"},
                {"rule_id": "rule_B", "passed": True, "value": 230.0, "detail": "val 230 vs min 50"},
            ]),
            json.dumps(["rule_A"]),
            None,
        ),
        # C5: sole blocker rule_B
        (
            "2026-09-02T09:00:00+00:00", "SYM5", "mint5", "{}", "fail", "thesis5",
            json.dumps([
                {"rule_id": "rule_A", "passed": True, "value": 120.0, "detail": "val 120 vs floor 80"},
                {"rule_id": "rule_B", "passed": False, "value": 10.0, "detail": "val 10 vs min 50"},
            ]),
            json.dumps(["rule_B"]),
            None,
        ),
        # C6: failed both rule_A and rule_B
        (
            "2026-09-02T10:00:00+00:00", "SYM6", "mint6", "{}", "fail", "thesis6",
            json.dumps([
                {"rule_id": "rule_A", "passed": False, "value": 70.0, "detail": "val 70 vs floor 80"},
                {"rule_id": "rule_B", "passed": False, "value": 20.0, "detail": "val 20 vs min 50"},
            ]),
            json.dumps(["rule_A", "rule_B"]),
            None,
        ),
    ]

    for row in rows:
        conn.execute(
            """
            INSERT INTO feed_events (
                ts, symbol, mint_address, candidate_snapshot, verdict, thesis,
                rule_breakdown, failed_rule_ids, led_to_trade_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )

    # Insert corresponding trade for C1
    conn.execute(
        """
        INSERT INTO trades (
            trade_id, symbol, mint_address, opened_at, entry_price_usd, position_size_usd, quantity
        ) VALUES ('t1', 'SYM1', 'mint1', '2026-09-01T10:00:05+00:00', 1.0, 100.0, 100.0)
        """
    )
    conn.commit()
    conn.close()
    return db_file


def test_funnel_report_calculations_on_seeded_db(seeded_db):
    """Verify exact hand-calculated metrics from seeded database."""
    report = generate_funnel_report(db_path=seeded_db)

    assert report["total_candidates"] == 6
    summary = report["baseline_summary"]
    assert summary["candidates_seen"] == 6
    assert summary["gate_passed"] == 2
    assert summary["think_passed"] == 1
    assert summary["entered"] == 1
    assert summary["gate_pass_rate"] == pytest.approx(2 / 6, rel=1e-3)

    # 1. Daily Funnel
    assert len(report["daily_funnel"]) == 2
    d1 = report["daily_funnel"][0]
    assert d1["date"] == "2026-09-01"
    assert d1["candidates_seen"] == 3
    assert d1["gate_passed"] == 2
    assert d1["think_passed"] == 1
    assert d1["entered"] == 1
    assert d1["gate_pass_rate"] == pytest.approx(2 / 3, rel=1e-3)

    d2 = report["daily_funnel"][1]
    assert d2["date"] == "2026-09-02"
    assert d2["candidates_seen"] == 3
    assert d2["gate_passed"] == 0
    assert d2["think_passed"] == 0
    assert d2["entered"] == 0
    assert d2["gate_pass_rate"] == 0.0

    # 2. Per-rule failure rates & sole blockers
    rule_stats = {r["rule_id"]: r for r in report["per_rule_stats"]}
    assert "rule_A" in rule_stats
    assert rule_stats["rule_A"]["fail_count"] == 3
    assert rule_stats["rule_A"]["fail_rate"] == pytest.approx(0.50, rel=1e-3)
    assert rule_stats["rule_A"]["sole_blocker_count"] == 2
    assert rule_stats["rule_A"]["sole_blocker_rate"] == pytest.approx(2 / 6, rel=1e-3)

    assert "rule_B" in rule_stats
    assert rule_stats["rule_B"]["fail_count"] == 2
    assert rule_stats["rule_B"]["fail_rate"] == pytest.approx(2 / 6, rel=1e-3)
    assert rule_stats["rule_B"]["sole_blocker_count"] == 1
    assert rule_stats["rule_B"]["sole_blocker_rate"] == pytest.approx(1 / 6, rel=1e-3)

    # 3. Counterfactual analysis
    cf = report["counterfactuals"]
    assert cf["rule_A"]["total_passed_without_rule"] == 4
    assert cf["rule_A"]["incremental_gain"] == 2
    assert cf["rule_A"]["pass_rate_without_rule"] == pytest.approx(4 / 6, rel=1e-3)

    assert cf["rule_B"]["total_passed_without_rule"] == 3
    assert cf["rule_B"]["incremental_gain"] == 1
    assert cf["rule_B"]["pass_rate_without_rule"] == pytest.approx(3 / 6, rel=1e-3)

    # 4. Distribution of top blocking rules
    top_dist = {item["rule_id"]: item for item in report["top_blocking_rules_distribution"]}
    assert "rule_A" in top_dist
    dist_a = top_dist["rule_A"]["distribution"]
    assert dist_a["count"] == 3
    assert dist_a["min"] == 50.0
    assert dist_a["median"] == 60.0
    assert dist_a["p90"] == 68.0

    assert "rule_B" in top_dist
    dist_b = top_dist["rule_B"]["distribution"]
    assert dist_b["count"] == 2
    assert dist_b["min"] == 10.0
    assert dist_b["median"] == 15.0
    assert dist_b["p90"] == 19.0

    # 5. LLM funnel & verdict mix
    llm = report["llm_funnel"]
    assert llm["gate_passers"] == 2
    assert llm["model_approved"] == 1
    assert llm["model_refused"] == 1
    assert llm["llm_refusal_rate_of_gate_passers"] == 0.50

    vm = report["verdict_mix"]
    assert vm["pass"]["count"] == 1
    assert vm["fail"]["count"] == 5


def test_funnel_report_since_filtering(seeded_db):
    """Verify that --since filters out older candidates correctly."""
    report = generate_funnel_report(db_path=seeded_db, since="2026-09-02")
    assert report["total_candidates"] == 3
    assert report["baseline_summary"]["candidates_seen"] == 3
    assert report["baseline_summary"]["gate_passed"] == 0
    assert len(report["daily_funnel"]) == 1
    assert report["daily_funnel"][0]["date"] == "2026-09-02"


def test_funnel_report_empty_db(tmp_path):
    """Verify report handles an empty or missing database gracefully."""
    empty_db = tmp_path / "empty.db"
    conn = sqlite3.connect(str(empty_db))
    conn.commit()
    conn.close()

    report = generate_funnel_report(db_path=empty_db)
    assert report["total_candidates"] == 0
    assert report["baseline_summary"]["gate_passed"] == 0
    assert report["daily_funnel"] == []
    assert report["per_rule_stats"] == []

    # Text report formatting does not throw
    text = format_text_report(report)
    assert "Total candidates evaluated: 0" in text


def test_funnel_report_cli_invocation(seeded_db):
    """Verify scripts/gate_funnel_report.py executes via subprocess with text and JSON modes."""
    script_path = str(SCRIPTS_DIR / "gate_funnel_report.py")

    # Text mode
    res_text = subprocess.run(
        [sys.executable, script_path, "--db", str(seeded_db)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "GATE FUNNEL DIAGNOSTIC REPORT (§79 Phase G)" in res_text.stdout
    assert "rule_A" in res_text.stdout
    assert "rule_B" in res_text.stdout

    # JSON mode
    res_json = subprocess.run(
        [sys.executable, script_path, "--db", str(seeded_db), "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    parsed = json.loads(res_json.stdout)
    assert parsed["total_candidates"] == 6
    assert parsed["baseline_summary"]["gate_passed"] == 2
