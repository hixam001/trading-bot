"""Small schema predicates shared by persisted authority readers."""
import math


def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def validate_wallet_snapshot(snapshot):
    """Validate the persisted wallet-drop baseline without rewriting history.

    The historical schema has no wallet identity field. Preserve its optional
    operator acknowledgement metadata; validate an identity if one is supplied
    rather than silently adopting malformed future/owner-created metadata.
    """
    error = "wallet snapshot invalid; preserve history and reconcile before repair"
    if not isinstance(snapshot, dict):
        raise RuntimeError(error + ": expected object")
    for field in ("ts", "usdc", "sol"):
        value = snapshot.get(field)
        if not finite_number(value) or value < 0:
            raise RuntimeError(error + ": " + field + " must be finite and nonnegative")
    for field in ("wallet", "wallet_address", "owner"):
        if field in snapshot and (not isinstance(snapshot[field], str) or not snapshot[field].strip()):
            raise RuntimeError(error + ": " + field + " identity must be a nonempty string")
