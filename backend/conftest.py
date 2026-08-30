# Puts backend/ (this directory) on sys.path so every test — backend/tests
# AND live_execution/tests — can `import config`, `import run_live_cycle`,
# `from live_execution import ...` deterministically, regardless of the
# working directory pytest was launched from or test-file ordering.
import sys
from pathlib import Path

_BACKEND = str(Path(__file__).resolve().parent)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
