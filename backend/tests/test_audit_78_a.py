"""
backend/tests/test_audit_78_a.py — tests for Master Prompt §78 Phase A:
Signing guard bypass reproduction, owner checks, wallet ownership proofs,
and CreateAccount/outflow caps.
"""
import struct
import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from live_execution import config
from live_execution.jupiter_executor import ExecutionError, inspect_swap_transaction

SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ATA_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
COMPUTE_BUDGET_ID = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
JUPITER_V6_ID = Pubkey.from_string("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")


def _make_tx(instructions: list[Instruction], payer: Keypair, signers: list[Keypair] | None = None) -> VersionedTransaction:
    msg = Message.new_with_blockhash(instructions, payer.pubkey(), Hash.default())
    all_signers = [payer] if signers is None else signers
    return VersionedTransaction(msg, all_signers)


# ---------------------------------------------------------------------------
# REPRODUCTIONS OF §78 BYPASSES (A1, A2, A3)
# ---------------------------------------------------------------------------

def test_reproduce_bypass_a1_drain_refuses():
    """
    A1 (drain):
    System.CreateAccount(payer -> N, 165 bytes, owner=Token)
    Token.InitializeAccount3(N, owner=ATTACKER)
    System.Transfer(payer -> N, 5 SOL)
    Token.SyncNative(N)
    Must be refused (owner check on InitializeAccount3, unproven wallet ownership, and outflow cap).
    """
    payer = Keypair()
    n_account = Keypair()
    attacker = Keypair().pubkey()

    # 1. System.CreateAccount(payer -> N, 165 bytes, owner = Token program, 2_000_000 lamports)
    create_data = struct.pack("<IQQ", 0, 2_000_000, 165) + bytes(TOKEN_PROGRAM_ID)
    ix_create = Instruction(
        SYSTEM_PROGRAM_ID,
        create_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), True, True)],
    )

    # 2. Token.InitializeAccount3(N, owner = ATTACKER) (tag 18)
    init3_data = bytes([18]) + bytes(attacker)
    ix_init = Instruction(
        TOKEN_PROGRAM_ID,
        init3_data,
        [AccountMeta(n_account.pubkey(), False, True), AccountMeta(WSOL_MINT, False, False)],
    )

    # 3. System.Transfer(payer -> N, 5 SOL)
    transfer_data = struct.pack("<IQ", 2, 5_000_000_000)
    ix_transfer = Instruction(
        SYSTEM_PROGRAM_ID,
        transfer_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), False, True)],
    )

    # 4. Token.SyncNative(N) (tag 17)
    sync_data = bytes([17])
    ix_sync = Instruction(
        TOKEN_PROGRAM_ID,
        sync_data,
        [AccountMeta(n_account.pubkey(), False, True)],
    )

    tx = _make_tx([ix_create, ix_init, ix_transfer, ix_sync], payer, [payer, n_account])

    with pytest.raises(ExecutionError) as exc_info:
        inspect_swap_transaction(tx, str(payer.pubkey()))
    err_msg = str(exc_info.value).lower()
    assert any(k in err_msg for k in ["owner", "outflow", "unapproved", "match", "foreign", "wallet"])


def test_reproduce_bypass_a2_sink_grief_refuses():
    """
    A2 (sink/grief):
    System.CreateAccount(payer -> N, 50 SOL, space 0, owner = Jupiter v6)
    Must be refused because:
    1. Owner program is Jupiter v6 (must be Token or Token-2022 only).
    2. Lamports (50 SOL) exceeds MAX_CREATE_ACCOUNT_LAMPORTS / MAX_SYSTEM_OUTFLOW_LAMPORTS.
    """
    payer = Keypair()
    n_account = Keypair()

    create_data = struct.pack("<IQQ", 0, 50_000_000_000, 0) + bytes(JUPITER_V6_ID)
    ix_create = Instruction(
        SYSTEM_PROGRAM_ID,
        create_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), True, True)],
    )
    tx = _make_tx([ix_create], payer, [payer, n_account])

    with pytest.raises(ExecutionError) as exc_info:
        inspect_swap_transaction(tx, str(payer.pubkey()))
    err_msg = str(exc_info.value).lower()
    assert any(k in err_msg for k in ["owner", "forbidden", "unapproved", "outflow", "lamports", "cap"])


def test_reproduce_bypass_a3_init_account_attacker_owner_refuses():
    """
    A3:
    Token.InitializeAccount (tag 1) with accounts[2] = attacker must be refused.
    """
    payer = Keypair()
    n_account = Keypair().pubkey()
    attacker = Keypair().pubkey()

    # Tag 1: accounts = [account, mint, owner]
    init_data = bytes([1])
    ix_init = Instruction(
        TOKEN_PROGRAM_ID,
        init_data,
        [
            AccountMeta(n_account, False, True),
            AccountMeta(WSOL_MINT, False, False),
            AccountMeta(attacker, False, False),
        ],
    )
    tx = _make_tx([ix_init], payer)

    with pytest.raises(ExecutionError) as exc_info:
        inspect_swap_transaction(tx, str(payer.pubkey()))
    err_msg = str(exc_info.value).lower()
    assert "owner" in err_msg and ("match" in err_msg or "payer" in err_msg or "refused" in err_msg)


# ---------------------------------------------------------------------------
# OWNER CHECKS & TOKEN-2022
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("program_id", [TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID])
def test_init_owner_equals_payer_passes(program_id):
    """InitializeAccount (tag 1), InitializeAccount2 (tag 16), InitializeAccount3 (tag 18) pass when owner == payer."""
    payer = Keypair()
    n1 = Keypair().pubkey()
    n2 = Keypair().pubkey()
    n3 = Keypair().pubkey()

    # Tag 1
    ix1 = Instruction(
        program_id,
        bytes([1]),
        [AccountMeta(n1, False, True), AccountMeta(WSOL_MINT, False, False), AccountMeta(payer.pubkey(), False, False)],
    )
    # Tag 16 (InitializeAccount2: [16] + owner[32])
    ix2 = Instruction(
        program_id,
        bytes([16]) + bytes(payer.pubkey()),
        [AccountMeta(n2, False, True), AccountMeta(WSOL_MINT, False, False)],
    )
    # Tag 18 (InitializeAccount3: [18] + owner[32])
    ix3 = Instruction(
        program_id,
        bytes([18]) + bytes(payer.pubkey()),
        [AccountMeta(n3, False, True), AccountMeta(WSOL_MINT, False, False)],
    )

    tx = _make_tx([ix1, ix2, ix3], payer)
    inspect_swap_transaction(tx, str(payer.pubkey()))


@pytest.mark.parametrize("program_id", [TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID])
@pytest.mark.parametrize("tag", [16, 18])
def test_init_account2_and_3_attacker_owner_refused(program_id, tag):
    """InitializeAccount2 and InitializeAccount3 with attacker owner in data must be refused."""
    payer = Keypair()
    n_account = Keypair().pubkey()
    attacker = Keypair().pubkey()

    ix = Instruction(
        program_id,
        bytes([tag]) + bytes(attacker),
        [AccountMeta(n_account, False, True), AccountMeta(WSOL_MINT, False, False)],
    )
    tx = _make_tx([ix], payer)
    with pytest.raises(ExecutionError, match="owner.*match|refuse"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


# ---------------------------------------------------------------------------
# CREATE ACCOUNT RESTRICTIONS & CAPS
# ---------------------------------------------------------------------------

def test_create_account_over_cap_refused():
    """CreateAccount with lamports exceeding MAX_CREATE_ACCOUNT_LAMPORTS must be refused."""
    payer = Keypair()
    n_account = Keypair()
    lamports = config.MAX_CREATE_ACCOUNT_LAMPORTS + 1

    create_data = struct.pack("<IQQ", 0, lamports, 165) + bytes(TOKEN_PROGRAM_ID)
    ix = Instruction(
        SYSTEM_PROGRAM_ID,
        create_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), True, True)],
    )
    tx = _make_tx([ix], payer, [payer, n_account])
    with pytest.raises(ExecutionError, match="MAX_CREATE_ACCOUNT_LAMPORTS|lamports.*exceeds"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


def test_create_account_owned_by_jupiter_refused():
    """CreateAccount owned by any DEX (even allowlisted Jupiter) must be refused."""
    payer = Keypair()
    n_account = Keypair()

    create_data = struct.pack("<IQQ", 0, 2_000_000, 165) + bytes(JUPITER_V6_ID)
    ix = Instruction(
        SYSTEM_PROGRAM_ID,
        create_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), True, True)],
    )
    tx = _make_tx([ix], payer, [payer, n_account])
    with pytest.raises(ExecutionError, match="forbidden owner program|Token or Token-2022"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


def test_create_account_space_over_165_refused():
    """CreateAccount with space > 165 (e.g. 200) must be refused."""
    payer = Keypair()
    n_account = Keypair()

    create_data = struct.pack("<IQQ", 0, 2_000_000, 200) + bytes(TOKEN_PROGRAM_ID)
    ix = Instruction(
        SYSTEM_PROGRAM_ID,
        create_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), True, True)],
    )
    tx = _make_tx([ix], payer, [payer, n_account])
    with pytest.raises(ExecutionError, match="space.*165"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


# ---------------------------------------------------------------------------
# OUTFLOW CAP & ORDERING CHECKS
# ---------------------------------------------------------------------------

def test_outflow_cap_exceeded_across_several_transfers_refused():
    """Sum of top-level outflows exceeding MAX_SYSTEM_OUTFLOW_LAMPORTS must be refused."""
    payer = Keypair()
    # 3 transfers of 4_000_000 = 12_000_000 > 10_000_000
    transfer_data = struct.pack("<IQ", 2, 4_000_000)
    ix1 = Instruction(
        SYSTEM_PROGRAM_ID,
        transfer_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(payer.pubkey(), False, True)],
    )
    ix2 = Instruction(
        SYSTEM_PROGRAM_ID,
        transfer_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(payer.pubkey(), False, True)],
    )
    ix3 = Instruction(
        SYSTEM_PROGRAM_ID,
        transfer_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(payer.pubkey(), False, True)],
    )
    tx = _make_tx([ix1, ix2, ix3], payer)
    with pytest.raises(ExecutionError, match="MAX_SYSTEM_OUTFLOW_LAMPORTS|outflow.*exceeds"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


def test_transfer_to_uninitialized_created_account_refused():
    """Transfer to an account that was created in the tx but never initialized must be refused."""
    payer = Keypair()
    n_account = Keypair()

    create_data = struct.pack("<IQQ", 0, 2_000_000, 165) + bytes(TOKEN_PROGRAM_ID)
    ix_create = Instruction(
        SYSTEM_PROGRAM_ID,
        create_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), True, True)],
    )
    transfer_data = struct.pack("<IQ", 2, 100_000)
    ix_transfer = Instruction(
        SYSTEM_PROGRAM_ID,
        transfer_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), False, True)],
    )
    tx = _make_tx([ix_create, ix_transfer], payer, [payer, n_account])
    with pytest.raises(ExecutionError, match="foreign|unproven|not owned"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


def test_transfer_before_initialize_refused():
    """Transfer to a created account BEFORE its Initialize instruction must be refused (order matters)."""
    payer = Keypair()
    n_account = Keypair()

    create_data = struct.pack("<IQQ", 0, 2_000_000, 165) + bytes(TOKEN_PROGRAM_ID)
    ix_create = Instruction(
        SYSTEM_PROGRAM_ID,
        create_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), True, True)],
    )
    # Transfer before initialize
    transfer_data = struct.pack("<IQ", 2, 100_000)
    ix_transfer = Instruction(
        SYSTEM_PROGRAM_ID,
        transfer_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), False, True)],
    )
    # Initialize comes after
    init_data = bytes([1])
    ix_init = Instruction(
        TOKEN_PROGRAM_ID,
        init_data,
        [AccountMeta(n_account.pubkey(), False, True), AccountMeta(WSOL_MINT, False, False), AccountMeta(payer.pubkey(), False, False)],
    )
    tx = _make_tx([ix_create, ix_transfer, ix_init], payer, [payer, n_account])
    with pytest.raises(ExecutionError, match="foreign|unproven|not owned"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


# ---------------------------------------------------------------------------
# CONTROLS: LEGITIMATE WRAP/UNWRAP & ATA ROUTES STILL PASS
# ---------------------------------------------------------------------------

def test_legit_wrap_unwrap_control_passes():
    """Legit wrap and unwrap pattern (CreateAccount -> Initialize -> Transfer -> SyncNative -> CloseAccount) passes."""
    payer = Keypair()
    n_account = Keypair()

    # 1. CreateAccount (165 bytes, owner=Token, 2_000_000 lamports)
    create_data = struct.pack("<IQQ", 0, 2_000_000, 165) + bytes(TOKEN_PROGRAM_ID)
    ix_create = Instruction(
        SYSTEM_PROGRAM_ID,
        create_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), True, True)],
    )
    # 2. InitializeAccount (tag 1, owner=payer) -> proves wallet-owned
    init_data = bytes([1])
    ix_init = Instruction(
        TOKEN_PROGRAM_ID,
        init_data,
        [AccountMeta(n_account.pubkey(), False, True), AccountMeta(WSOL_MINT, False, False), AccountMeta(payer.pubkey(), False, False)],
    )
    # 3. Transfer (500_000 lamports) -> to proven wallet-owned account, total outflow 2.5m <= 10m
    transfer_data = struct.pack("<IQ", 2, 500_000)
    ix_transfer = Instruction(
        SYSTEM_PROGRAM_ID,
        transfer_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), False, True)],
    )
    # 4. SyncNative (tag 17)
    ix_sync = Instruction(TOKEN_PROGRAM_ID, bytes([17]), [AccountMeta(n_account.pubkey(), False, True)])
    # 5. CloseAccount (tag 9, destination=payer, owner=payer)
    ix_close = Instruction(
        TOKEN_PROGRAM_ID,
        bytes([9]),
        [AccountMeta(n_account.pubkey(), False, True), AccountMeta(payer.pubkey(), False, True), AccountMeta(payer.pubkey(), True, False)],
    )

    tx = _make_tx([ix_create, ix_init, ix_transfer, ix_sync, ix_close], payer, [payer, n_account])
    inspect_swap_transaction(tx, str(payer.pubkey()))


def test_legit_ata_create_idempotent_route_close_to_wallet_passes():
    """Legit ATA-create-idempotent + route + close-to-wallet control still passes."""
    payer = Keypair()
    mint = Keypair().pubkey()
    ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ATA_PROGRAM_ID,
    )

    ix_compute = Instruction(COMPUTE_BUDGET_ID, bytes([2, 0, 0, 0, 100, 0, 0, 0]), [])
    ix_ata = Instruction(
        ATA_PROGRAM_ID,
        bytes([1]),
        [
            AccountMeta(payer.pubkey(), True, True),
            AccountMeta(ata, False, True),
            AccountMeta(payer.pubkey(), False, False),
            AccountMeta(mint, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(TOKEN_PROGRAM_ID, False, False),
        ],
    )
    ix_jup = Instruction(
        JUPITER_V6_ID,
        b"\xe5\x17\x22\x00\x01\x02\x03",
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(ata, False, True)],
    )
    ix_close = Instruction(
        TOKEN_PROGRAM_ID,
        bytes([9]),
        [AccountMeta(ata, False, True), AccountMeta(payer.pubkey(), False, True), AccountMeta(payer.pubkey(), True, False)],
    )

    tx = _make_tx([ix_compute, ix_ata, ix_jup, ix_close], payer)
    inspect_swap_transaction(tx, str(payer.pubkey()))


# ---------------------------------------------------------------------------
# TRUNCATED DATA CHECKS
# ---------------------------------------------------------------------------

def test_truncated_data_system_create_refused():
    """System CreateAccount with truncated data (< 52 bytes) must be refused."""
    payer = Keypair()
    n_account = Keypair()
    ix = Instruction(
        SYSTEM_PROGRAM_ID,
        b"\x00" * 30,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), True, True)],
    )
    tx = _make_tx([ix], payer, [payer, n_account])
    with pytest.raises(ExecutionError, match="truncated"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


def test_truncated_data_system_transfer_refused():
    """System Transfer with truncated data (< 12 bytes) must be refused."""
    payer = Keypair()
    ix = Instruction(
        SYSTEM_PROGRAM_ID,
        struct.pack("<I", 2) + b"\x00" * 4,  # 8 bytes < 12
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(payer.pubkey(), False, True)],
    )
    tx = _make_tx([ix], payer)
    with pytest.raises(ExecutionError, match="truncated"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


def test_truncated_data_init_account_accounts_refused():
    """Token InitializeAccount (tag 1) with < 3 accounts must be refused."""
    payer = Keypair()
    n_account = Keypair().pubkey()
    ix = Instruction(
        TOKEN_PROGRAM_ID,
        bytes([1]),
        [AccountMeta(n_account, False, True), AccountMeta(WSOL_MINT, False, False)],
    )
    tx = _make_tx([ix], payer)
    with pytest.raises(ExecutionError, match="[Tt]runcated|missing.*account"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


@pytest.mark.parametrize("tag", [16, 18])
def test_truncated_data_init_account2_and_3_refused(tag):
    """Token InitializeAccount2 and InitializeAccount3 with data < 33 bytes must be refused."""
    payer = Keypair()
    n_account = Keypair().pubkey()
    ix = Instruction(
        TOKEN_PROGRAM_ID,
        bytes([tag]) + b"\x01" * 10,  # 11 bytes < 33
        [AccountMeta(n_account, False, True), AccountMeta(WSOL_MINT, False, False)],
    )
    tx = _make_tx([ix], payer)
    with pytest.raises(ExecutionError, match="truncated"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


# ---------------------------------------------------------------------------
# MUTATION CHECKS (RULE 2)
# ---------------------------------------------------------------------------

def test_mutation_check_owner_verification():
    """
    Mutation check proving that if tag 18 owner check is bypassed (e.g. attacker owner accepted),
    the test fails because attacker is recognized as non-payer.
    """
    payer = Keypair()
    n_account = Keypair().pubkey()
    attacker = Keypair().pubkey()

    # Tag 18 with attacker
    ix = Instruction(
        TOKEN_PROGRAM_ID,
        bytes([18]) + bytes(attacker),
        [AccountMeta(n_account, False, True), AccountMeta(WSOL_MINT, False, False)],
    )
    tx = _make_tx([ix], payer)
    with pytest.raises(ExecutionError):
        inspect_swap_transaction(tx, str(payer.pubkey()))


def test_mutation_check_create_account_owner_verification():
    """
    Mutation check proving that if CreateAccount accepts DEX program owner,
    it must be refused.
    """
    payer = Keypair()
    n_account = Keypair()
    create_data = struct.pack("<IQQ", 0, 1_000_000, 165) + bytes(JUPITER_V6_ID)
    ix = Instruction(
        SYSTEM_PROGRAM_ID,
        create_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(n_account.pubkey(), True, True)],
    )
    tx = _make_tx([ix], payer, [payer, n_account])
    with pytest.raises(ExecutionError):
        inspect_swap_transaction(tx, str(payer.pubkey()))

