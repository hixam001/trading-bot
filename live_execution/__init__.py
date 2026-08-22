"""
live_execution — REAL-MONEY execution package. Strictly separated from the
paper-trading pipeline (backend/).

Isolation contract (enforced by tests):
  - Nothing under backend/ may import this package.
  - This package never auto-triggers from signals/tick loop/API; it is
    operator-invoked only (python -m live_execution.execute ...).
  - It runs on its OWN enable-flag (LIVE_EXECUTION_ENABLED), independent of
    the paper system's PAPER_TRADING_ONLY (which stays hardcoded True).
"""
