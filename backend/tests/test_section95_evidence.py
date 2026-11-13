"""§95 S3: operator documentation must describe the shipped telemetry contract."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("filename", ["docs/18_SECURITY.md", "docs/11_DEPLOYMENT.md"])
def test_operator_runbooks_describe_private_restart_scoped_counters(filename):
    text = (ROOT / filename).read_text()
    assert "consensus_counters.json" in text
    assert "scripts/consensus_status.py" in text
    assert "since_boot_at" in text and "STALE" in text
    assert "SIM_CONSENSUS_REFUSED" in text and "SIM_CONSENSUS_ATTEMPT" in text
    assert '`/api/health` returns exactly `{"status":"ok"}`' in text


@pytest.mark.parametrize("filename", ["handoff.md", "memory-bank/activeContext.md",
                                      "memory-bank/decisionLog.md", "memory-bank/progress.md",
                                      "memory-bank/session-log.md"])
def test_section95_handoffs_record_restart_scope_and_unverified_limits(filename):
    text = (ROOT / filename).read_text().split("## §95 —", 1)[-1]
    assert "2026-11-13" in text and "consensus_counters.json" in text
    assert "UNVERIFIED" in text
    assert all(word in text for word in ("buy-refusal", "VM/Docker", "Supabase"))


def test_behavior_record_lists_section95_changes():
    text = (ROOT / "docs/20_BEHAVIOUR_CHANGES_SINCE_3FEA445.md").read_text()
    assert "§95 S1" in text and "consensus_counters.json" in text
    assert "§95 S2" in text and "payer" in text


def test_generated_mutation_log_names_all_four_section95_catches():
    text = (ROOT / "docs/17_MUTATION_LOG.md").read_text()
    for name in ("test_public_health_is_exactly_status_ok",
                 "test_status_missing_is_no_data_never_healthy_zeros",
                 "test_snapshot_write_failure_never_stops_heartbeat",
                 "test_check_f_consensus_uses_explicit_payer_even_when_intent_wallet_differs"):
        assert name in text
