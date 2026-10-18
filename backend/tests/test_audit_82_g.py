"""
backend/tests/test_audit_82_g.py — Phase G test suite for hermetic network guard.

Verifies:
1. Non-loopback connect (e.g. 1.1.1.1:443) raises AssertionError containing "network access attempted".
2. Non-loopback getaddrinfo (e.g. example.com:80) raises AssertionError containing "network access attempted".
3. Loopback connect to a closed local port raises ConnectionRefusedError / OSError, NOT AssertionError.
4. Loopback getaddrinfo ("localhost", "127.0.0.1", "::1") succeeds.
5. @pytest.mark.network exempts marked tests from the socket guard.
"""

import socket
import pytest


def test_network_guard_blocks_remote_ip_connect():
    """Connecting to 1.1.1.1:443 must raise AssertionError containing 'network access attempted'."""
    with pytest.raises(AssertionError, match="network access attempted in test: 1.1.1.1:443"):
        socket.create_connection(("1.1.1.1", 443), timeout=0.1)


def test_network_guard_blocks_remote_dns_resolution():
    """getaddrinfo on non-loopback host must raise AssertionError containing 'network access attempted'."""
    with pytest.raises(AssertionError, match="network access attempted in test: example.com:80"):
        socket.getaddrinfo("example.com", 80)


def test_network_guard_allows_loopback_connect():
    """Loopback connect to a closed port must raise ConnectionRefusedError/OSError, NOT AssertionError."""
    # Find an unused local port
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    try:
        # Connecting to closed loopback port must raise ConnectionRefusedError or OSError
        with pytest.raises((ConnectionRefusedError, OSError)) as excinfo:
            s_conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s_conn.connect(("127.0.0.1", port))
            finally:
                s_conn.close()
        assert not isinstance(excinfo.value, AssertionError), f"Expected ConnectionRefusedError, got {excinfo.value}"
    except AssertionError:
        pytest.fail("Loopback connection raised AssertionError instead of ConnectionRefusedError/OSError")


def test_network_guard_allows_loopback_getaddrinfo():
    """getaddrinfo on localhost / 127.0.0.1 / ::1 must succeed."""
    res1 = socket.getaddrinfo("127.0.0.1", 80)
    assert res1
    res2 = socket.getaddrinfo("localhost", 80)
    assert res2


@pytest.mark.network
def test_network_guard_exempt_with_network_marker():
    """A test marked with @pytest.mark.network is exempt from the socket guard."""
    # In an offline environment this may fail at OS level (e.g. host unreachable or timeout),
    # but it MUST NOT raise AssertionError from our guard!
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=0.01)
    except AssertionError as exc:
        pytest.fail(f"Network guard was not disabled for @pytest.mark.network: {exc}")
    except (OSError, TimeoutError):
        # Normal OS level network error when offline / timed out
        pass
