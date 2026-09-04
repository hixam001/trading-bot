# Puts backend/ (this directory) on sys.path so every test — backend/tests
# AND live_execution/tests — can `import config`, `import run_live_cycle`,
# `from live_execution import ...` deterministically, regardless of the
# working directory pytest was launched from or test-file ordering.
import sys
from pathlib import Path

_BACKEND = str(Path(__file__).resolve().parent)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_live_state(tmp_path_factory):
    """Point every live-state reader at a throwaway directory for the suite.

    The break state and kill switch are REAL operator state written by the
    running live cycle. Without this, `not_on_break` (and every tick test
    that depends on the gate) reads whatever the bot happens to be doing:
    the suite goes red the moment the operator's bot takes a break, and a
    test could in principle write to live state. Hermetic by default —
    individual tests can still monkeypatch these config values.
    """
    import config

    state = tmp_path_factory.mktemp("live_state")
    originals = {
        "LIVE_STATE_DIR": getattr(config, "LIVE_STATE_DIR", None),
        "BREAK_STATE_FILE": getattr(config, "BREAK_STATE_FILE", None),
        "KILL_SWITCH_FILE": getattr(config, "KILL_SWITCH_FILE", None),
        # §49: the blocklist sidecar is REAL operator state (loss memory +
        # auto blocks). Without this, every tick-closing test (e2e mock tick,
        # exit engine, price guards) writes mock mints into the operator's
        # file — and auto-blocks them, so the NEXT run filters every mock
        # candidate and the suite goes red. Same hermetic-by-default
        # contract as the break/kill-switch state above.
        "BLOCKLIST_STATE_FILE": getattr(config, "BLOCKLIST_STATE_FILE", None),
        # §54: the encrypted-secret-store key file and the fomo Privy
        # sidecar both default into the REPO ROOT. Point both at the
        # throwaway state dir so the suite never generates real key
        # material next to the operator's, and never reads/migrates the
        # operator's real sidecar.
        "SECRET_STORE_KEY": getattr(config, "SECRET_STORE_KEY", None),
        "SECRET_STORE_KEY_FILE": getattr(config, "SECRET_STORE_KEY_FILE", None),
        "FOMO_PRIVY_STATE_FILE": getattr(config, "FOMO_PRIVY_STATE_FILE", None),
    }
    config.LIVE_STATE_DIR = state
    config.BREAK_STATE_FILE = str(state / "break_state.json")
    config.KILL_SWITCH_FILE = str(state / "kill_switch.json")
    config.BLOCKLIST_STATE_FILE = str(state / "blocklist_state.json")
    config.SECRET_STORE_KEY = ""
    config.SECRET_STORE_KEY_FILE = str(state / ".secret_store.key")
    config.FOMO_PRIVY_STATE_FILE = str(state / ".fomo_privy.json")
    yield state
    for name, value in originals.items():
        if value is not None:
            setattr(config, name, value)
