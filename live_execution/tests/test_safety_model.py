"""
tests for the live_execution safety model itself:
kill switch (manual + automatic daily-loss breaker), confirmation queue
(fail-closed expiry at every stage), execution ledger (idempotency /
exposure / realized P&L), wallet loading, and the hardcoded safety flags.

Everything here is offline and hermetic: tmp state dirs, injected clocks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import live_execution.config as le_config
from live_execution import kill_switch, wallet
from live_execution.confirmation_queue import (
    ConfirmationError,
    ConfirmationQueue,
)
from live_execution.models import ExecutionLedger


class Clock:
    """Injectable clock for deterministic expiry testing."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def state(tmp_path) -> Path:
    return tmp_path


# --- hardcoded safety flags (load-bearing repo defaults) ----------------------

def test_safety_flags_are_hardcoded_safe_defaults():
    assert le_config.LIVE_TRADING_ENABLED is False
    assert le_config.REQUIRE_MANUAL_CONFIRMATION is True


def test_safety_flags_are_not_env_readable():
    source = Path(le_config.__file__).read_text()
    for flag in ("LIVE_TRADING_ENABLED", "REQUIRE_MANUAL_CONFIRMATION"):
        line = next(l for l in source.splitlines()
                    if l.startswith(flag))
        assert "os.getenv" not in line, flag


def test_swap_url_is_the_verified_lite_api_value():
    # Verified live 2026-08-23 (422 deserialization error, not 404).
    assert le_config.JUPITER_SWAP_URL == \
        "https://lite-api.jup.ag/swap/v1/swap"


# --- kill switch ---------------------------------------------------------------

def test_kill_switch_defaults_clear_and_round_trips(state):
    assert kill_switch.is_tripped(state) is False
    kill_switch.trip("operator panic", state_dir=state)
    assert kill_switch.is_tripped(state) is True
    assert kill_switch.trip_reason(state) == "operator panic"
    kill_switch.clear(state_dir=state)
    assert kill_switch.is_tripped(state) is False


def test_assert_not_tripped_raises_with_reason(state):
    kill_switch.trip("bad day", state_dir=state)
    with pytest.raises(kill_switch.KillSwitchTripped, match="bad day"):
        kill_switch.assert_not_tripped(state)


def test_kill_switch_survives_recreation(state):
    kill_switch.trip("persistme", state_dir=state)
    # A brand-new process would read the same file:
    assert kill_switch.is_tripped(state) is True


def test_corrupt_kill_switch_state_fails_loudly(state):
    p = state / "kill_switch.json"
    p.write_text("{not json")
    with pytest.raises(RuntimeError, match="corrupt"):
        kill_switch.is_tripped(state)


def test_daily_breaker_trips_at_threshold_and_stays(state, tmp_path):
    clock = Clock()
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=clock)
    ledger.record_buy("b", "MINT", 50.0, 1.0, 50.0, "s")
    ledger.mark_close("MINT", proceeds_usd=-20.0)     # -70 > -75: no trip
    assert kill_switch.check_daily_loss_breaker(
        ledger, state_dir=state, now_fn=clock) is False
    assert kill_switch.is_tripped(state) is False

    ledger.record_buy("b2", "M2", 50.0, 1.0, 50.0, "s")
    ledger.mark_close("M2", proceeds_usd=-10.0)       # -80 <= -75: TRIP
    assert kill_switch.check_daily_loss_breaker(
        ledger, state_dir=state, now_fn=clock) is True
    assert "AUTO" in kill_switch.trip_reason(state)


def test_daily_breaker_ignores_yesterdays_losses(state, tmp_path):
    clock = Clock()
    ledger = ExecutionLedger(tmp_path / "exec.json", now_fn=clock)
    ledger.record_buy("b", "MINT", 50.0, 1.0, 50.0, "s")
    ledger.mark_close("MINT", proceeds_usd=-90.0)     # big loss...
    clock.advance(90_000)                              # ...but yesterday
    assert kill_switch.check_daily_loss_breaker(
        ledger, state_dir=state, now_fn=clock) is False


# --- confirmation queue: fail-closed expiry at EVERY stage ---------------------

def test_confirmation_happy_path(state):
    clock = Clock()
    q = ConfirmationQueue(state / "c.json", now_fn=clock, expiry_seconds=300)
    pc = q.propose("MINT", 6, 10.0, quote_snapshot={"p": 1})
    assert pc.status == "pending"
    q.approve(pc.id)
    done = q.consume(pc.id)
    assert done.status == "consumed"


def test_expired_proposal_cannot_be_approved(state):
    clock = Clock()
    q = ConfirmationQueue(state / "c.json", now_fn=clock, expiry_seconds=100)
    pc = q.propose("MINT", 6, 10.0)
    clock.advance(101)
    with pytest.raises(ConfirmationError, match="EXPIRED"):
        q.approve(pc.id)
    stored = json.loads((state / "c.json").read_text())
    assert stored["confirmations"][pc.id]["status"] == "expired"


def test_approval_consumed_after_expiry_refuses_fail_closed(state):
    """Even an APPROVED confirmation dies at its window — checked at use."""
    clock = Clock()
    q = ConfirmationQueue(state / "c.json", now_fn=clock, expiry_seconds=100)
    pc = q.propose("MINT", 6, 10.0)
    q.approve(pc.id)                       # inside the window...
    clock.advance(500)                     # ...but consumed too late
    with pytest.raises(ConfirmationError, match="expired"):
        q.consume(pc.id)


def test_double_consume_refuses(state):
    q = ConfirmationQueue(state / "c.json")
    pc = q.propose("MINT", 6, 10.0)
    q.approve(pc.id)
    q.consume(pc.id)
    with pytest.raises(ConfirmationError, match="not consumable"):
        q.consume(pc.id)


def test_unknown_and_denied_ids_refuse(state):
    q = ConfirmationQueue(state / "c.json")
    with pytest.raises(ConfirmationError, match="unknown"):
        q.consume("ghost")
    pc = q.propose("MINT", 6, 10.0)
    q.deny(pc.id)
    with pytest.raises(ConfirmationError, match="denied"):
        q.consume(pc.id)


def test_list_active_expires_due_items(state):
    clock = Clock()
    q = ConfirmationQueue(state / "c.json", now_fn=clock, expiry_seconds=50)
    old = q.propose("OLD", 6, 5.0)
    clock.advance(51)
    fresh = q.propose("KEEP", 6, 5.0)     # proposed after the advance
    active = q.list_active()
    assert [p.id for p in active] == [fresh.id]
    stored = json.loads((state / "c.json").read_text())
    assert stored["confirmations"][old.id]["status"] == "expired"


# --- execution ledger ------------------------------------------------------------

def test_ledger_idempotency_lookup(state):
    led = ExecutionLedger(state / "e.json")
    assert led.get_by_idempotency_key("k") is None
    led.record_buy("k", "MINT", 10.0, 2.0, 5.0, "sigX")
    rec = led.get_by_idempotency_key("k")
    assert rec is not None
    assert rec.tokens_out == 2.0 and rec.signature == "sigX"


def test_ledger_close_realizes_pnl_fifo_and_frees_exposure(state):
    led = ExecutionLedger(state / "e.json")
    led.record_buy("k1", "MINT", 40.0, 4.0, 10.0, "s1")
    led.record_buy("k2", "MINT", 20.0, 1.0, 20.0, "s2")
    assert led.total_open_exposure() == pytest.approx(60.0)

    close = led.mark_close("MINT", proceeds_usd=55.0)   # closes k1 ($40 cost)
    assert close.pnl_usd == pytest.approx(15.0)
    # Only the second buy stays open:
    assert led.total_open_exposure() == pytest.approx(20.0)
    with pytest.raises(ValueError, match="no open position"):
        led.mark_close("OTHER", proceeds_usd=1.0)


def test_ledger_corrupt_file_raises_never_looks_empty(state):
    p = state / "e.json"
    p.write_text("garbage{")
    with pytest.raises(RuntimeError, match="corrupt"):
        ExecutionLedger(p).open_positions()


# --- wallet ------------------------------------------------------------------------

def test_wallet_missing_path_refuses(state):
    with pytest.raises(wallet.WalletError, match="missing"):
        wallet.load_keypair(str(state / "absent.json"))


def test_wallet_garbage_json_refuses(state):
    p = state / "kp.json"
    p.write_text("{not a byte array")
    with pytest.raises(wallet.WalletError, match="unreadable"):
        wallet.load_keypair(str(p))


def test_wallet_non_byte_array_refuses(state):
    p = state / "kp.json"
    p.write_text('{"hello": true}')
    with pytest.raises(wallet.WalletError, match="byte array"):
        wallet.load_keypair(str(p))


def test_wallet_wrong_length_refuses(state):
    p = state / "kp.json"
    p.write_text(json.dumps([1, 2, 3]))          # valid u8s, wrong length
    with pytest.raises(wallet.WalletError, match="64"):
        wallet.load_keypair(str(p))


def test_wallet_loads_real_keypair_file(state):
    """Regression (2026-08-28): the success path through solders. A previous
    revision passed the file PATH to solders' from_json (which expects JSON
    CONTENT), so every real keypair load fail-closed with "expected value at
    line 1 column 1". The old tests only exercised refusal paths, which is
    why this loads a REAL generated keypair end-to-end."""
    from solders.keypair import Keypair

    kp = Keypair()
    p = state / "kp.json"
    p.write_text(json.dumps(list(bytes(kp))))
    loaded = wallet.load_keypair(str(p))
    assert wallet.pubkey_string(loaded) == str(kp.pubkey())


def test_wallet_verify_expected_address_mismatch_refuses(state):
    from solders.keypair import Keypair

    kp = Keypair()
    p = state / "kp.json"
    p.write_text(json.dumps(list(bytes(kp))))
    loaded = wallet.load_keypair(str(p))
    wrong = str(Keypair().pubkey())              # some OTHER account
    import live_execution.config as cfg
    original = cfg.EXPECTED_WALLET_ADDRESS
    cfg.EXPECTED_WALLET_ADDRESS = wrong
    try:
        with pytest.raises(wallet.WalletError, match="not the expected wallet"):
            wallet.verify_expected_address(loaded)
    finally:
        cfg.EXPECTED_WALLET_ADDRESS = original


def test_wallet_verify_expected_address_match_passes(state):
    from solders.keypair import Keypair

    kp = Keypair()
    p = state / "kp.json"
    p.write_text(json.dumps(list(bytes(kp))))
    loaded = wallet.load_keypair(str(p))
    import live_execution.config as cfg
    original = cfg.EXPECTED_WALLET_ADDRESS
    cfg.EXPECTED_WALLET_ADDRESS = str(kp.pubkey())
    try:
        assert wallet.verify_expected_address(loaded) == str(kp.pubkey())
    finally:
        cfg.EXPECTED_WALLET_ADDRESS = original


def test_wallet_config_default_missing_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(le_config, "WALLET_KEYPAIR_PATH", "")
    with pytest.raises(wallet.WalletError, match="WALLET_KEYPAIR_PATH"):
        wallet.load_keypair()