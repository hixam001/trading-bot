"""
llm/grounding.py — groundedness validation for LLM theses (D2 / §4.1).

The vocabulary is DERIVED FROM THE RULE DEFINITIONS themselves (not a
hand-maintained central keyword list), so adding a rule adds its vocabulary
in the same change. A thesis that references a rule's terms when that rule
was not part of the decision's actual rule list gets FLAGGED on the feed
event — flagged, never silently dropped or rewritten.

Known limitation (documented, accepted): paraphrases can slip past term
matching. This is a human-review flag, not a safety gate — the verdict was
already decided deterministically, so an undetected paraphrase costs nothing
structurally.
"""
from __future__ import annotations

import re

# Terms each rule may legitimately mention in a thesis.
RULE_GROUNDING_TERMS: dict[str, tuple[str, ...]] = {
    "liquidity_floor": ("liquidity", "pool depth"),
    "volume_alive": ("volume", "tape", "1h volume"),
    "buy_pressure": ("buy pressure", "buys", "sells", "buy/sell"),
    "not_newborn_fade": ("newborn", "fresh launch", "newly launched", "newborn-fade"),
    "public_presence": ("twitter", "telegram", "website", "social", "presence"),
    "market_regime_ok": ("regime", "market state", "broad", "universe"),
    "cash_available": ("cash", "capital", "funds"),
    "crowd_heat": ("crowd", "fomo", "heat", "conviction", "hype", "attention"),
    "already_held": ("already held", "size on", "position in", "held"),
    "not_on_break": ("break", "awake", "paused", "liveness"),
    "security_clear": (
        "honeypot", "mint authority", "freeze authority", "rug",
        "authority revoked", "mint revok", "freeze revok",
    ),
}

_WORD_RE = re.compile(r"[a-z][a-z\-/ ]{2,}")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def validate_thesis(thesis: str, present_rule_ids: list[str]) -> list[str]:
    """
    Return a list of grounding flags. Empty list = clean. Flags name the
    ungrounded term and which absent rule it belongs to.
    """
    flags: list[str] = []
    norm = _normalize(thesis)
    present = set(present_rule_ids)
    for rule_id, terms in RULE_GROUNDING_TERMS.items():
        if rule_id in present:
            continue
        for term in terms:
            if term in norm:
                flags.append(
                    f"term '{term}' implies rule '{rule_id}' which was not "
                    f"in this decision's rule list"
                )
                break

    # Invented-rule check: rule-id-shaped tokens that aren't real rule ids
    # in this decision.
    for token in re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", norm):
        if token in RULE_GROUNDING_TERMS and token not in present:
            flags.append(f"references rule '{token}' not in this decision's rule list")
    return flags


def validate_numbers(thesis: str, detail_strings: list[str]) -> list[str]:
    """
    Numeric echo check: every number cited in the thesis should appear in one
    of the rule detail strings handed to the prompt. Counts of failing rules
    ("failed 3 check(s)") are meta-information about the decision, not data
    claims, and are excluded before the check.
    """
    def nums(s: str) -> set[str]:
        found = set()
        for m in re.finditer(r"-?\$?\d[\d,.]*%?", s.replace(",", "")):
            # Strip sentence punctuation glued to the number ('0.80.' at the
            # end of a clause must match the detail's '0.80').
            tok = m.group(0).strip(".").lstrip("+")
            if tok:
                found.add(tok)
        return found

    # Strip "N check(s)" style failure counts — they are not data claims.
    cleaned_thesis = re.sub(r"\b\d+\s+checks?\b", "", thesis, flags=re.I)
    allowed = set()
    for d in detail_strings:
        allowed |= nums(d.lower())
    flags = []
    for n in sorted(nums(cleaned_thesis.lower())):
        bare = n.lstrip("$").rstrip("%")
        if bare not in {x.lstrip("$").rstrip("%") for x in allowed}:
            flags.append(f"cited number '{n}' not present in any rule detail")
    return flags
