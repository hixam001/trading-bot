"""§93 R2 — safety-state write failures are loud; TLS never silently downgrades."""
from __future__ import annotations

import logging
import ssl

import pytest
from cryptography.fernet import Fernet

import alerts
import config


@pytest.fixture(autouse=True)
def _reset_alert_state():
    alerts._reset_state_write_alerts()
    yield
    alerts._reset_state_write_alerts()


def _capture_alerts(monkeypatch) -> list:
    sent: list = []

    def _fake(event, message, **kwargs):
        sent.append((event, message, kwargs))

    monkeypatch.setattr(alerts, "alert_nowait", _fake)
    return sent


# ---------------------------------------------------------------------------
# blocklist safety state
# ---------------------------------------------------------------------------

def test_blocklist_write_failure_logs_error_and_alerts_hourly(
        rofs_service, monkeypatch, caplog):
    import blocklist

    sent = _capture_alerts(monkeypatch)
    rofs_service("live")
    monkeypatch.setattr(config, "BLOCKLIST_STATE_FILE",
                        "/app/backend/blocklist_state.json")

    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            blocklist.record_close_outcome("MINT_X", "XXX", "exit_stop_loss",
                                           -1.0)
        blocklist.block_mint("MINT_X", "XXX", "manual block")

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "blocklist write failure was not logged at ERROR"
    assert any(r.levelno == logging.ERROR for r in errors)
    assert sent, "blocklist write failure did not enqueue an alert"
    assert sent[0][0] == alerts.AlertEvent.STATE_WRITE_FAILURE
    # Deduped: three failed saves + one failed block within the hour -> 1 alert.
    assert len([s for s in sent
                if s[0] == alerts.AlertEvent.STATE_WRITE_FAILURE]) == 1
    # The tick is unchanged: the control fails non-fatally.
    assert not blocklist.is_blocked_mint("MINT_X")


# ---------------------------------------------------------------------------
# API session revocations
# ---------------------------------------------------------------------------

def test_session_revocation_write_failure_is_loud(
        rofs_service, monkeypatch, caplog):
    from api import auth
    from api.security_monitor import SecurityEventType, security_monitor

    monkeypatch.setattr(config, "ADMIN_TOKEN",
                        "test-operator-token-§93-r2-32-chars")
    rofs_service("api")
    monkeypatch.setattr(config, "SESSION_REVOCATION_FILE",
                        "/app/state/session_revocations.json")
    security_monitor.clear()

    token = auth.create_session_token(config.ADMIN_TOKEN)
    with caplog.at_level(logging.ERROR):
        auth.revoke_session_token(token)

    assert any(r.levelno == logging.ERROR for r in caplog.records), \
        "session revocation write failure was not logged at ERROR"
    events = [e for e in security_monitor.get_recent_events()
              if e["event_type"] == SecurityEventType.INTERNAL_ERROR.value]
    assert events, "session revocation write failure emitted no SecurityEvent"


# ---------------------------------------------------------------------------
# Privy rotated refresh token
# ---------------------------------------------------------------------------

def test_privy_persist_failure_is_loud(rofs_service, monkeypatch, caplog):
    from data_providers import crowd

    sent = _capture_alerts(monkeypatch)
    rofs_service("live")
    monkeypatch.setattr(config, "FOMO_PRIVY_STATE_FILE",
                        "/app/.fomo_privy.json")
    monkeypatch.setattr(config, "SECRET_STORE_KEY",
                        Fernet.generate_key().decode("utf-8"))

    with caplog.at_level(logging.ERROR):
        crowd._persist_refresh("rotated-token-§93-r2")

    assert any(r.levelno == logging.ERROR and r.name == "data_providers.crowd"
               and "could not persist rotated refresh token" in r.getMessage()
               for r in caplog.records), "Privy persist failure was not logged at ERROR"
    assert any(s[0] == alerts.AlertEvent.STATE_WRITE_FAILURE for s in sent)


# ---------------------------------------------------------------------------
# Supabase TLS pin
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unwritable_tls_pin_is_held_in_memory_and_verified(
        rofs_service, monkeypatch, caplog):
    from api import db_pg

    sent = _capture_alerts(monkeypatch)
    rofs_service("live")
    monkeypatch.setattr(config, "SUPABASE_PIN_FILE",
                        "/app/backend/.supabase_fp.txt")
    monkeypatch.setattr(config, "SUPABASE_CERT_FINGERPRINT", "")
    db_pg._reset_tls_pin_state_for_tests()
    fp = "ab" * 32

    monkeypatch.setattr(db_pg, "_fetch_cert_fingerprint",
                        lambda _h, _p: fp)

    async def _no_system_trust(*_a, **_kw):
        raise ssl.SSLCertVerificationError("self-signed pooler chain")

    monkeypatch.setattr(db_pg.asyncpg, "connect", _no_system_trust)

    with caplog.at_level(logging.ERROR):
        ctx = await db_pg._tls_context()

    assert ctx is not None
    assert db_pg._pin_path().exists() is False
    assert db_pg._MEMORY_PIN == fp
    assert any(r.levelno == logging.ERROR for r in caplog.records), \
        "unwritable pin file was not logged at ERROR"
    assert any(s[0] == alerts.AlertEvent.STATE_WRITE_FAILURE for s in sent)

    # A later pool refill (in-memory pin, no file) still verifies.
    ctx2 = await db_pg._tls_context()
    assert ctx2 is not None

    # A different certificate is a MITM: the in-memory pin must refuse.
    monkeypatch.setattr(db_pg, "_fetch_cert_fingerprint",
                        lambda _h, _p: "cd" * 32)
    with pytest.raises(RuntimeError, match="FINGERPRINT MISMATCH"):
        await db_pg._tls_context()


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_error", [OSError, RuntimeError])
async def test_ca_and_probe_failure_refuse_before_pool_authentication(
        monkeypatch, tmp_path, caplog, probe_error):
    from api import db_pg

    monkeypatch.setattr(config, "SUPABASE_PIN_FILE", str(tmp_path / "pin"))
    monkeypatch.setattr(config, "SUPABASE_CERT_FINGERPRINT", "")
    monkeypatch.setattr(config, "SUPABASE_DB_URL",
                        "postgresql://dummy_user:dummy_password@db.invalid/test")
    monkeypatch.setattr(db_pg, "_MEMORY_PIN", None)
    monkeypatch.setattr(db_pg, "_pool", None)
    calls = []

    async def strict_refusal(*args, **kwargs):
        calls.append("strict")
        context = kwargs["ssl"]
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        raise ssl.SSLCertVerificationError("untrusted")

    def probe_failure(*args):
        calls.append("probe")
        raise probe_error("dummy_password must not appear in errors")

    async def forbidden_pool(*args, **kwargs):
        calls.append("pool authentication")
        raise AssertionError("unverified pool creation")

    monkeypatch.setattr(db_pg.asyncpg, "connect", strict_refusal)
    monkeypatch.setattr(db_pg, "_fetch_cert_fingerprint", probe_failure)
    monkeypatch.setattr(db_pg.asyncpg, "create_pool", forbidden_pool)
    with pytest.raises(RuntimeError, match="refusing connection") as exc:
        await db_pg._get_pool()
    assert calls == ["strict", "probe"]
    assert db_pg._pool is None
    assert not (tmp_path / "pin").exists()
    assert "dummy_password" not in str(exc.value) + caplog.text
    assert exc.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_strict_ca_context_remains_authenticated(monkeypatch, tmp_path):
    from api import db_pg
    monkeypatch.setattr(config, "SUPABASE_PIN_FILE", str(tmp_path / "pin"))
    monkeypatch.setattr(config, "SUPABASE_CERT_FINGERPRINT", "")
    monkeypatch.setattr(db_pg, "_MEMORY_PIN", None)
    calls = []
    class Connection:
        async def close(self):
            calls.append("closed")
    async def trusted(*args, **kwargs):
        assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["ssl"].check_hostname is True
        return Connection()
    def no_probe(*args):
        raise AssertionError("CA authentication must not downgrade to TOFU")
    monkeypatch.setattr(db_pg.asyncpg, "connect", trusted)
    monkeypatch.setattr(db_pg, "_fetch_cert_fingerprint", no_probe)
    context = await db_pg._tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert calls == ["closed"]

@pytest.mark.asyncio
async def test_pool_refill_verifies_the_actual_connection(monkeypatch):
    import hashlib
    from types import SimpleNamespace
    from api import db_pg
    cert = b'unit-test-certificate'
    monkeypatch.setattr(db_pg, '_MEMORY_PIN', hashlib.sha256(cert).hexdigest())
    monkeypatch.setattr(config, 'SUPABASE_CERT_FINGERPRINT', '')
    ssl_object = SimpleNamespace(getpeercert=lambda binary_form: cert)
    conn = SimpleNamespace(_transport=SimpleNamespace(
        get_extra_info=lambda name: ssl_object))
    await db_pg._verify_pool_connection(conn)
    monkeypatch.setattr(db_pg, '_MEMORY_PIN', 'cd' * 32)
    with pytest.raises(RuntimeError, match='FINGERPRINT MISMATCH'):
        await db_pg._verify_pool_connection(conn)

@pytest.mark.asyncio
async def test_known_pin_cannot_fall_back_when_probe_fails(monkeypatch, tmp_path):
    from api import db_pg
    monkeypatch.setattr(config, 'SUPABASE_PIN_FILE', str(tmp_path / 'pin'))
    monkeypatch.setattr(config, 'SUPABASE_CERT_FINGERPRINT', 'ab' * 32)
    async def untrusted(*args, **kwargs):
        raise ssl.SSLCertVerificationError('untrusted')
    def unreachable(*args):
        raise OSError('unreachable')
    monkeypatch.setattr(db_pg.asyncpg, 'connect', untrusted)
    monkeypatch.setattr(db_pg, '_fetch_cert_fingerprint', unreachable)
    with pytest.raises(RuntimeError, match='pin'):
        await db_pg._tls_context()

@pytest.mark.asyncio
async def test_pool_installs_certificate_verifier(monkeypatch):
    from api import db_pg
    monkeypatch.setattr(db_pg, '_pool', None)
    captured = {}
    async def context():
        return ssl.create_default_context()
    async def create(*args, **kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr(db_pg, '_tls_context', context)
    monkeypatch.setattr(db_pg.asyncpg, 'create_pool', create)
    await db_pg._get_pool()
    assert captured['init'] is db_pg._verify_pool_connection


def test_tls_preflight_checks_each_service():
    from scripts import preflight_deploy as pf
    compose = {'services': {
        'api': {'environment': {'SUPABASE_PIN_FILE': '/app/data/pin'},
                'volumes': [{'target': '/app/data', 'read_only': False}]},
        'live': {'environment': {'SUPABASE_PIN_FILE': '/app/root/pin'},
                 'volumes': [{'target': '/app/state', 'read_only': False}]}}}
    assert pf.check_supabase_tls(compose, {'USE_SUPABASE_DB': '1'})[0].status == pf.WARN
    assert pf.check_supabase_tls(compose, {'USE_SUPABASE_DB': '1',
        'SUPABASE_CERT_FINGERPRINT': 'ab' * 32})[0].status == pf.PASS


@pytest.mark.asyncio
async def test_operator_pin_is_checked_before_database_auth(monkeypatch, tmp_path):
    from api import db_pg
    monkeypatch.setattr(config, 'SUPABASE_PIN_FILE', str(tmp_path / 'pin'))
    monkeypatch.setattr(config, 'SUPABASE_CERT_FINGERPRINT', 'ab' * 32)
    calls = []
    async def authenticate(*args, **kwargs):
        calls.append('database authentication')
        raise AssertionError('password sent before pin verification')
    monkeypatch.setattr(db_pg.asyncpg, 'connect', authenticate)
    monkeypatch.setattr(db_pg, '_fetch_cert_fingerprint', lambda *a: 'cd' * 32)
    with pytest.raises(RuntimeError, match='FINGERPRINT MISMATCH'):
        await db_pg._tls_context()
    assert calls == []


def test_pin_is_enforced_during_the_tls_handshake(tmp_path):
    """Memory BIOs exercise actual TLS without sockets or DB credentials."""
    import hashlib
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from api import db_pg
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'unit-test.invalid')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1).not_valid_before(now-timedelta(days=1))
        .not_valid_after(now+timedelta(days=1)).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path/'cert.pem', tmp_path/'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    def handshake(pin):
        ci, co, si, so = (ssl.MemoryBIO() for _ in range(4))
        client = db_pg._pinned_context(pin).wrap_bio(ci, co, server_hostname='unit-test.invalid')
        server = server_context.wrap_bio(si, so, server_side=True)
        done = set()
        for _ in range(20):
            for label, peer in [('client', client), ('server', server)]:
                if label not in done:
                    try:
                        peer.do_handshake()
                        done.add(label)
                    except ssl.SSLWantReadError:
                        pass
            si.write(co.read()); ci.write(so.read())
            if len(done) == 2:
                return
        pytest.fail('TLS handshake did not finish')
    handshake(hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest())
    with pytest.raises(ssl.SSLCertVerificationError, match='FINGERPRINT MISMATCH'):
        handshake('cd' * 32)


@pytest.mark.parametrize('reply', [b'S', b'N'])
def test_certificate_probe_negotiates_postgres_starttls(monkeypatch, reply):
    import hashlib
    import socket
    import struct
    from api import db_pg
    sent = []
    class Sock:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def sendall(self, data): sent.append(data)
        def recv(self, length): return reply
    class TLS(Sock):
        def getpeercert(self, binary_form): return b'test-server-certificate'
    class Context:
        def __init__(self, *args): pass
        def wrap_socket(self, sock, server_hostname):
            assert sent == [struct.pack('!II', 8, 80877103)]
            assert reply == b'S'
            return TLS()
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: Sock())
    monkeypatch.setattr(db_pg.ssl, 'SSLContext', Context)
    if reply == b'S':
        assert db_pg._fetch_cert_fingerprint('unit-test.invalid', 5432) == hashlib.sha256(b'test-server-certificate').hexdigest()
    else:
        with pytest.raises(ssl.SSLError, match='SSLRequest'):
            db_pg._fetch_cert_fingerprint('unit-test.invalid', 5432)


@pytest.mark.asyncio
@pytest.mark.parametrize("contents", ["", "not-a-fingerprint"])
async def test_invalid_persisted_pin_never_rebootstraps(monkeypatch, tmp_path, contents):
    from api import db_pg
    pin = tmp_path / "pin"
    pin.write_text(contents)
    monkeypatch.setattr(config, "SUPABASE_PIN_FILE", str(pin))
    monkeypatch.setattr(config, "SUPABASE_CERT_FINGERPRINT", "")
    monkeypatch.setattr(db_pg, "_MEMORY_PIN", None)
    async def forbidden_connect(*args, **kwargs):
        raise AssertionError("corrupt known pin must not authenticate")
    def forbidden_probe(*args):
        raise AssertionError("corrupt known pin must not rebootstrap")
    monkeypatch.setattr(db_pg.asyncpg, "connect", forbidden_connect)
    monkeypatch.setattr(db_pg, "_fetch_cert_fingerprint", forbidden_probe)
    with pytest.raises(RuntimeError, match="pin.*refusing connection"):
        await db_pg._tls_context()
    assert pin.read_text() == contents
