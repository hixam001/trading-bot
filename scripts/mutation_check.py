#!/usr/bin/env python3
"""
scripts/mutation_check.py — Automated, reproducible mutation testing harness.

Runs declarative mutations against critical security barriers, failure classifications,
and integrity checks. Verifies that every mutation causes at least one real test to fail.
Guarantees file restoration even on error/interrupt and verifies clean git diff.

Usage:
    python scripts/mutation_check.py
    python scripts/mutation_check.py --write-doc
"""

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Mutation:
    phase: str
    mutation_id: str
    file_path: str
    exact_old_text: str
    new_text: str
    test_selector: str
    description: str


MUTATIONS: list[Mutation] = [
    # §81-A
    Mutation(
        phase="§81-A",
        mutation_id="81A_EXIT_ERRORS_RULE1",
        file_path="backend/live_execution/exit_errors.py",
        exact_old_text="code = custom_error_code(exc_or_msg.onchain_err)",
        new_text="code = None",
        test_selector="backend/tests/test_audit_81_a.py",
        description="Rule 1 custom error extraction disabled",
    ),
    Mutation(
        phase="§81-A",
        mutation_id="81A_EXECUTOR_LANDED_FAILED",
        file_path="backend/live_execution/executor.py",
        exact_old_text="if failure.retry and attempt_idx <= len(ladder):",
        new_text="if False and attempt_idx <= len(ladder):",
        test_selector="backend/tests/test_audit_81_a.py",
        description="Landed retryable error ladder escalation disabled",
    ),
    # §81-B
    Mutation(
        phase="§81-B",
        mutation_id="81B_RECONCILE_NONE_BALANCES",
        file_path="backend/live_execution/reconcile.py",
        exact_old_text="if current_usdc is None or current_sol is None:",
        new_text="if False:",
        test_selector="backend/tests/test_audit_81_b.py",
        description="Fail-closed None balance skip check disabled",
    ),
    Mutation(
        phase="§81-B",
        mutation_id="81B_RECONCILE_SOL_DROPPED",
        file_path="backend/live_execution/reconcile.py",
        exact_old_text="sol_dropped = actual_sol_delta < -sol_tolerance",
        new_text="sol_dropped = False",
        test_selector="backend/tests/test_audit_81_b.py",
        description="Independent SOL channel drop detection disabled",
    ),
    # §81-C
    Mutation(
        phase="§81-C",
        mutation_id="81C_SIM_GUARD_DELEGATE",
        file_path="backend/live_execution/sim_guard.py",
        exact_old_text='if post_tok.get("delegate") is not None:',
        new_text="if False:",
        test_selector="backend/tests/test_audit_81_c.py",
        description="Created token account delegate refusal disabled",
    ),
    Mutation(
        phase="§81-C",
        mutation_id="81C_SIM_GUARD_CLOSE_AUTH",
        file_path="backend/live_execution/sim_guard.py",
        exact_old_text='if post_tok.get("close_authority") is not None:',
        new_text="if False:",
        test_selector="backend/tests/test_audit_81_c.py",
        description="Created token account close_authority refusal disabled",
    ),
    Mutation(
        phase="§81-C",
        mutation_id="81C_SIM_GUARD_STATE",
        file_path="backend/live_execution/sim_guard.py",
        exact_old_text='if post_tok.get("state") != 1:',
        new_text="if False:",
        test_selector="backend/tests/test_audit_81_c.py",
        description="Created token account uninitialized state refusal disabled",
    ),
    Mutation(
        phase="§81-C",
        mutation_id="81C_SOLANA_AGREE_CLOSE_AUTH",
        file_path="backend/live_execution/solana.py",
        exact_old_text='if dec_a.get("close_authority") != dec_b.get("close_authority"):',
        new_text="if False:",
        test_selector="backend/tests/test_audit_81_c.py",
        description="responses_agree close_authority consensus disabled",
    ),
    Mutation(
        phase="§81-C",
        mutation_id="81C_SOLANA_AGREE_DELEGATED_AMT",
        file_path="backend/live_execution/solana.py",
        exact_old_text='if dec_a.get("delegated_amount") != dec_b.get("delegated_amount"):',
        new_text="if False:",
        test_selector="backend/tests/test_audit_81_c.py",
        description="responses_agree delegated_amount consensus disabled",
    ),
    Mutation(
        phase="§81-C",
        mutation_id="81C_EXECUTOR_BUY_REF_PRICE",
        file_path="backend/live_execution/executor.py",
        exact_old_text="if not has_valid_reference_price(reference_price_usd):",
        new_text="if False:",
        test_selector="backend/tests/test_audit_81_c.py",
        description="place_buy pre-memo reference price requirement disabled",
    ),
    # §81-D
    Mutation(
        phase="§81-D",
        mutation_id="81D_QUOTE_PARAMS_BUY_DEXES",
        file_path="backend/live_execution/quote_params.py",
        exact_old_text='if side.lower() == "buy":',
        new_text="if True:",
        test_selector="backend/tests/test_audit_81_d.py",
        description="Sell DEX filter exclusion disabled (sells filter DEXes)",
    ),
    # §81-E
    Mutation(
        phase="§81-E",
        mutation_id="81E_RESTORE_KILL_SWITCH_TRIP",
        file_path="scripts/restore_state.py",
        exact_old_text="kill_switch.trip(trip_reason, state_dir=target_state_dir)  # Pass 4 safety latch",
        new_text="pass",
        test_selector="backend/tests/test_audit_81_e.py",
        description="Post-restore mandatory kill switch trip disabled",
    ),
    Mutation(
        phase="§81-E",
        mutation_id="81E_RESTORE_PRAGMA_CHECK",
        file_path="scripts/restore_state.py",
        exact_old_text='if not res or res[0][0] != "ok":',
        new_text="if False:",
        test_selector="backend/tests/test_audit_81_e.py",
        description="SQLite PRAGMA integrity_check validation disabled",
    ),
    Mutation(
        phase="§81-E",
        mutation_id="81E_RESTORE_TRAVERSAL_PARTS",
        file_path="scripts/restore_state.py",
        exact_old_text='if ".." in parts:',
        new_text="if False:",
        test_selector="backend/tests/test_audit_81_e.py",
        description="Archive member path directory traversal check disabled",
    ),
    # §82-A
    Mutation(
        phase="§82-A",
        mutation_id="82A_MEMO_SEND_REJECTED",
        file_path="backend/live_execution/memo.py",
        exact_old_text="except solana.SendRejected as exc:",
        new_text="except (KeyError, ValueError) as exc:",
        test_selector="backend/tests/test_audit_82_a.py",
        description="publish_commit_memo SendRejected containment handler disabled",
    ),
    Mutation(
        phase="§82-A",
        mutation_id="82A_EXEC_CONFIRMED_TRADE",
        file_path="backend/live_execution/jupiter_executor.py",
        exact_old_text="except SendRejected as exc:",
        new_text="except (KeyError, ValueError) as exc:",
        test_selector="backend/tests/test_audit_82_a.py",
        description="execute_confirmed_trade SendRejected containment handler disabled",
    ),
    # §82-B
    Mutation(
        phase="§82-B",
        mutation_id="82B_ALREADY_PROCESSED",
        file_path="backend/live_execution/solana.py",
        exact_old_text="if any(is_already_processed(a) for a in outcome.attempts):",
        new_text="if False:",
        test_selector="backend/tests/test_audit_82_b.py",
        description="Already-processed ambiguous send return None disabled",
    ),
    Mutation(
        phase="§82-B",
        mutation_id="82B_PREFLIGHT_RESET_SIG",
        file_path="backend/live_execution/executor.py",
        exact_old_text="last_sig = None\n                    last_blockhash = None",
        new_text="pass",
        test_selector="backend/tests/test_audit_82_b.py",
        description="Preflight rejection last_sig reset on sell ladder disabled",
    ),
    # §82-C
    Mutation(
        phase="§82-C",
        mutation_id="82C_RECONCILE_KILL_SWITCH",
        file_path="backend/live_execution/reconcile.py",
        exact_old_text='kill_switch.trip(f"wallet_drop: {channel_str}", state_dir=target_dir)',
        new_text="pass",
        test_selector="backend/tests/test_audit_82_c.py",
        description="check_wallet_drop kill switch trip disabled",
    ),
    # §82-D
    Mutation(
        phase="§82-D",
        mutation_id="82D_USDC_NONE_DROP_CHECK",
        file_path="backend/run_live_cycle.py",
        exact_old_text="check_wallet_drop(usdc_bal, sol_bal, ledger=ledger)",
        new_text="check_wallet_drop(cash, sol_bal, ledger=ledger)",
        test_selector="backend/tests/test_audit_82_d.py",
        description="Pass unreadable usdc_bal as None to check_wallet_drop reverted to cash (0.0)",
    ),
    # §82-F
    Mutation(
        phase="§82-F",
        mutation_id="82F_RESTORE_RECONCILE_ONLY",
        file_path="scripts/restore_state.py",
        exact_old_text="if args.reconcile_only:",
        new_text="if False:",
        test_selector="backend/tests/test_audit_82_f.py",
        description="restore_state.py --reconcile-only execution check disabled",
    ),
    Mutation(
        phase="§82-F",
        mutation_id="82F_RESTORE_PARTIAL_PROMOTION",
        file_path="scripts/restore_state.py",
        exact_old_text="if len(promoted) > 0:",
        new_text="if False:",
        test_selector="backend/tests/test_audit_82_f.py",
        description="Partial promotion kill switch trip and rollback check disabled",
    ),
    # §87-A
    Mutation(
        phase="§87-A",
        mutation_id="87A_ENTRYPOINT_WALLET_READABLE",
        file_path="backend/docker-entrypoint.sh",
        exact_old_text=(
            '  [ -n "${WALLET_KEYPAIR_PATH:-}" ] \\\n'
            '    && { [ ! -f "${WALLET_KEYPAIR_PATH}" ] || [ ! -r "${WALLET_KEYPAIR_PATH}" ]; }'
        ),
        new_text="  return 1",
        test_selector="backend/tests/test_docker_entrypoint.py",
        description="Entrypoint wallet-readable/regular-file check disabled",
    ),
    # §87-B
    Mutation(
        phase="§87-B",
        mutation_id="87B_GRACEFUL_SHUTDOWN_EXIT_LOCK",
        file_path="backend/run_live_cycle.py",
        exact_old_text=(
            "    async with _exit_lock():\n"
            "        if exit_task is not None and not exit_task.done():"
        ),
        new_text=(
            "    if True:\n"
            "        if exit_task is not None and not exit_task.done():"
        ),
        test_selector="backend/tests/test_graceful_shutdown.py",
        description="Graceful shutdown no longer waits for the in-flight exit lock",
    ),
    # §87-C
    Mutation(
        phase="§88-E",
        mutation_id="88E_PRE_SEND_PENDING_WRITE",
        file_path="backend/live_execution/executor.py",
        exact_old_text=(
            "        ledger.record_pending_buy(\n"
            "            idempotency_key=idempotency_key,\n"
            "            mint=mint,"
        ),
        new_text=(
            "        (lambda **kwargs: None)(\n"
            "            idempotency_key=idempotency_key,\n"
            "            mint=mint,"
        ),
        test_selector="backend/live_execution/tests/test_executor_memo_flow.py",
        description="Pre-send pending ledger write disabled (crash loses the signed tx)",
    ),
    Mutation(
        phase="§87-C",
        mutation_id="87C_BLOCKHASH_EXPIRY_PREDICATE",
        file_path="backend/run_live_cycle.py",
        exact_old_text="                if valid is False:",
        new_text="                if valid is not True:",
        test_selector="backend/tests/test_pending_buy_resolution.py",
        description="Pending buy expires on unknown blockhash validity (unknown != expired)",
    ),
    Mutation(
        phase="§87-D",
        mutation_id="87D_EXIT_BACKOFF_GUARD",
        file_path="backend/run_live_cycle.py",
        exact_old_text="if not _exit_backoff_allows(mint, m[\"tokens\"], risk_off):",
        new_text="if False:",
        test_selector="backend/tests/test_exit_backoff.py",
        description="Failed-exit retry backoff disabled (sell hammered every scan)",
    ),
    Mutation(
        phase="§87-D",
        mutation_id="87D_RISK_OFF_BACKOFF_CAP",
        file_path="backend/run_live_cycle.py",
        exact_old_text="if risk_off:\n        delay = min(delay, _EXIT_RETRY_RISK_OFF_MAX_SECONDS)",
        new_text="if False:\n        delay = min(delay, _EXIT_RETRY_RISK_OFF_MAX_SECONDS)",
        test_selector="backend/tests/test_exit_backoff.py",
        description="Risk-off exit backoff cap disabled (stop may wait 300s)",
    ),
    # §87-D2 (operator-approved 2026-11-01)
    Mutation(
        phase="§87-D2",
        mutation_id="87D2_RISK_OFF_IMPACT_CEILING",
        file_path="backend/live_execution/executor.py",
        exact_old_text=(
            "        risk_off_exit = isinstance(rule_id, str) and rule_id in RISK_OFF_RULES"
        ),
        new_text="        risk_off_exit = False",
        test_selector="backend/live_execution/tests/test_executor_memo_flow.py",
        description="Risk-off exit price-impact ceiling disabled (protective exit uses 2.5%)",
    ),
    # §87-E
    Mutation(
        phase="§87-E",
        mutation_id="87E_ZERO_QUANTITY_GUARD",
        file_path="backend/run_live_cycle.py",
        exact_old_text=(
            '        if float(m.get("tokens") or 0.0) <= 0:\n'
            "            # E2: zero tokens is NOT one token. Skip pricing/exit evaluation\n"
            "            # (and the mirror) until the row has a real quantity.\n"
            "            _alert_unresolved_quantity(mint)\n"
            "            continue"
        ),
        new_text=(
            "        if False:\n"
            "            _alert_unresolved_quantity(mint)\n"
            "            continue"
        ),
        test_selector="backend/tests/test_audit_87_e.py",
        description="Zero-token quantity guard disabled (prices an invented 1.0 size)",
    ),
    # §88-A
    Mutation(
        phase="§88-A",
        mutation_id="88A_GATE_PENDING_VISIBLE",
        file_path="backend/run_live_cycle.py",
        exact_old_text=(
            "    for mint, cost in pending_costs.items():\n"
            "        positions.append(Trade("
        ),
        new_text=(
            "    for mint, cost in {}.items():\n"
            "        positions.append(Trade("
        ),
        test_selector="backend/tests/test_audit_88_a.py",
        description="Pending buys hidden from the live gate (already_held can pass)",
    ),
    Mutation(
        phase="§88-A",
        mutation_id="88A_PREFLIGHT_PENDING_GUARD",
        file_path="backend/live_execution/jupiter_executor.py",
        exact_old_text=(
            "    pending_mints = {r.mint for r in ledger.pending_buys()}\n"
            "    if output_mint in pending_mints:"
        ),
        new_text=(
            "    pending_mints = {r.mint for r in ledger.pending_buys()}\n"
            "    if False:"
        ),
        test_selector="backend/tests/test_audit_88_a.py",
        description="Preflight accepts a second buy while the first is pending",
    ),
    Mutation(
        phase="§88-A",
        mutation_id="88A_EXECUTOR_PENDING_GUARD",
        file_path="backend/live_execution/executor.py",
        exact_old_text="        if mint in {r.mint for r in ledger.pending_buys()}:",
        new_text="        if False:",
        test_selector="backend/tests/test_audit_88_a.py",
        description="Executor pre-replay pending guard disabled (defence in depth lost)",
    ),
    # §88-B
    Mutation(
        phase="§88-B",
        mutation_id="88B_ENTRYPOINT_PASSTHROUGH",
        file_path="backend/docker-entrypoint.sh",
        exact_old_text=(
            'if [ "$#" -gt 0 ]; then\n'
            '  exec "$@"\n'
            'fi'
        ),
        new_text=(
            'if false; then\n'
            '  exec "$@"\n'
            'fi'
        ),
        test_selector="backend/tests/test_docker_entrypoint.py",
        description="Explicit commands ignored before role logic (starts a second engine)",
    ),
    # §88-C
    Mutation(
        phase="§88-C",
        mutation_id="88C_JSON_CHANNEL_SKIP",
        file_path="scripts/preflight_deploy.py",
        exact_old_text="    if wallet_channel(env) == WALLET_CHANNEL_JSON:",
        new_text="    if False:",
        test_selector="backend/tests/test_preflight_deploy.py",
        description="JSON wallet channel still checks the absent host keypair file",
    ),
    Mutation(
        phase="§88-C",
        mutation_id="88C_EUID_READABILITY_GATE",
        file_path="scripts/preflight_deploy.py",
        exact_old_text=(
            "    if euid in (EXPECTED_WALLET_UID, 0) and not os.access(path, os.R_OK):"
        ),
        new_text="    if not os.access(path, os.R_OK):",
        test_selector="backend/tests/test_preflight_deploy.py",
        description="Readability checked from an unrelated uid (false FAIL for chmod 600)",
    ),
    # §88-D
    Mutation(
        phase="§88-D",
        mutation_id="88D_MULTI_ENDPOINT_REQUERY",
        file_path="backend/run_live_cycle.py",
        exact_old_text="                    if sig and not status_requeried:",
        new_text="                    if False:",
        test_selector="backend/tests/test_pending_buy_resolution.py",
        description="Pending expiry trusts one endpoint instead of re-querying all",
    ),
    Mutation(
        phase="§88-D",
        mutation_id="88D_EXPIRY_MIN_AGE",
        file_path="backend/run_live_cycle.py",
        exact_old_text=(
            "                    if recheck_ok and age >= PENDING_EXPIRY_MIN_AGE_SECONDS:"
        ),
        new_text="                    if recheck_ok:",
        test_selector="backend/tests/test_pending_buy_resolution.py",
        description="Young pending buy expires before the 90s late-land window",
    ),
    Mutation(
        phase="§88-D",
        mutation_id="88D_STATUS_ALL_ENDPOINTS",
        file_path="backend/live_execution/solana.py",
        exact_old_text=(
            "    return list(await asyncio.gather(*(_one(ep) for ep in target)))"
        ),
        new_text="    return [await _one(target[0])] if target else []",
        test_selector="backend/tests/test_audit_88_d.py",
        description="Signature re-query stops at the first endpoint (lagging node wins)",
    ),
    # §88-E
    Mutation(
        phase="§88-E",
        mutation_id="88E_REPLACE_PENDING_ON_FILL",
        file_path="backend/live_execution/models.py",
        exact_old_text=(
            '            if (raw.get("kind") == "buy"\n'
            '                    and raw.get("idempotency_key") == idempotency_key\n'
            '                    and raw.get("status") == "pending"):'
        ),
        new_text="            if False:",
        test_selector="backend/live_execution/tests/test_executor_memo_flow.py",
        description="Fill appends a second buy row instead of replacing the pending one",
    ),
    # §88-F
    Mutation(
        phase="§88-F",
        mutation_id="88F_STOP_ENTRY_GUARD",
        file_path="backend/run_live_cycle.py",
        exact_old_text="        if stop_event.is_set():",
        new_text="        if False:",
        test_selector="backend/tests/test_audit_88_f.py",
        description="A new entry is placed after shutdown was requested",
    ),
    Mutation(
        phase="§88-F",
        mutation_id="88F_RESOLVE_REQUIRE_PENDING",
        file_path="scripts/resolve_pending_buy.py",
        exact_old_text="    if rec is None:",
        new_text="    if False:",
        test_selector="backend/tests/test_resolve_pending_buy.py",
        description="Operator resolver proceeds without a pending row",
    ),
    Mutation(
        phase="§88-F",
        mutation_id="88F_ADOPT_ACTUALS_REQUIRED",
        file_path="scripts/resolve_pending_buy.py",
        exact_old_text=(
            "    if (usd_actual is None or tokens_actual is None\n"
            "            or float(usd_actual) <= 0 or float(tokens_actual) <= 0):"
        ),
        new_text="    if False:",
        test_selector="backend/tests/test_resolve_pending_buy.py",
        description="Operator adoption proceeds without readable fill actuals",
    ),
    Mutation(
        phase="§88-F",
        mutation_id="88F_ADOPT_OWNER_REQUIRED",
        file_path="scripts/resolve_pending_buy.py",
        exact_old_text="    if not owner:",
        new_text="    if False:",
        test_selector="backend/tests/test_resolve_pending_buy.py",
        description="Operator adoption reads unfiltered deltas without a wallet owner",
    ),
]


def run_mutation(m: Mutation, py_exec: str) -> tuple[bool, str, str]:
    target = REPO_ROOT / m.file_path
    if not target.exists():
        raise FileNotFoundError(f"File not found: {target}")

    original_bytes = target.read_bytes()
    original_text = original_bytes.decode("utf-8")

    count = original_text.count(m.exact_old_text)
    if count != 1:
        raise ValueError(
            f"Mutation {m.mutation_id}: exact_old_text found {count} times in {m.file_path} (expected 1)"
        )

    mutated_text = original_text.replace(m.exact_old_text, m.new_text, 1)

    try:
        target.write_bytes(mutated_text.encode("utf-8"))
        cmd = [
            py_exec,
            "-m",
            "pytest",
            "-q",
            "-x",
            "-p",
            "no:cacheprovider",
            m.test_selector,
        ]
        sub_env = dict(os.environ)
        sub_env["PYTHONDONTWRITEBYTECODE"] = "1"
        res = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            env=sub_env,
        )
        caught = res.returncode != 0
        failing_test = ""
        failure_line = ""

        # Extract failing test name from FAILED lines first
        for line in res.stdout.splitlines():
            line_s = line.strip()
            if line_s.startswith("FAILED "):
                parts = line_s.split()
                if len(parts) >= 2:
                    test_id = parts[1]
                    failing_test = test_id.split("::")[-1].split("[")[0]
                    failure_line = line_s
                    break

        if not failing_test:
            for line in res.stdout.splitlines():
                line_s = line.strip()
                if line_s.startswith("ERROR ") and "::" in line_s:
                    parts = line_s.split()
                    if len(parts) >= 2:
                        test_id = parts[1]
                        failing_test = test_id.split("::")[-1].split("[")[0]
                        failure_line = line_s
                        break

        if caught and not failing_test:
            m_fail = re.search(r"::(test_\w+)", res.stdout)
            if m_fail:
                failing_test = m_fail.group(1)
            failure_line = (res.stderr or res.stdout).strip().splitlines()[-1] if (res.stderr or res.stdout).strip() else "non-zero exit"

        return caught, failing_test, failure_line
    finally:
        target.write_bytes(original_bytes)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run mutation checks")
    parser.add_argument("--write-doc", action="store_true", help="Overwrite docs/17_MUTATION_LOG.md")
    args = parser.parse_args()

    # Find python executable
    venv_py = REPO_ROOT / ".venv" / "bin" / "python"
    py_exec = str(venv_py) if venv_py.exists() else sys.executable

    print(f"Running {len(MUTATIONS)} mutations using {py_exec}...")
    results = []
    survivors = []

    # Snapshot initial bytes of all target files
    target_files = sorted(set(m.file_path for m in MUTATIONS))
    initial_bytes = {fpath: (REPO_ROOT / fpath).read_bytes() for fpath in target_files}

    for idx, m in enumerate(MUTATIONS, start=1):
        print(f"[{idx:2d}/{len(MUTATIONS)}] {m.phase} {m.mutation_id} in {m.file_path}...", end=" ", flush=True)
        try:
            caught, failing_test, failure_summary = run_mutation(m, py_exec)
        except Exception as exc:
            print(f"ERROR: {exc}")
            return 1

        status_str = "CAUGHT" if caught else "SURVIVED"
        print(f"{status_str} (test: {failing_test or 'none'})")
        results.append({
            "mutation": m,
            "caught": caught,
            "failing_test": failing_test,
            "failure_summary": failure_summary,
        })
        if not caught:
            survivors.append(m.mutation_id)

    # Verify all target files match their pre-run bytes exactly
    restoration_failed = False
    for fpath, expected_b in initial_bytes.items():
        actual_b = (REPO_ROOT / fpath).read_bytes()
        if actual_b != expected_b:
            print(f"ERROR: File {fpath} was not restored to its pre-run state!")
            restoration_failed = True

    if restoration_failed:
        return 1

    print(f"\nAll restorations verified clean. Mutations: {len(results)}, Caught: {len(results) - len(survivors)}, Survivors: {len(survivors)}")

    # Format Markdown document
    doc_lines = [
        "# §81–§88 Mutation Log",
        "",
        "This document records the automated, reproducible mutation verification discipline executed across all phases of Master Prompts §81, §82, §87 and §88.",
        "Generated by `scripts/mutation_check.py`. For every refusal rule, security barrier, and classification branch,",
        "the active enforcement line was mutated and tested against real test suites to prove that tests fail when guards are broken.",
        "",
        "| Phase | Target File | Mutation Description (Line Removed / Altered) | Test That Failed | Observed Failure Result |",
        "|---|---|---|---|---|",
    ]

    for r in results:
        m = r["mutation"]
        test_str = f"`{r['failing_test']}`" if r["failing_test"] else "*(None - SURVIVED)*"
        summary = r["failure_summary"].replace("|", "\\|")
        doc_lines.append(
            f"| **{m.phase}** | `{m.file_path}` | {m.description} | {test_str} | {summary} |"
        )

    doc_lines.append("")
    doc_content = "\n".join(doc_lines)

    if args.write_doc:
        doc_path = REPO_ROOT / "docs" / "17_MUTATION_LOG.md"
        doc_path.write_text(doc_content, encoding="utf-8")
        print(f"Updated {doc_path}")
    else:
        print("\n" + doc_content)

    return 0 if not survivors else 2


if __name__ == "__main__":
    sys.exit(main())
