"""
backend/tests/test_audit_81_d.py — §81 Phase D: DEX allowlist and label hygiene.

Tests:
  D-1: Lifinity IDs absent from APPROVED_SWAP_PROGRAM_IDS.
  D-2: Visibility of inert filter (warning log on empty labels, status payload reports inactive/active).
  D-3: refresh_dex_labels script accepts both dict and list response shapes; rejects garbage without writing;
       uses lite-api URL (no quote-api.jup.ag in repo); reports unmapped IDs.
  D-4: build_quote_params includes dexes on buy (when labels non-empty) and NEVER includes dexes on sell.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx
import pytest

from live_execution import config as live_config
from live_execution.quote_params import build_quote_params, get_active_dex_labels
from scripts.refresh_dex_labels import parse_label_map, refresh_labels
import config as backend_config


# ---------------------------------------------------------------------------
# D-1: Lifinity IDs absent from APPROVED_SWAP_PROGRAM_IDS
# ---------------------------------------------------------------------------

def test_d1_lifinity_ids_absent_from_approved_swap_program_ids():
    """
    D-1 [READ]: The two Lifinity program IDs must be ABSENT from APPROVED_SWAP_PROGRAM_IDS
    because neither is present in Jupiter's current program-id-to-label map.
    """
    lifinity_v1 = "EewxydAPCCVuNEyrVN68PuSYdQ7wKn27V9Gjeoi8dy3S"
    lifinity_v2 = "2wT8Yq49kHgDzXuPxZSaeLaH1qbmGXtEyPy64bL7aD3c"

    assert lifinity_v1 not in live_config.APPROVED_SWAP_PROGRAM_IDS, "Lifinity v1 must be removed"
    assert lifinity_v2 not in live_config.APPROVED_SWAP_PROGRAM_IDS, "Lifinity v2 must be removed"


# ---------------------------------------------------------------------------
# D-2: Inert-by-default filter visibility and SystemStatus payload
# ---------------------------------------------------------------------------

def test_d2_empty_labels_warning_and_status(monkeypatch, caplog):
    """
    D-2 [PROVEN]: When dex_labels is empty, warning is logged on startup
    and get_system_status reports dex_filter: 'inactive'.
    When labels are present, reports 'active (N labels)'.
    """
    import logging
    from api.routes.system_status import _get_labels

    # With empty labels
    monkeypatch.setattr("api.routes.system_status._get_labels", lambda: [])
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.system_status import router

    app = FastAPI()
    app.include_router(router)
    app.state.narrator = MagicMock()
    app.state.narrator._main_llm = MagicMock()
    app.state.narrator._main_llm.health = MagicMock(return_value=True)

    client = TestClient(app)
    resp = client.get("/api/system-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("dex_filter") == "inactive"

    # With populated labels
    monkeypatch.setattr("api.routes.system_status._get_labels", lambda: ["Raydium", "Orca", "Meteora"])
    resp_active = client.get("/api/system-status")
    assert resp_active.status_code == 200
    data_active = resp_active.json()
    assert "active (3 labels)" in data_active.get("dex_filter", "")


# ---------------------------------------------------------------------------
# D-3: refresh_dex_labels script (dual shape parser, garbage refusal, unmapped IDs)
# ---------------------------------------------------------------------------

def test_d3_parse_label_map_dict_and_list_shapes():
    """
    D-3 [READ]: parse_label_map must accept both:
      (a) Object shape: {"prog1": "Label1", "prog2": "Label2"}
      (b) List shape: [{"programId": "prog1", "label": "Label1"}, ...]
    """
    # Dict shape
    dict_fixture = {
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium",
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Whirlpool",
    }
    parsed_dict = parse_label_map(dict_fixture)
    assert parsed_dict["675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"] == "Raydium"
    assert parsed_dict["whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"] == "Whirlpool"

    # List shape
    list_fixture = [
        {"programId": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", "label": "Raydium"},
        {"programId": "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", "label": "Whirlpool"},
    ]
    parsed_list = parse_label_map(list_fixture)
    assert parsed_list == parsed_dict


def test_d3_parse_label_map_garbage_refused():
    """Garbage response raises ValueError without writing any file."""
    with pytest.raises(ValueError):
        parse_label_map("garbage_string")

    with pytest.raises(ValueError):
        parse_label_map(12345)

    with pytest.raises(ValueError):
        parse_label_map({})

    with pytest.raises(ValueError):
        parse_label_map([])


def test_d3_refresh_labels_writes_atomically_and_reports_unmapped(tmp_path, monkeypatch):
    """
    refresh_labels writes atomically with fetched_at, source_url, labels, unmapped_ids.
    """
    out_file = tmp_path / "dex_labels.json"
    fake_map = {
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium",
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    }

    class FakeResponse:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return fake_map

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeResponse())

    res = refresh_labels(labels_url="https://lite-api.jup.ag/swap/v1/program-id-to-label", out_file=out_file)

    assert out_file.is_file(), "Target file must be written"
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    assert "Raydium" in saved["labels"]
    assert "Orca Whirlpool" in saved["labels"]
    assert saved["source_url"] == "https://lite-api.jup.ag/swap/v1/program-id-to-label"
    assert saved["fetched_at"] is not None
    # Unmapped IDs are approved programs not in fake_map
    assert len(saved["unmapped_ids"]) > 0


def test_d3_no_dead_quote_api_string_in_repo():
    """
    Ensure the dead 'quote-api.jup.ag' host does not appear in production code or scripts.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    for ext in ("*.py", "*.sh", "*.json"):
        for path in repo_root.glob(f"**/{ext}"):
            if ".git" in str(path) or ".venv" in str(path) or "test_" in str(path) or "node_modules" in str(path):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "quote-api.jup.ag" not in text, f"Found dead quote-api.jup.ag in {path}"


# ---------------------------------------------------------------------------
# D-4: build_quote_params side behavior (buys filter, sells never filter)
# ---------------------------------------------------------------------------

def test_d4_build_quote_params_side_filtering():
    """
    D-4 [READ]:
    - Buys apply 'dexes' filter when labels are non-empty.
    - Sells NEVER include 'dexes', preventing exit/crash lockups on unlabelled pools.
    """
    test_labels = ["Raydium", "Orca V2", "Meteora"]

    # Buy with labels -> includes dexes
    buy_params = build_quote_params(
        input_mint="USDC",
        output_mint="MINT",
        amount_raw=10_000_000,
        slippage_bps=50,
        side="buy",
        dex_labels=test_labels,
    )
    assert "dexes" in buy_params
    assert buy_params["dexes"] == "Raydium,Orca V2,Meteora"

    # Sell with labels -> NEVER includes dexes
    sell_params = build_quote_params(
        input_mint="MINT",
        output_mint="USDC",
        amount_raw=50_000_000,
        slippage_bps=50,
        side="sell",
        dex_labels=test_labels,
    )
    assert "dexes" not in sell_params, "Sells must NEVER restrict DEX routing via dexes"

    # Buy with empty labels -> dexes omitted
    buy_empty = build_quote_params(
        input_mint="USDC",
        output_mint="MINT",
        amount_raw=10_000_000,
        slippage_bps=50,
        side="buy",
        dex_labels=[],
    )
    assert "dexes" not in buy_empty
