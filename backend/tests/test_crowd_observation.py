"""Engine observation transport: clock boundaries and process isolation."""
import json

import pytest

from data_providers import crowd


@pytest.mark.parametrize("age,stale", [(0, False), (900, False), (901, True), (-1, True)])
def test_observation_freshness(tmp_path, monkeypatch, age, stale):
    path = tmp_path / "chain.json"
    monkeypatch.setattr(crowd, "_chain_status_path", lambda: path)
    monkeypatch.setattr(crowd.time, "time", lambda: 2000.0)
    path.write_text(json.dumps({"observed_at": 2000 - age,
                               "configured": ["scrapling"],
                               "benched": ["scrapling"], "exhausted": True}))
    result = crowd.observed_chain_status()
    assert result["stale"] is stale
    assert result["exhausted"] is (None if stale else True)


@pytest.mark.parametrize("content", [None, "{broken", "[]", "{}"])
def test_missing_or_corrupt_observation_is_unknown(tmp_path, monkeypatch, content):
    path = tmp_path / "chain.json"
    monkeypatch.setattr(crowd, "_chain_status_path", lambda: path)
    if content is not None:
        path.write_text(content)
    assert crowd.observed_chain_status()["exhausted"] is None


def test_published_observation_does_not_read_api_benches(tmp_path, monkeypatch):
    monkeypatch.setattr(crowd, "_chain_status_path", lambda: tmp_path / "chain.json")
    monkeypatch.setattr(crowd, "_configured_scrapers", lambda: [("scrapling", None)])
    monkeypatch.setattr(crowd, "_is_benched", lambda _: True)
    crowd.publish_chain_status()
    # Simulate the separate API process's empty local state.
    monkeypatch.setattr(crowd, "_is_benched", lambda _: False)
    assert crowd.chain_status()["exhausted"] is False
    assert crowd.observed_chain_status()["exhausted"] is True
