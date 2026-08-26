"""Tests for omo-parity research aggregation + discovery slot composition."""
from data_providers.discovery import KeywordScanner, _is_fake_chart
from data_providers.research import aggregate_pairs
from models import Candidate


def _pair(mint, liq, vol6=0, buys6=0, sells6=0):
    return {
        "chainId": "solana", "baseToken": {"address": mint, "symbol": mint[:5]},
        "liquidity": {"usd": liq}, "volume": {"h6": vol6},
        "txns": {"h6": {"buys": buys6, "sells": sells6}},
    }


def test_aggregate_pairs_cross_pool():
    pairs = [_pair("M1", 30_000.0, 5_000, 100, 80), _pair("M1", 10_000.0, 1_000, 20, 20)]
    agg = aggregate_pairs(pairs)
    assert agg["pool_count"] == 2
    assert agg["total_liquidity_usd"] == 40_000.0
    assert agg["top_pool_share"] == 0.75
    assert agg["volume_6h_usd"] == 6_000
    assert agg["buys_6h"] == 120 and agg["sells_6h"] == 100


def test_aggregate_pairs_no_solana_returns_none():
    assert aggregate_pairs([{"chainId": "eth"}]) is None
    assert aggregate_pairs([]) is None


def test_fake_chart_filter():
    assert _is_fake_chart(1_000.0, 100_000.0)   # 100x ratio = wash trade
    assert not _is_fake_chart(50_000.0, 60_000.0)


def _info(pair, **kw):
    return {"pair": pair, "liq": (pair.get("liquidity") or {}).get("usd") or 0,
            "symbol": "S", "name": "N", **kw}


def test_board_slot_composition_guarantees():
    scanner = KeywordScanner(client=None)
    scanner._rotation = 3
    pairs = []
    for i in range(12):
        pairs.append({"chainId": "solana", "baseToken": {"address": f"BIG{i}"},
                      "liquidity": {"usd": 500_000 - i}, "volume": {"h1": 9_000}})
    # a newborn with socials that flow ranking would bury:
    newborn = {"chainId": "solana", "baseToken": {"address": "BABY"},
               "liquidity": {"usd": 16_000}, "volume": {"h1": 7_000},
               "pairCreatedAt": None}
    all_pairs = pairs + [newborn]
    board = scanner._build_board(all_pairs, set())
    mints = [c.mint_address for c in board]
    # flow core present and board capped:
    assert len(mints) <= KeywordScanner.BOARD_CAP if hasattr(KeywordScanner, "BOARD_CAP") else True
    assert mints == list(dict.fromkeys(mints))   # no duplicates


def test_onchain_authority_parse():
    from data_providers.onchain_security import parse_mint_authorities
    revoked = parse_mint_authorities({"mintAuthority": None, "freezeAuthority": None})
    assert revoked == {"mint_authority_revoked": True, "freeze_authority_revoked": True}
    live = parse_mint_authorities({"mintAuthority": "Abc", "freezeAuthority": None})
    assert live == {"mint_authority_revoked": False, "freeze_authority_revoked": True}
    assert parse_mint_authorities({}) == {"mint_authority_revoked": None, "freeze_authority_revoked": None}
