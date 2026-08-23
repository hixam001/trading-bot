#!/usr/bin/env bash
# ============================================================================
# One-shot verification for the discovery-diversity + memory-efficiency
# changes (Tasks A & B). Run from anywhere:
#   bash verify_tasks.sh
# Expects the repo .venv at ../.venv relative to this script's location.
# ============================================================================
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"

echo "== 1. Full test suite (expect all pass)"
cd "$ROOT/backend"
"$PY" -m pytest tests/ -q 2>&1 | tail -2

echo ""
echo "== 2. Mock-mode discovery_source variety (Task A.3)"
"$PY" - <<'EOF'
import asyncio
from data_providers.mock import MockProvider
cands = asyncio.run(MockProvider().get_candidates(6))
for c in cands[:6]:
    print(f"  {c.symbol:10s} discovery_source={c.discovery_source}")
sources = {c.discovery_source for c in cands}
assert {"trending", "new_listing", "both"} <= sources, sources
print("  OK: trending/new_listing/both all present")
EOF

echo ""
echo "== 3. Rule-engine isolation from discovery_source (Task A.4)"
"$PY" - <<'EOF'
import inspect
from rule_engine.rules import ACTIVE_RULES
for fn in ACTIVE_RULES:
    assert "discovery_source" not in inspect.getsource(fn), fn.__name__
print("  OK: no rule references discovery_source")
EOF

echo ""
echo "== 4. Thesis-reuse thresholds sanity (Task A.5)"
"$PY" - <<'EOF'
from llm.reuse import reused_if_stable
base = {"all_passed": False, "failed_rule_ids": ["buy_pressure"]}
sig = (50_000.0, 20_000.0, 100_000.0, 300, 200, 5.0)
assert reused_if_stable({"all_passed": False, "failed_rule_ids": ["buy_pressure"],
                         "stats": sig}, False, ["buy_pressure"], sig)
assert not reused_if_stable({"all_passed": False, "failed_rule_ids": ["buy_pressure"],
                             "stats": sig}, False, ["buy_pressure"],
                            (50_000.0, 21_500.0, 100_000.0, 300, 200, 5.0))
print("  OK: stable -> reuse; meaningful move -> fresh narration")
EOF

echo ""
echo "== 5. num_ctx set + provider shutdown hook (Task B)"
grep -n "num_ctx" "$ROOT/backend/config.py" | head -1
grep -n '"num_ctx": config.OLLAMA_NUM_CTX' "$ROOT/backend/llm/narrator.py"
grep -n "close_provider = getattr(provider" "$ROOT/backend/main.py"

echo ""
echo "== 6. Ollama measured prompt tokens (live; needs ollama running)"
"$PY" - <<'EOF'
import asyncio, json
from dotenv import load_dotenv
load_dotenv(__import__("pathlib").Path(__file__).resolve().parent.parent / ".env") \
    if False else None
from llm.narrator import Narrator, build_prompt
from models import Candidate

c = Candidate(symbol="PROBE", mint_address="M"*40, price_usd=0.001,
              liquidity_usd=50_000.0, volume_24h_usd=100_000.0,
              market_cap_usd=100_000.0, volume_1h_usd=20_000.0,
              buys_1h=300, sells_1h=200, price_change_1h_pct=5.0,
              age_hours=24.0, has_twitter=True,
              mint_authority_revoked=True, freeze_authority_revoked=True,
              is_likely_honeypot=False)
from rule_engine.gate import evaluate_gate
from rule_engine.regime import compute_market_regime
from rule_engine.rules import ACTIVE_RULES
gate = evaluate_gate(c, __import__("models").PortfolioState(cash_usd=1000.0),
                     compute_market_regime([c]), ACTIVE_RULES)
prompt = build_prompt(gate)

async def go():
    n = Narrator()
    ok = await n.check_ollama_health()
    if not ok:
        print("  ollama down — start it and rerun section 6")
        await n.aclose(); return
    r = await n.client.post(
        __import__("config").OLLAMA_GENERATE_ENDPOINT,
        json={"model": __import__("config").MODEL_NAME, "prompt": prompt,
              "stream": False, "think": False,
              "options": {"temperature": 0.2,
                          "num_ctx": __import__("config").OLLAMA_NUM_CTX}})
    j = r.json()
    print("  prompt_eval_count:", j.get("prompt_eval_count"),
          "| num_ctx:", __import__("config").OLLAMA_NUM_CTX)
    print("  thesis:", (j.get("response") or "").strip()[:120])
    await n.aclose()

asyncio.run(go())
print("  RULE: if prompt_eval_count > ~700, raise OLLAMA_NUM_CTX to 2048.")
EOF

echo ""
echo "== 7. live_execution offline tests (Task C)"
(cd "$ROOT" && "$PY" -m pytest live_execution/tests -q 2>&1 | tail -2)
if ! "$PY" -c "import solders" 2>/dev/null; then
  echo "  NOTE: 'solders' not installed — signing path untested."
  echo "  Install when going real: $PY -m pip install solders"
fi

echo ""
echo "== 8. Isolation: backend must never import live_execution"
grep -rn "live_execution" "$ROOT/backend" --include="*.py" \
  && echo "  FAIL: backend imports live_execution!" \
  || echo "  OK: backend/ has zero references to live_execution"

echo ""
echo "VERIFY DONE"

