"""
tests/test_api_endpoints.py — API endpoint shape tests.

Tests that every GET endpoint returns HTTP 200 with the expected response
shape against a fresh (empty) test database.

Uses FastAPI's TestClient (synchronous) and aiosqlite for setup.
Endpoints tested: /api/feed, /api/holdings, /api/journal, /api/stats,
                  /api/knowledge-base, /api/promotion-gate,
                  /api/learning-window, /api/system-status

Run: pytest tests/test_api_endpoints.py -v
"""
import sys
import os
import asyncio
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

# Override the DB path to a temp file before importing anything that reads config
tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmpdb.close()
os.environ["DB_PATH"] = tmpdb.name

import config
config.DB_PATH = tmpdb.name  # type: ignore[assignment]

# Now import the app (which will use the patched DB_PATH)
from api.main import app
from api import db as db_module

# One-time DB init
asyncio.run(db_module.init_db())

client = TestClient(app)


class TestFeedEndpoint:
    def test_returns_200(self):
        resp = client.get("/api/feed")
        assert resp.status_code == 200

    def test_response_shape(self):
        data = client.get("/api/feed").json()
        assert "events" in data
        assert "limit" in data
        assert "offset" in data
        assert "count" in data
        assert isinstance(data["events"], list)

    def test_pagination_params(self):
        resp = client.get("/api/feed?limit=10&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 10
        assert data["offset"] == 0

    def test_invalid_limit_rejected(self):
        resp = client.get("/api/feed?limit=0")
        assert resp.status_code == 422  # FastAPI validation


class TestHoldingsEndpoint:
    def test_returns_200(self):
        resp = client.get("/api/holdings")
        assert resp.status_code == 200

    def test_response_shape(self):
        data = client.get("/api/holdings").json()
        assert "holdings" in data
        assert "open_count" in data
        assert "cash_balance_usd" in data
        assert isinstance(data["holdings"], list)
        assert isinstance(data["cash_balance_usd"], (int, float))

    def test_initial_cash_matches_config(self):
        data = client.get("/api/holdings").json()
        assert abs(data["cash_balance_usd"] - config.INITIAL_CASH_USD) < 0.01


class TestJournalEndpoint:
    def test_returns_200(self):
        resp = client.get("/api/journal")
        assert resp.status_code == 200

    def test_response_shape(self):
        data = client.get("/api/journal").json()
        assert "trades" in data
        assert "limit" in data
        assert "offset" in data
        assert "sort" in data
        assert isinstance(data["trades"], list)

    def test_sort_by_pnl(self):
        resp = client.get("/api/journal?sort=pnl")
        assert resp.status_code == 200

    def test_invalid_sort_rejected(self):
        resp = client.get("/api/journal?sort=invalid")
        assert resp.status_code == 422


class TestStatsEndpoint:
    def test_returns_200(self):
        resp = client.get("/api/stats")
        assert resp.status_code == 200

    def test_response_shape(self):
        data = client.get("/api/stats").json()
        required_keys = {
            "cash_balance_usd",
            "total_realized_pnl_usd",
            "open_positions",
            "total_closed_trades",
            "win_count",
            "loss_count",
            "max_drawdown_pct",
            "equity_curve",
            "initial_cash_usd",
        }
        for key in required_keys:
            assert key in data, f"Missing key: {key}"

    def test_equity_curve_is_list(self):
        data = client.get("/api/stats").json()
        assert isinstance(data["equity_curve"], list)

    def test_initial_cash_correct(self):
        data = client.get("/api/stats").json()
        assert abs(data["initial_cash_usd"] - config.INITIAL_CASH_USD) < 0.01


class TestKnowledgeBaseEndpoint:
    def test_returns_200(self):
        resp = client.get("/api/knowledge-base")
        assert resp.status_code == 200

    def test_response_shape(self):
        data = client.get("/api/knowledge-base").json()
        assert "static_knowledge" in data
        assert "ingested_files" in data
        assert "dynamic_stats" in data
        assert isinstance(data["ingested_files"], list)

    def test_static_knowledge_is_string(self):
        data = client.get("/api/knowledge-base").json()
        assert isinstance(data["static_knowledge"], str)


class TestPromotionGateEndpoint:
    def test_returns_200(self):
        resp = client.get("/api/promotion-gate")
        assert resp.status_code == 200

    def test_response_shape(self):
        data = client.get("/api/promotion-gate").json()
        assert "all_criteria_met" in data
        assert "criteria" in data
        assert "summary" in data
        assert "note" in data
        assert isinstance(data["criteria"], list)
        assert len(data["criteria"]) == 5

    def test_all_false_on_empty_db(self):
        data = client.get("/api/promotion-gate").json()
        assert data["all_criteria_met"] is False

    def test_note_always_present(self):
        data = client.get("/api/promotion-gate").json()
        assert len(data["note"]) > 10

    def test_cannot_write_through_gate(self):
        """Verify there is no POST /api/promotion-gate endpoint."""
        resp = client.post("/api/promotion-gate", json={})
        assert resp.status_code == 405  # Method Not Allowed


class TestLearningWindowEndpoint:
    def test_returns_200(self):
        resp = client.get("/api/learning-window")
        assert resp.status_code == 200

    def test_response_shape(self):
        data = client.get("/api/learning-window").json()
        required = {"days_elapsed", "days_target", "trades_closed", "trades_target",
                    "window_started", "window_complete"}
        for key in required:
            assert key in data, f"Missing key: {key}"

    def test_targets_match_config(self):
        data = client.get("/api/learning-window").json()
        assert data["days_target"] == config.LEARNING_WINDOW_DAYS
        assert data["trades_target"] == config.PROMOTION_MIN_TRADES


class TestSystemStatusEndpoint:
    def test_returns_200(self):
        resp = client.get("/api/system-status")
        assert resp.status_code == 200

    def test_response_shape(self):
        data = client.get("/api/system-status").json()
        assert "paper_trading_only" in data
        assert "data_backend" in data
        assert "ollama" in data
        assert "config" in data

    def test_paper_trading_is_true(self):
        """PAPER_TRADING_ONLY must always be True."""
        data = client.get("/api/system-status").json()
        assert data["paper_trading_only"] is True


class TestHealthEndpoint:
    def test_returns_200(self):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_paper_trading_in_health(self):
        data = client.get("/health").json()
        assert data["paper_trading_only"] is True
