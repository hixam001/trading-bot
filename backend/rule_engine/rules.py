"""
rule_engine/rules.py — the deterministic decision-makers (§2.3).

Every rule is a pure function (candidate, portfolio, regime) -> RuleResult.
No I/O, no randomness, no LLM. Given the same inputs, the same verdict.

None semantics:
  - Security fields: None = unknown = PASS (security_clear only asserts when
    the underlying field is known — per §2.3).
  - Numeric evaluation inputs (volume_1h_usd etc.): None = unevaluable =
    FAIL CLOSED with an explicit "unavailable" detail. A skipped entry costs
    nothing; an entry on missing data corrupts the record (defense-first
    rule 2). This is "skip the trade", never "guess a value".
"""
from __future__ import annotations

from typing import Optional

import config
from models import Candidate, PortfolioState, RuleResult
from rule_engine.regime import MarketRegime


def _unavailable(rule_id: str, field_name: str) -> RuleResult:
    """Fail-closed result for a rule whose input data was not returned."""
    return RuleResult(
        rule_id=rule_id,
        passed=False,
        detail=f"{field_name} unavailable (provider did not return it) — cannot evaluate",
        value=None,
    )


# --- liquidity_floor -------------------------------------------------------

def liquidity_floor(c: Candidate, p: PortfolioState, r: MarketRegime) -> RuleResult:
    if c.liquidity_usd is None:
        return _unavailable("liquidity_floor", "liquidity_usd")
    ok = c.liquidity_usd >= config.MIN_LIQUIDITY_USD
    return RuleResult(
        "liquidity_floor", ok,
        f"liquidity ${c.liquidity_usd:,.0f} vs floor ${config.MIN_LIQUIDITY_USD:,.0f}",
        value=c.liquidity_usd,
    )


# --- volume_alive ----------------------------------------------------------

def volume_alive(c: Candidate, p: PortfolioState, r: MarketRegime) -> RuleResult:
    if c.volume_1h_usd is None:
        return _unavailable("volume_alive", "volume_1h_usd")
    ok = c.volume_1h_usd >= config.MIN_VOLUME_1H_USD
    return RuleResult(
        "volume_alive", ok,
        f"1h volume ${c.volume_1h_usd:,.0f} vs min ${config.MIN_VOLUME_1H_USD:,.0f}",
        value=c.volume_1h_usd,
    )


# --- buy_pressure ----------------------------------------------------------

def buy_pressure(c: Candidate, p: PortfolioState, r: MarketRegime) -> RuleResult:
    if c.buys_1h is None or c.sells_1h is None:
        return _unavailable("buy_pressure", "buys_1h/sells_1h")
    ok = c.buys_1h > c.sells_1h
    return RuleResult(
        "buy_pressure", ok,
        f"buys {c.buys_1h} vs sells {c.sells_1h} (1h tx counts)",
        value=c.buys_1h - c.sells_1h,
    )


# --- not_newborn_fade (joint age+momentum condition) -----------------------

def not_newborn_fade(c: Candidate, p: PortfolioState, r: MarketRegime) -> RuleResult:
    if c.age_hours is None or c.price_change_1h_pct is None:
        return _unavailable("not_newborn_fade", "age_hours/price_change_1h_pct")
    is_newborn = c.age_hours < config.NEWBORN_AGE_HOURS
    is_fading = c.price_change_1h_pct <= -config.NEWBORN_FADE_PCT
    ok = not (is_newborn and is_fading)
    return RuleResult(
        "not_newborn_fade", ok,
        (
            f"age {c.age_hours:.1f}h, 1h change {c.price_change_1h_pct:+.1f}% — "
            + ("newborn AND fading hard" if not ok else "joint newborn-fade condition not met")
        ),
        value=c.age_hours,
    )


# --- public_presence --------------------------------------------------------

def public_presence(c: Candidate, p: PortfolioState, r: MarketRegime) -> RuleResult:
    channels = {
        "twitter": c.has_twitter,
        "telegram": c.has_telegram,
        "website": c.has_website,
    }
    present = [name for name, v in channels.items() if v is True]
    # None = unknown channel simply doesn't contribute; any one known-present
    # channel passes. Unknown ≠ absent.
    ok = len(present) > 0
    detail = f"present channels: {', '.join(present) if present else 'none confirmed'}"
    return RuleResult("public_presence", ok, detail, value=ok)


# --- market_regime_ok -------------------------------------------------------

def market_regime_ok(c: Candidate, p: PortfolioState, r: MarketRegime) -> RuleResult:
    return RuleResult(
        "market_regime_ok", bool(r.regime_ok),
        f"regime {'OK' if r.regime_ok else 'BAD'} this tick ({r.regime_detail})",
        value=r.regime_ok,
    )


# --- cash_available ---------------------------------------------------------

def cash_available(c: Candidate, p: PortfolioState, r: MarketRegime) -> RuleResult:
    size = config.INTENDED_POSITION_SIZE_USD
    ok = p.cash_usd >= size
    return RuleResult(
        "cash_available", ok,
        f"cash ${p.cash_usd:,.2f} vs intended size ${size:,.2f}",
        value=p.cash_usd,
    )


# --- crowd_heat (the reference bot parity; proxy source until a FOMO feed lands) -----

def compute_crowd_heat(c: Candidate) -> int:
    """
    0-100 conviction index, the reference bot' formula: heat = 20 + 8 x conviction
    items, clamped 0-100.

    Source priority (filled during the read stage by crowd.enrich_crowd_heat):
      1. c.fomo_heat — REAL heat from the fomo.fun board (theses with the
         authors' live positions behind them) or pump.fun comments
      2. presence-channel proxy (twitter/telegram/website) — documented
         fallback when no feed answered (see docs/FOMO_INTEGRATION.md)
    """
    if c.fomo_heat is not None:
        return int(c.fomo_heat)
    signals = sum(1 for v in (c.has_twitter, c.has_telegram, c.has_website) if v)
    return min(100, config.CROWD_HEAT_BASE
               + config.CROWD_HEAT_PER_SIGNAL * signals)


def crowd_heat(c: Candidate, p: PortfolioState, r: MarketRegime) -> RuleResult:
    # §43: the fomo.fun lookup that feeds this rule is metered, so the pipeline
    # only spends it on candidates that already cleared every other rule. When
    # it was deliberately skipped we say so instead of scoring the presence
    # proxy — a rejected candidate's journal reads "not evaluated" for this one
    # field. Fail-closed by construction: passed=False + evaluated=False can
    # never contribute to all_passed (see gate.evaluate_gate).
    if c.crowd_lookup_deferred and c.fomo_heat is None:
        return RuleResult(
            "crowd_heat", False,
            "not evaluated — crowd feed reserved for candidates that cleared "
            "every other rule (this one did not; no quota spent)",
            value=None, evaluated=False,
        )
    heat = compute_crowd_heat(c)
    source = (c.crowd_heat_source or "proxy").strip() or "proxy"
    ok = config.CROWD_HEAT_MIN <= heat <= config.CROWD_HEAT_MAX
    if ok:
        detail = (f"heat {heat} [{source}] inside act band "
                  f"[{config.CROWD_HEAT_MIN}, {config.CROWD_HEAT_MAX}]")
    elif heat < config.CROWD_HEAT_MIN:
        detail = (f"heat {heat} [{source}] below act band "
                  f"[{config.CROWD_HEAT_MIN}, {config.CROWD_HEAT_MAX}] — "
                  f"no crowd conviction yet")
    else:
        detail = f"heat {heat} [{source}] above act band — hype peak"
    return RuleResult("crowd_heat", ok, detail, value=heat)


# --- already_held (the reference bot parity: strictly ONE position per name) ---------

def already_held(c: Candidate, p: PortfolioState, r: MarketRegime) -> RuleResult:
    held = p.held_usd_in_mint(c.mint_address)
    ok = held <= 0.0
    detail = (
        "no size on"
        if ok else
        f"already ${held:,.2f} on in {c.symbol} — one position per name, "
        f"no pyramiding into a held ticket"
    )
    return RuleResult("already_held", ok, detail, value=held)


# --- not_on_break (the reference bot parity: loop liveness/operator break) ------------

def not_on_break(c: Candidate, p: PortfolioState, r: MarketRegime) -> RuleResult:
    from rule_engine import liveness
    on_break = liveness.is_on_break()
    ok = not on_break
    if ok:
        detail = "awake"
    else:
        reason = liveness.break_reason()
        detail = f"operator break active — {reason}" if reason else "operator break active — no entries"
    return RuleResult("not_on_break", ok, detail, value=ok)


# --- security_clear (only asserts on KNOWN-bad values) ----------------------

def security_clear(c: Candidate, p: PortfolioState, r: MarketRegime) -> RuleResult:
    problems = []
    if c.mint_authority_revoked is False:
        problems.append("mint authority NOT revoked")
    if c.is_likely_honeypot is True:
        problems.append("flagged as likely honeypot")
    ok = not problems

    def fmt(v):
        return "unknown" if v is None else ("yes" if v else "no")

    detail = "; ".join(problems) if problems else (
        f"no known-bad signals "
        f"(mint authority revoked: {fmt(c.mint_authority_revoked)}, "
        f"freeze authority revoked: {fmt(c.freeze_authority_revoked)}, "
        f"honeypot: {fmt(c.is_likely_honeypot)})"
    )
    return RuleResult("security_clear", ok, detail, value=ok)


# ---------------------------------------------------------------------------
# The active rule set - the reference bot 9 rules, in evaluation order, plus
# security_clear (re-activated 2026-08-29 by explicit operator decision; it
# fails only on KNOWN-bad values per B10: live mint authority or a honeypot
# flag - None/unknown always passes, so it can never block a trade on missing
# data). market_regime_ok remains computed/logged but retired from the gate
# (observability only).
# evaluate_gate() runs ALL of them unconditionally (no short-circuiting)
# so every rejection shows its full profile in the journal.
# ---------------------------------------------------------------------------

ACTIVE_RULES = [
    liquidity_floor,
    volume_alive,
    buy_pressure,
    not_newborn_fade,
    public_presence,
    crowd_heat,
    cash_available,
    already_held,
    security_clear,
    not_on_break,
]

