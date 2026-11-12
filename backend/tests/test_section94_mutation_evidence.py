"""§94 S4: the named control-field mutants must hit active enforcement."""
import sys
import pytest
from scripts.mutation_check import MUTATIONS, run_mutation


@pytest.mark.parametrize('mutation_id', ['81C_SOLANA_AGREE_CLOSE_AUTH', '81C_SOLANA_AGREE_DELEGATED_AMT'])
@pytest.mark.parametrize("plugin_autoload", [True, False])
def test_named_control_field_mutation_is_caught_by_existing_regression(mutation_id, monkeypatch, plugin_autoload):
    # CI discovers installed plugins; the OS sandbox disables discovery.
    # Exercise both incoming environments through the real child pytest.
    if plugin_autoload:
        monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)
    else:
        monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTEST_PLUGINS", "pytest_asyncio.plugin")
    mutation = next(m for m in MUTATIONS if m.mutation_id == mutation_id)
    caught, test, failure = run_mutation(mutation, sys.executable)
    assert caught, f'{mutation_id} SURVIVED: it must target the active enforcement'
    assert test == 'test_c2_responses_agree_control_fields_differ', failure
