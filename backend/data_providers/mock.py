"""
data_providers/mock.py — synthetic candidate provider (A6).

Mock mode must work throughout the entire build: every Candidate field gets
a plausible value, and the generated universe deliberately straddles BOTH
sides of every rule threshold so the full pipeline — every rule, both
branches, narration, exits — is exercised without live API keys.

Values are deterministic per (mint, tick) so tests are reproducible, while
prices drift between ticks so exit conditions can genuinely trigger.
"""
from __future__ import annotations

import hashlib
import time
from typing import Optional

from models import Candidate, SecurityInfo


# Archetypes: each one fails exactly one specific rule (or passes all), so a
# mock batch produces rich, varied feed events and rejections with real
# reasons. Fields chosen relative to config thresholds at import time.
def _archetypes() -> list[dict]:
    return [
        # Passes everything -> will open / scale in
        dict(symbol="HEALTH", price_usd=0.0012, liquidity_usd=60_000.0,
             volume_24h_usd=250_000.0, market_cap_usd=180_000.0,
             volume_1h_usd=25_000.0, buys_1h=320, sells_1h=210,
             price_change_1h_pct=6.0, age_hours=30.0,
             has_twitter=True, has_telegram=None, has_website=True),
        # Fails liquidity_floor
        dict(symbol="THINLIQ", price_usd=0.0004, liquidity_usd=4_000.0,
             volume_24h_usd=90_000.0, market_cap_usd=120_000.0,
             volume_1h_usd=24_000.0, buys_1h=200, sells_1h=150,
             price_change_1h_pct=3.0, age_hours=20.0, has_twitter=True),
        # Fails volume_alive
        dict(symbol="DEADTAPE", price_usd=0.002, liquidity_usd=40_000.0,
             volume_24h_usd=8_000.0, market_cap_usd=90_000.0,
             volume_1h_usd=900.0, buys_1h=30, sells_1h=25,
             price_change_1h_pct=-1.0, age_hours=48.0, has_telegram=True),
        # Fails buy_pressure
        dict(symbol="SELLOFF", price_usd=0.0008, liquidity_usd=35_000.0,
             volume_24h_usd=150_000.0, market_cap_usd=110_000.0,
             volume_1h_usd=26_000.0, buys_1h=120, sells_1h=310,
             price_change_1h_pct=-8.0, age_hours=12.0, has_website=True),
        # Fails not_newborn_fade (young AND fading hard)
        dict(symbol="NEWFADE", price_usd=0.0003, liquidity_usd=22_000.0,
             volume_24h_usd=70_000.0, market_cap_usd=60_000.0,
             volume_1h_usd=23_000.0, buys_1h=90, sells_1h=260,
             price_change_1h_pct=-45.0, age_hours=1.2, has_twitter=True),
        # Fails public_presence (no channels)
        dict(symbol="GHOSTY", price_usd=0.0009, liquidity_usd=28_000.0,
             volume_24h_usd=130_000.0, market_cap_usd=95_000.0,
             volume_1h_usd=22_000.0, buys_1h=210, sells_1h=190,
             price_change_1h_pct=2.5, age_hours=18.0,
             has_twitter=False, has_telegram=False, has_website=False),
        # Fails security_clear (honeypot flag)
        dict(symbol="HONEYPT", price_usd=0.0006, liquidity_usd=45_000.0,
             volume_24h_usd=160_000.0, market_cap_usd=140_000.0,
             volume_1h_usd=27_000.0, buys_1h=280, sells_1h=240,
             price_change_1h_pct=9.0, age_hours=26.0, has_twitter=True,
             is_likely_honeypot=True, mint_authority_revoked=True),
        # Fails volume_mcap_ratio_ok (thin 24h vol vs cap)
        dict(symbol="BUNDLE", price_usd=0.0018, liquidity_usd=55_000.0,
             volume_24h_usd=9_000.0, market_cap_usd=300_000.0,
             volume_1h_usd=21_000.0, buys_1h=150, sells_1h=140,
             price_change_1h_pct=1.0, age_hours=60.0, has_twitter=True),
    ]


_ARCHETYPES = _archetypes()


def _seed(mint: str) -> int:
    return int(hashlib.sha256(mint.encode()).hexdigest()[:8], 16)


class MockProvider:
    """Deterministic-but-dynamic synthetic data source."""

    def __init__(self) -> None:
        self._tick = 0
        self._anchors: dict[str, float] = {}   # mint -> last issued entry price

    async def get_candidates(self, limit: int) -> list[Candidate]:
        self._tick += 1
        out: list[Candidate] = []
        for i in range(min(limit, len(_ARCHETYPES))):
            a = dict(_ARCHETYPES[i % len(_ARCHETYPES)])
            mint = f"MockMint{i:02d}{'x' * 32}{self._tick:04d}"
            drift = ((self._tick * 7 + i * 13) % 21 - 10) / 100.0   # ±10%
            a["price_usd"] = round(a["price_usd"] * (1 + drift), 8)
            a["price_change_1h_pct"] = round(a["price_change_1h_pct"] + drift * 50, 2)
            cand = Candidate(mint_address=mint, source="mock", **{
                k: v for k, v in a.items()
            })
            # Varied mint decimals for field parity (6-decimal pump tokens,
            # 9-decimal standards) — exercises decimals-aware price quoting.
            cand.decimals = (6, 9, 6, 8, 6, 9, 6, 8)[i % 8]
            # Varied discovery provenance so downstream display of
            # discovery_source is exercised in mock mode too.
            cand.discovery_source = ("trending", "new_listing", "both")[i % 3]
            out.append(cand)
            self._anchors[mint] = a["price_usd"]
        # One candidate per batch carries unknown security fields so the
        # None-passes-security_clear path is exercised end-to-end.
        if out:
            out[-1].mint_authority_revoked = None
            out[-1].freeze_authority_revoked = None
            out[-1].is_likely_honeypot = None
        return out

    async def get_current_price(self, mint_address: str,
                                decimals: Optional[int] = None) -> float:
        """
        Price anchored to the entry price issued for this mint (±5% walk),
        deterministic per 30s window. `decimals` accepted for protocol parity
        but ignored — synthetic prices are already real-world USD values,
        not raw-unit quotes.
        """
        base_seed = _seed(mint_address)
        elapsed = int(time.time() / 30)   # moves every ~30s
        h = (_seed(mint_address + str(elapsed)) % 1000) / 1000.0   # 0..1
        anchor = self._anchors.get(mint_address)
        if anchor is None:
            base_prices = [a["price_usd"] for a in _ARCHETYPES]
            anchor = base_prices[base_seed % len(base_prices)]
        return round(anchor * (0.95 + 0.10 * h), 8)

    async def get_security_info(self, mint_address: str) -> SecurityInfo:
        r = _seed(mint_address + "sec") % 4
        return [
            SecurityInfo(True, True, False),          # clean, fully known
            SecurityInfo(False, True, False),         # mint authority live -> fails
            SecurityInfo(None, None, None),           # entirely unknown -> passes
            SecurityInfo(None, None, True),           # honeypot flagged -> fails
        ][r]
