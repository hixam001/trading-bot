"""
backend/tests/test_audit_77_c.py — tests for Master Prompt §77 Phase C:
Signing guard (instruction-level deny-by-default, allowlist hygiene).
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


def _make_tx(instructions: list[Instruction], payer: Keypair) -> VersionedTransaction:
    msg = Message.new_with_blockhash(instructions, payer.pubkey(), Hash.default())
    return VersionedTransaction(msg, [payer])


def test_system_transfer_to_foreign_pubkey_refuses():
    """System Program transfer to a third-party account must be refused."""
    payer = Keypair()
    foreign = Keypair().pubkey()
    transfer_data = struct.pack("<IQ", 2, 1_000_000)
    ix = Instruction(
        SYSTEM_PROGRAM_ID,
        transfer_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(foreign, False, True)],
    )
    tx = _make_tx([ix], payer)
    with pytest.raises(ExecutionError, match="[Ss]ystem.*[Tt]ransfer|foreign|destination"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


def test_system_transfer_to_own_wallet_or_ata_passes():
    """System Program transfer to own wallet or own derived wSOL ATA must be allowed."""
    payer = Keypair()
    # 1. Transfer to own wallet
    transfer_data = struct.pack("<IQ", 2, 1_000_000)
    ix_self = Instruction(
        SYSTEM_PROGRAM_ID,
        transfer_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(payer.pubkey(), False, True)],
    )
    tx_self = _make_tx([ix_self], payer)
    inspect_swap_transaction(tx_self, str(payer.pubkey()))

    # 2. Transfer to own wSOL ATA
    wsol_ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN_PROGRAM_ID), bytes(WSOL_MINT)],
        ATA_PROGRAM_ID,
    )
    ix_ata = Instruction(
        SYSTEM_PROGRAM_ID,
        transfer_data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(wsol_ata, False, True), AccountMeta(WSOL_MINT, False, False)],
    )
    tx_ata = _make_tx([ix_ata], payer)
    inspect_swap_transaction(tx_ata, str(payer.pubkey()))


def test_spl_transfer_at_top_level_refuses():
    """SPL Token / Token-2022 Transfer (3) and TransferChecked (12) must be refused at top level."""
    payer = Keypair()
    foreign = Keypair().pubkey()
    mint = Keypair().pubkey()

    # Transfer (tag 3)
    data_transfer = struct.pack("<BQ", 3, 500)
    ix_spl = Instruction(
        TOKEN_PROGRAM_ID,
        data_transfer,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(foreign, False, True), AccountMeta(payer.pubkey(), True, False)],
    )
    tx_spl = _make_tx([ix_spl], payer)
    with pytest.raises(ExecutionError, match="[Tt]oken.*[Tt]ransfer"):
        inspect_swap_transaction(tx_spl, str(payer.pubkey()))

    # TransferChecked (tag 12)
    data_checked = struct.pack("<BQB", 12, 500, 6)
    ix_spl2 = Instruction(
        TOKEN_2022_PROGRAM_ID,
        data_checked,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(mint, False, False), AccountMeta(foreign, False, True), AccountMeta(payer.pubkey(), True, False)],
    )
    tx_spl2 = _make_tx([ix_spl2], payer)
    with pytest.raises(ExecutionError, match="[Tt]oken.*[Tt]ransfer"):
        inspect_swap_transaction(tx_spl2, str(payer.pubkey()))


def test_spl_approve_at_top_level_refuses():
    """SPL Token Approve (4) and ApproveChecked (13) must be refused at top level."""
    payer = Keypair()
    delegate = Keypair().pubkey()

    data_approve = struct.pack("<BQ", 4, 1_000_000)
    ix = Instruction(
        TOKEN_PROGRAM_ID,
        data_approve,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(delegate, False, False), AccountMeta(payer.pubkey(), True, False)],
    )
    tx = _make_tx([ix], payer)
    with pytest.raises(ExecutionError, match="[Aa]pprove"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


def test_spl_set_authority_at_top_level_refuses():
    """SPL Token SetAuthority (6) must be refused at top level."""
    payer = Keypair()
    new_auth = Keypair().pubkey()

    # Tag 6 = SetAuthority, followed by authority_type (u8) + Option<Pubkey>
    data = bytes([6, 0, 1]) + bytes(new_auth)
    ix = Instruction(
        TOKEN_PROGRAM_ID,
        data,
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(payer.pubkey(), True, False)],
    )
    tx = _make_tx([ix], payer)
    with pytest.raises(ExecutionError, match="[Ss]etAuthority"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


def test_spl_close_account_to_foreign_pubkey_refuses():
    """SPL Token CloseAccount (9) where destination is NOT payer must be refused."""
    payer = Keypair()
    account_to_close = Keypair().pubkey()
    foreign = Keypair().pubkey()

    # CloseAccount (tag 9)
    # accounts: [account_to_close, destination, owner]
    data = bytes([9])
    ix = Instruction(
        TOKEN_PROGRAM_ID,
        data,
        [AccountMeta(account_to_close, False, True), AccountMeta(foreign, False, True), AccountMeta(payer.pubkey(), True, False)],
    )
    tx = _make_tx([ix], payer)
    with pytest.raises(ExecutionError, match="[Cc]loseAccount.*destination"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


def test_spl_close_account_to_own_wallet_passes():
    """SPL Token CloseAccount (9) where destination is payer must be allowed."""
    payer = Keypair()
    account_to_close = Keypair().pubkey()

    data = bytes([9])
    ix = Instruction(
        TOKEN_PROGRAM_ID,
        data,
        [AccountMeta(account_to_close, False, True), AccountMeta(payer.pubkey(), False, True), AccountMeta(payer.pubkey(), True, False)],
    )
    tx = _make_tx([ix], payer)
    inspect_swap_transaction(tx, str(payer.pubkey()))


def test_unknown_system_discriminant_refuses():
    """System Program instruction with unknown discriminant must be refused."""
    payer = Keypair()
    data = struct.pack("<IQ", 99, 0)
    ix = Instruction(
        SYSTEM_PROGRAM_ID,
        data,
        [AccountMeta(payer.pubkey(), True, True)],
    )
    tx = _make_tx([ix], payer)
    with pytest.raises(ExecutionError, match="[Ss]ystem.*discriminant"):
        inspect_swap_transaction(tx, str(payer.pubkey()))


def test_valid_jupiter_swap_with_compute_budget_ata_create_and_jupiter_passes():
    """Valid Jupiter swap transaction with ComputeBudget, ATA CreateIdempotent, and Jupiter route."""
    payer = Keypair()
    mint = Keypair().pubkey()
    ata, _ = Pubkey.find_program_address(
        [bytes(payer.pubkey()), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ATA_PROGRAM_ID,
    )

    # 1. Compute budget
    ix_compute = Instruction(COMPUTE_BUDGET_ID, bytes([2, 0, 0, 0, 100, 0, 0, 0]), [])
    # 2. ATA CreateIdempotent (tag 1)
    # accounts: [funding, ata, wallet, mint, system, token]
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
    # 3. Jupiter route
    ix_jup = Instruction(
        JUPITER_V6_ID,
        b"\xe5\x17\x22\x00\x01\x02\x03",
        [AccountMeta(payer.pubkey(), True, True), AccountMeta(ata, False, True)],
    )
    tx = _make_tx([ix_compute, ix_ata, ix_jup], payer)
    inspect_swap_transaction(tx, str(payer.pubkey()))


def test_allowlist_hygiene_and_moonshot_removed():
    """Every entry in APPROVED_SWAP_PROGRAM_IDS must parse as a Pubkey and Moonshot must be removed."""
    assert "MSPC8t9vL7iQc4JzJjS9KqR7vC8bZpUuJ9G7r5vB2pD" not in config.APPROVED_SWAP_PROGRAM_IDS
    for pid in config.APPROVED_SWAP_PROGRAM_IDS:
        Pubkey.from_string(pid)
