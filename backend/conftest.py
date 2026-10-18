import os
import sys
import tempfile
from pathlib import Path

# Hermetic live execution state for pytest (§81 Phase F-1):
# Set up a dedicated temporary directory before any imports can load live_execution.config.
_SESSION_LIVE_STATE = Path(tempfile.mkdtemp(prefix="pytest_live_execution_state_"))
os.environ["LIVE_EXECUTION_STATE_DIR"] = str(_SESSION_LIVE_STATE)

# Puts both repo root and backend/ on sys.path so every test can import
# root scripts, config, live_execution deterministically regardless of
# working directory or test runner invocation mode (bare pytest or python -m).
_BACKEND = str(Path(__file__).resolve().parent)
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest

_REAL_LIVE_STATE_DIR = Path(__file__).resolve().parent / "live_execution" / "state"
_INITIAL_STATE_SNAPSHOT: dict[str, tuple[int, int]] = {}


def _snapshot_dir(directory: Path) -> dict[str, tuple[int, int]]:
    snapshot = {}
    if directory.is_dir():
        for p in directory.rglob("*"):
            if p.is_file():
                try:
                    st = p.stat()
                    snapshot[str(p.relative_to(directory))] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    pass
    return snapshot


def pytest_sessionstart(session):
    global _INITIAL_STATE_SNAPSHOT
    _REAL_LIVE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    _INITIAL_STATE_SNAPSHOT = _snapshot_dir(_REAL_LIVE_STATE_DIR)


def pytest_sessionfinish(session, exitstatus):
    current_snapshot = _snapshot_dir(_REAL_LIVE_STATE_DIR)
    if current_snapshot != _INITIAL_STATE_SNAPSHOT:
        diff_added = set(current_snapshot.keys()) - set(_INITIAL_STATE_SNAPSHOT.keys())
        diff_removed = set(_INITIAL_STATE_SNAPSHOT.keys()) - set(current_snapshot.keys())
        diff_changed = {
            k for k in current_snapshot.keys() & _INITIAL_STATE_SNAPSHOT.keys()
            if current_snapshot[k] != _INITIAL_STATE_SNAPSHOT[k]
        }
        msg = (
            f"HERMETICITY VIOLATION: backend/live_execution/state/ was modified during the test session!\n"
            f"Added: {sorted(diff_added)}\nRemoved: {sorted(diff_removed)}\nChanged: {sorted(diff_changed)}"
        )
        session.exitstatus = 1
        pytest.exit(msg)


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


import ipaddress
import socket
from typing import Any

_IS_NETWORK_EXEMPT = False


def _is_loopback_host(host: Any) -> bool:
    if not host:
        return True
    if isinstance(host, (bytes, bytearray)):
        host = host.decode("utf-8", errors="ignore")
    host_str = str(host).strip().lower()
    if host_str in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    try:
        return ipaddress.ip_address(host_str).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _check_network_marker(request):
    global _IS_NETWORK_EXEMPT
    _IS_NETWORK_EXEMPT = bool(request.node.get_closest_marker("network"))
    try:
        yield
    finally:
        _IS_NETWORK_EXEMPT = False


@pytest.fixture(autouse=True, scope="session")
def _hermetic_network_guard():
    """Session-scoped socket patch blocking all non-loopback network calls (§82 Phase G)."""
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self, address):
        if not _IS_NETWORK_EXEMPT:
            if getattr(self, "family", None) != socket.AF_UNIX:
                if isinstance(address, tuple):
                    host = address[0]
                    port = address[1] if len(address) > 1 else 0
                    if not _is_loopback_host(host):
                        raise AssertionError(f"network access attempted in test: {host}:{port}")
        return orig_connect(self, address)

    def guarded_connect_ex(self, address):
        if not _IS_NETWORK_EXEMPT:
            if getattr(self, "family", None) != socket.AF_UNIX:
                if isinstance(address, tuple):
                    host = address[0]
                    port = address[1] if len(address) > 1 else 0
                    if not _is_loopback_host(host):
                        raise AssertionError(f"network access attempted in test: {host}:{port}")
        return orig_connect_ex(self, address)

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        if not _IS_NETWORK_EXEMPT:
            if not _is_loopback_host(host):
                raise AssertionError(f"network access attempted in test: {host}:{port}")
        return orig_getaddrinfo(host, port, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.getaddrinfo = guarded_getaddrinfo

    yield

    socket.socket.connect = orig_connect
    socket.socket.connect_ex = orig_connect_ex
    socket.getaddrinfo = orig_getaddrinfo

