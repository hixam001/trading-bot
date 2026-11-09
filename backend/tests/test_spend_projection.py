"""§93 R6: offline spend projection matches the enabled live call graph."""
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.spend_projection import project, render_projection


def _row(projection, metric, provider):
    return next(row for row in projection.rows
                if (row.metric, row.provider) == (metric, provider))


def test_hand_computed_live_brain_social_search_and_narrative_budget():
    settings = {
        "DATA_BACKEND": "live", "MAIN_LLM_PROVIDER": "deepseek",
        "DEEPSEEK_API_KEY": "placeholder", "SOCIAL_LLM_API_KEY": "placeholder",
        "LLM_BRAIN": True, "TICK_INTERVAL_SECONDS": 3600,
        "MAX_CANDIDATES_PER_TICK": 10, "RESEARCH_PER_TICK": 4,
        "THESIS_RESTATE_PER_PASS": 2, "THESIS_RESTATE_STALE_HOURS": 6,
        "SEARXNG_URL": "http://placeholder", "BRAVE_SEARCH_API_KEY": "placeholder",
        "FIRECRAWL_API_KEY": "placeholder",
    }
    result = project(settings, typical_candidates_per_tick=2,
                     typical_closes_per_day=1)
    # 24 brain + 3 open positions * 4 restates/day + 1 reflection = 37.
    assert _row(result, "llm_calls", "deepseek").typical == 37
    # Worst: 24 brain + 240 thinker + 48 repeated failed restates +
    # (3 initial open positions + 3 replacements per tick) reflections.
    assert _row(result, "llm_calls", "deepseek").worst == 387
    assert _row(result, "llm_calls", "groq").typical == 48
    assert _row(result, "llm_calls", "groq").worst == 240
    assert _row(result, "search_calls", "web").typical == 48
    assert _row(result, "search_calls", "web").worst == 720
    assert _row(result, "dex_research_calls", "dexscreener").typical == 96
    assert result.llm_calls_typical == 85
    assert result.llm_calls_worst == 627


def test_brain_missing_verdicts_and_provider_fallbacks_are_counted():
    settings = {"DATA_BACKEND": "live", "DEEPSEEK_API_KEY": "placeholder",
                "GROQ_API_KEY": "placeholder", "TICK_INTERVAL_SECONDS": 3600,
                "MAX_CANDIDATES_PER_TICK": 10}
    result = project(settings, typical_candidates_per_tick=10,
                     typical_closes_per_day=0)
    # Brain covers 8 of 10; two cleared candidates still need the thinker.
    assert _row(result, "llm_calls", "deepseek").typical == 24 + 48 + 12
    assert _row(result, "llm_calls", "groq").typical == 0
    assert _row(result, "llm_calls", "groq").worst == 24


def test_main_groq_role_chain_can_attempt_same_provider_twice():
    result = project({"DATA_BACKEND": "live", "MAIN_LLM_PROVIDER": "groq",
                      "GROQ_API_KEY": "placeholder", "TICK_INTERVAL_SECONDS": 3600,
                      "MAX_CANDIDATES_PER_TICK": 1})
    brain = next(stage for stage in result.stages if stage.name == "brain")
    assert brain.typical_calls == 24
    assert brain.worst_calls == 48


@pytest.mark.parametrize("disabled", ["false", "", "0", False])
def test_disabled_brain_uses_per_candidate_thinker(disabled):
    result = project({"DATA_BACKEND": "live", "LLM_BRAIN": disabled,
                      "DEEPSEEK_API_KEY": "placeholder", "TICK_INTERVAL_SECONDS": 3600,
                      "MAX_CANDIDATES_PER_TICK": 5},
                     typical_candidates_per_tick=2, typical_closes_per_day=0)
    assert not any(stage.name == "brain" for stage in result.stages)
    thinker = next(stage for stage in result.stages if stage.name == "thinker")
    assert (thinker.typical_calls, thinker.worst_calls) == (48, 120)


@pytest.mark.parametrize("settings", [{}, {"DATA_BACKEND": "live"},
    {"DATA_BACKEND": "mock", "DEEPSEEK_API_KEY": "placeholder",
     "GROQ_API_KEY": "placeholder", "SOCIAL_LLM_API_KEY": "placeholder",
     "SEARXNG_URL": "http://placeholder"}])
def test_mock_or_keyless_stages_make_no_llm_or_search_calls(settings):
    result = project(settings)
    assert result.llm_calls_typical == result.llm_calls_worst == 0
    assert _row(result, "search_calls", "web").worst == 0


def test_caps_compare_each_provider_and_do_not_mutate_settings():
    settings = {"DATA_BACKEND": "live", "DEEPSEEK_API_KEY": "placeholder",
                "TICK_INTERVAL_SECONDS": 3600, "MAX_CANDIDATES_PER_TICK": 1,
                "LLM_DAILY_CALL_CAP": 1, "LLM_DAILY_TOKEN_CAP": 1,
                "LLM_DAILY_COST_CAP_USD": 0, "SEARCH_DAILY_CALL_CAP": 0}
    before = dict(settings)
    result = project(settings)
    assert any("typical" in warning and "LLM_DAILY_CALL_CAP" in warning
               for warning in result.warnings)
    assert any("LLM_DAILY_TOKEN_CAP" in warning for warning in result.warnings)
    assert any("LLM_DAILY_COST_CAP_USD" in warning for warning in result.warnings)
    assert settings == before
    assert result.recommendations["LLM_DAILY_CALL_CAP"] >= result.llm_calls_worst


def test_high_caps_have_no_warning_and_config_object_is_supported():
    settings = SimpleNamespace(DATA_BACKEND="live", DEEPSEEK_API_KEY="placeholder",
        TICK_INTERVAL_SECONDS=3600, MAX_CANDIDATES_PER_TICK=1,
        LLM_DAILY_CALL_CAP=100_000, LLM_DAILY_TOKEN_CAP=1_000_000_000,
        LLM_DAILY_COST_CAP_USD=100_000, SEARCH_DAILY_CALL_CAP=100_000)
    result = project(settings)
    assert result.warnings == ()


def test_render_never_prints_configured_secrets_or_urls():
    result = project({"DATA_BACKEND": "live", "DEEPSEEK_API_KEY": "PRIVATE-API-KEY",
                      "SEARXNG_URL": "http://PRIVATE-SEARCH-URL"})
    output = render_projection(result)
    assert "PRIVATE" not in output
    assert "Typical" in output and "Worst" in output
    assert "LLM_DAILY_CALL_CAP=" in output
    assert "estimate" in output.lower()
    suggested_tokens = next(line for line in output.splitlines()
                            if line.startswith("LLM_DAILY_TOKEN_CAP="))
    assert int(suggested_tokens.split("=", 1)[1]) == result.recommendations["LLM_DAILY_TOKEN_CAP"]


def test_invalid_tick_interval_is_reported_instead_of_zero_division():
    with pytest.raises(ValueError, match="TICK_INTERVAL_SECONDS"):
        project({"TICK_INTERVAL_SECONDS": 0})


def test_cli_reads_literal_env_and_exported_values_without_importing_config(tmp_path):
    settings = tmp_path / "projection.env"
    settings.write_text('DATA_BACKEND=live\nexport DEEPSEEK_API_KEY="PRIVATE-API-KEY"\n'
                        'TICK_INTERVAL_SECONDS=3600 # overridden\n')
    script = Path(__file__).resolve().parents[2] / "scripts" / "spend_projection.py"
    proc = subprocess.run([sys.executable, str(script), "--env-file", str(settings)],
                          cwd=tmp_path, env={"TICK_INTERVAL_SECONDS": "7200",
                                             "ADMIN_TOKEN": "weak"},
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    assert "12 maximum ticks/day" in proc.stdout
    assert "PRIVATE-API-KEY" not in proc.stdout + proc.stderr
    assert "Total LLM calls/day" in proc.stdout


def test_env_provider_and_backend_normalization_matches_runtime():
    env = {'DATA_BACKEND': ' Live ', 'MAIN_LLM_PROVIDER': ' DeepSeek ',
        'DEEPSEEK_API_KEY': 'placeholder', 'LLM_DAILY_CALL_CAP': '1'}
    result = project(env)
    assert result.llm_calls_typical > 0
    assert any('LLM_DAILY_CALL_CAP' in warning for warning in result.warnings)
