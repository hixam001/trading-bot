#!/usr/bin/env python3
"""Offline §93 R6 capacity projection; never changes or reserves a spend cap.

The counts follow the live call graph, before the guard truncates demand:
run_live_cycle.run_cycle -> LLMBrain.tick (8 displayed candidates), then
gate_candidate_staged (web/social for cleared candidates), then Thinker.think
for missing brain verdicts. run_role can try both main and Groq, including
two attempts against Groq when it is the main provider. Thesis restatement
and post-close reflection are the live runner's narrative calls; the retired
per-candidate Narrator.narrate path is not counted. Cross-pool research is
Dexscreener HTTP, not an LLM or reserve_search call.

Typical is an explicit scenario, not measured utilisation: 10% of the board
(at least one) clears all rules, a valid brain covers the first eight of
those, search's first hop succeeds with no cache reuse, a three-position
book restates every six hours, and one full close/day needs a reflection.
Worst call demand assumes every candidate clears, no usable brain verdict,
all configured fallback/search hops attempted, restatements repeatedly fail
and stay due, and the book is fully replaced each tick. The scheduler sleeps
AFTER the cycle, so a zero-duration cycle bounds ticks/day conservatively.

Token/cost columns are planning ESTIMATES, not upper bounds: prompts depend
on the snapshot and book. Output limits follow call-site budgets, input
sizes are stated assumptions, and costs mirror the repository's pricing
snapshot in llm/client.py, not current provider pricing. Health GET /models,
RPC/exit requests, API digest calls on their separate counter, and external
manual script use are outside this live-runner projection.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import math
import os
from pathlib import Path
import sys


@dataclass(frozen=True)
class ProjectionRow:
    metric: str
    provider: str
    cap_key: str | None
    cap: float | None
    typical: float
    worst: float


@dataclass(frozen=True)
class Stage:
    name: str
    provider: str
    typical_calls: int
    worst_calls: int
    typical_input_tokens: int
    stress_input_tokens: int
    typical_output_tokens: int
    stress_output_tokens: int


@dataclass(frozen=True)
class Projection:
    ticks_per_day: int
    rows: tuple[ProjectionRow, ...]
    stages: tuple[Stage, ...]
    warnings: tuple[str, ...]
    assumptions: tuple[str, ...]
    recommendations: dict[str, float]

    @property
    def llm_calls_typical(self) -> int:
        return int(sum(row.typical for row in self.rows if row.metric == "llm_calls"))

    @property
    def llm_calls_worst(self) -> int:
        return int(sum(row.worst for row in self.rows if row.metric == "llm_calls"))


def _value(settings, key, default):
    value = settings.get(key, default) if isinstance(settings, Mapping) else getattr(settings, key, default)
    return default if value is None else value


def _enabled(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _number(settings, key, default, *, positive=False) -> float:
    try:
        value = float(_value(settings, key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a finite number") from None
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{key} must be a finite {'positive ' if positive else ''}number")
    return value


def _cost(provider: str, input_tokens: float, output_tokens: float, *, peak: bool) -> float:
    # Same snapshot as backend/llm/client.py::_estimate_cost. Typical is
    # off-peak cache-miss, stress is peak cache-miss (double for DeepSeek).
    input_rate, output_rate = (0.22, 0.66) if provider == "deepseek" else (0.80, 4.00)
    multiplier = 2 if peak and provider == "deepseek" else 1
    return (input_tokens * input_rate + output_tokens * output_rate) * multiplier / 1_000_000


def project(settings, *, typical_candidates_per_tick: int | None = None,
            typical_closes_per_day: int = 1) -> Projection:
    """Project uncapped 24h demand from an env mapping or config object.

    Credentials are used only as enabled/disabled flags and are never copied
    into the result. Caps remain per-provider, exactly as spend_guard does.
    ``typical_*`` keyword arguments change scenario assumptions, never config.
    """
    ticks = math.ceil(86400 / _number(settings, "TICK_INTERVAL_SECONDS", 60, positive=True))
    candidates = int(_number(settings, "MAX_CANDIDATES_PER_TICK", 20))
    research = int(_number(settings, "RESEARCH_PER_TICK", 8))
    if candidates < 0 or research < 0:
        raise ValueError("MAX_CANDIDATES_PER_TICK and RESEARCH_PER_TICK must be non-negative")
    eligible = (min(candidates, max(1, math.ceil(candidates / 10)))
                if typical_candidates_per_tick is None
                else min(candidates, max(0, int(typical_candidates_per_tick))))
    live = str(_value(settings, "DATA_BACKEND", "mock")).strip().lower() == "live"
    main = "deepseek" if str(_value(settings, "MAIN_LLM_PROVIDER", "deepseek")).strip().lower() == "deepseek" else "groq"
    main_enabled = live and bool(_value(settings, f"{main.upper()}_API_KEY", ""))
    groq_enabled = live and bool(_value(settings, "GROQ_API_KEY", ""))
    social_enabled = live and bool(_value(settings, "SOCIAL_LLM_API_KEY", ""))
    brain_enabled = live and candidates > 0 and _enabled(_value(settings, "LLM_BRAIN", True))
    brain_chain = ([main] if main_enabled else []) + (["groq"] if groq_enabled else [])
    stages: list[Stage] = []

    def stage(name, provider, typical, worst, input_typical, input_stress, out_typical, out_stress):
        stages.append(Stage(name, provider, int(typical), int(worst), input_typical,
                            input_stress, out_typical, out_stress))

    brain_budget = int(_number(settings, "LLM_BRAIN_MAX_TOKENS", 4000)) or 200
    if brain_enabled:
        for index, provider in enumerate(brain_chain):
            stage("brain", provider, ticks if index == 0 else 0, ticks,
                  2500 + min(candidates, 8) * 160 + 3 * 200,
                  8000 + min(candidates, 8) * 600,
                  min(2000, max(brain_budget, 0)), max(brain_budget, 0))
        # Combine duplicate logical-role attempts charged to the same provider.
        if len(brain_chain) == 2 and brain_chain[0] == brain_chain[1]:
            first, second = stages[-2:]
            stages[-2:] = [Stage(first.name, first.provider, first.typical_calls,
                                first.worst_calls + second.worst_calls,
                                first.typical_input_tokens, first.stress_input_tokens,
                                first.typical_output_tokens, first.stress_output_tokens)]
    if main_enabled:
        typical_thinker = max(0, eligible - 8) if brain_enabled and brain_chain else eligible
        stage("thinker", main, ticks * typical_thinker, ticks * candidates,
              1000, 3000, 200, 200)  # Thinker omits budget; client default is 200.
        per_pass = max(0, int(_number(settings, "THESIS_RESTATE_PER_PASS", 2)))
        stale_hours = _number(settings, "THESIS_RESTATE_STALE_HOURS", 6, positive=True)
        # MAX_OPEN_POSITIONS=3 is a hardcoded live limit, not an env cap.
        positions = 3
        main_budget = int(_number(settings, f"{main.upper()}_MAX_TOKENS", 192)) or 200
        typical_restates = min(ticks * per_pass, math.ceil(24 / stale_hours) * positions)
        stage("thesis_restate", main, typical_restates, ticks * min(positions, per_pass),
              700, 1800, min(128, max(main_budget, 0)), max(main_budget, 0))
        # Initial book plus at most three newly opened positions per tick.
        # Reflection is scheduled only after a fully closed position.
        worst_reflections = positions + ticks * min(candidates, positions)
        stage("reflection", main, min(max(0, typical_closes_per_day), worst_reflections),
              worst_reflections, 500, 1500,
              min(128, max(main_budget, 0)), max(main_budget, 0))
    if social_enabled:
        stage("social", "groq", ticks * eligible, ticks * candidates,
              600, 1000, 100, 200)

    rows: list[ProjectionRow] = []
    for provider in sorted({stage.provider for stage in stages}):
        matching = [stage for stage in stages if stage.provider == provider]
        typical_calls = sum(stage.typical_calls for stage in matching)
        worst_calls = sum(stage.worst_calls for stage in matching)
        typical_input = sum(stage.typical_calls * stage.typical_input_tokens for stage in matching)
        typical_output = sum(stage.typical_calls * stage.typical_output_tokens for stage in matching)
        worst_input = sum(stage.worst_calls * stage.stress_input_tokens for stage in matching)
        worst_output = sum(stage.worst_calls * stage.stress_output_tokens for stage in matching)
        for metric, cap_key, default, typical, worst in (
            ("llm_calls", "LLM_DAILY_CALL_CAP", 2000, typical_calls, worst_calls),
            ("llm_tokens", "LLM_DAILY_TOKEN_CAP", 2_000_000,
             typical_input + typical_output, worst_input + worst_output),
            ("llm_cost_usd", "LLM_DAILY_COST_CAP_USD", 5.0,
             _cost(provider, typical_input, typical_output, peak=False),
             _cost(provider, worst_input, worst_output, peak=True)),
        ):
            rows.append(ProjectionRow(metric, provider, cap_key,
                                      _number(settings, cap_key, default), typical, worst))

    search_hops = sum(bool(_value(settings, key, "")) for key in
                      ("BRAVE_SEARCH_API_KEY", "SEARXNG_URL", "FIRECRAWL_API_KEY")) if live else 0
    rows.append(ProjectionRow("search_calls", "web", "SEARCH_DAILY_CALL_CAP",
                _number(settings, "SEARCH_DAILY_CALL_CAP", 500),
                ticks * eligible if search_hops else 0,
                ticks * candidates * search_hops))
    dex_calls = ticks * min(candidates, research) if live else 0
    rows.append(ProjectionRow("dex_research_calls", "dexscreener", None, None,
                              dex_calls, dex_calls))
    warnings: list[str] = []
    recommendations: dict[str, float] = {}
    for row in rows:
        if row.cap is None or row.cap_key is None:
            continue
        if row.typical > row.cap:
            warnings.append(f"{row.provider} typical {row.metric} {row.typical:,.2f}/day exceeds {row.cap_key}={row.cap:g}")
        elif row.worst > row.cap:
            warnings.append(f"{row.provider} worst {row.metric} {row.worst:,.2f}/day exceeds {row.cap_key}={row.cap:g}")
        if row.worst > 0:
            # Suggested values support the stress scenario, without actually
            # applying them. A provider-shared key must cover the larger row.
            recommended = (math.ceil(row.worst * 100) / 100 if row.metric == "llm_cost_usd"
                           else math.ceil(row.worst))
            recommendations[row.cap_key] = max(recommended, recommendations.get(row.cap_key, 0))
    assumptions = (
        f"Typical: {eligible}/{candidates} candidates clear rules per tick; valid brain covers up to 8; first search hop succeeds; no search-cache savings assumed.",
        "Worst calls: missing brain verdicts, all configured role/search fallbacks, all candidates clear, no cache hits, stale restatements retry each tick, three-position book replaced each tick.",
        "Narrative: typical full three-position book restates every 6h (or configured stale interval) and one close/day by default; no per-candidate narrator call in the live runner.",
        "RESEARCH_PER_TICK limits Dexscreener cross-pool HTTP only; it does not limit social/thinker/web demand.",
        "Token/cost estimates use assumed prompt lengths and the repository pricing snapshot; input tokens, current prices, and market-driven turnover need measurement. Stress cost assumes DeepSeek peak and no input-cache discount.",
        "LLM caps apply per provider (Groq main/fallback/social share a counter); search cap is global. API digests have a separate counter and are excluded.",
        "Health GET /models and RPC requests are not spend_guard LLM calls. Estimates exclude manual invocations and do not weaken deterministic exits.",
    )
    return Projection(ticks, tuple(rows), tuple(stages), tuple(warnings), assumptions, recommendations)


def render_projection(projection: Projection) -> str:
    lines = [f"Live spend projection: {projection.ticks_per_day:,} maximum ticks/day",
             "Provider | Metric | Typical/day | Worst/day (token/cost estimate) | Effective cap",
             "--- | --- | ---: | ---: | ---:"]
    for row in projection.rows:
        def display(value):
            return f"{value:,.4f}" if row.metric == "llm_cost_usd" else f"{value:,.0f}"
        lines.append(f"{row.provider} | {row.metric} | {display(row.typical)} | {display(row.worst)} | "
                     + (display(row.cap) if row.cap is not None else "not guarded here"))
    lines.append(f"Total LLM calls/day: typical {projection.llm_calls_typical:,}; worst {projection.llm_calls_worst:,}.")
    lines.extend(f"Assumption: {assumption}" for assumption in projection.assumptions)
    lines.extend(f"WARN: {warning}" for warning in projection.warnings)
    lines.append("Suggested .env capacity for the stress scenario; operator approval required, nothing changed:")
    lines.extend(f"{key}=" + (f"{value:.2f}" if key == "LLM_DAILY_COST_CAP_USD" else str(int(value)))
                 for key, value in projection.recommendations.items())
    return "\n".join(lines)


def _read_env(path: Path) -> dict[str, str]:
    """Read literal .env values only: no expansion, execution or config import."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key.strip()] = value
    return values


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="optional literal .env file; exported environment overrides it")
    args = parser.parse_args(argv)
    try:
        settings = _read_env(args.env_file) if args.env_file else {}
        settings.update(os.environ)
        print(render_projection(project(settings)))
    except (OSError, ValueError) as exc:
        # Numeric-validation errors name only the key, never its value.
        print(f"Spend projection failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
