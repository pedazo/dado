"""Local, deterministic evaluation of benchmark runs recorded in the registry.

This module aggregates recorded real runs with provenance or explicitly
synthetic fixtures such as those under `evaluations/`. It never executes
models or contacts the network. Reports label their data kind and refuse
mixed real/synthetic comparisons; measurement truth remains the recorder's
responsibility.

Unknown values are kept unknown. A run without a recorded cost stays an
unknown-cost run; it is excluded from cost means and reported as unknown,
never as free or zero.

``main(["report", ...])`` implements the `dado evaluate` CLI.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from .errors import DadoError, ValidationError
from .fs import atomic_write

REGISTRY_VERSION = 1
REPORT_VERSION = 1

# Benchmark case categories covered by the fixture registry.
CATEGORIES = (
    "small_edit",
    "multfile_bug",
    "transversal_change",
    "parallel_research",
    "sequential_workflow",
    "interrupted_resume",
)
RISKS = ("low", "medium", "high")
AGENT_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
BASELINE_AGENT = "single_agent"

# Score formula is emitted into every report so results stay auditable.
# Cost is deliberately excluded: runs may carry unknown cost, which cannot be
# treated as zero or free.
SCORE_FORMULA = (
    "score = 100 * acceptance_rate - 50 * false_pass_rate - 3 * missed_bugs_per_run"
    " - 0.03 * latency_mean_seconds - 1.5 * retries_mean - 0.5 * human_minutes_mean"
    " (cost excluded because it can be unknown, not free)"
)

RUN_REQUIRED_KEYS = {
    "case", "rep", "acceptance", "latency_seconds", "tokens_in", "tokens_out",
    "retries", "human_minutes", "bugs_injected", "bugs_found", "verifier_verdict",
}
RUN_OPTIONAL_KEYS = {"cost", "rework_minutes", "notes"}
RUN_KEYS = RUN_REQUIRED_KEYS | RUN_OPTIONAL_KEYS
VERIFIER_VERDICTS = ("PASS", "FAIL", "BLOCKED", "not_run")

# Real (non-synthetic) records must carry explicit provenance. Nothing real is
# imported silently: a provenance mapping with these keys is mandatory when a
# file declares synthetic: false. ``variant`` may be null to record "model
# default" explicitly; every other key must be a non-empty string or, for
# ``tool_budget``, a non-empty string or mapping.
PROVENANCE_KEYS = ("model", "variant", "harness_revision", "initial_repository", "tool_budget")


def _validate_provenance(data: dict[str, Any], source: str) -> dict[str, Any]:
    """Validate the mandatory provenance of a real (synthetic: false) record."""
    provenance = data.get("provenance")
    _require(isinstance(provenance, dict),
             f"{source}: synthetic: false requires a provenance mapping with keys {PROVENANCE_KEYS}")
    unknown = sorted(set(provenance) - set(PROVENANCE_KEYS))
    _require(not unknown, f"{source}: provenance has unknown keys: {unknown}")
    missing = [key for key in PROVENANCE_KEYS if key not in provenance]
    _require(not missing, f"{source}: provenance is missing required keys: {missing}")
    for key in PROVENANCE_KEYS:
        value = provenance[key]
        if key == "variant":
            _require(value is None or isinstance(value, str) and value.strip(),
                     f"{source}: provenance.variant must be a string or null (model default)")
        elif key == "tool_budget":
            _require((isinstance(value, str) and value.strip())
                     or (isinstance(value, dict) and bool(value)),
                     f"{source}: provenance.tool_budget must be a non-empty string or mapping "
                     "describing the tool budget context under which measurements were taken")
        else:
            _require(isinstance(value, str) and value.strip(),
                     f"{source}: provenance.{key} must be a non-empty string")
    return dict(provenance)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _number(value: Any, name: str, *, minimum: float = 0.0, allow_none: bool = False) -> Any:
    """Validate a plain number (bools rejected); ``None`` allowed for unknowns."""
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be a number")
    if value < minimum:
        raise ValidationError(f"{name} must be >= {minimum}")
    return value


# ---------------------------------------------------------------------------
# Registry and run loading (strict validation)
# ---------------------------------------------------------------------------

def load_registry(path: Path) -> dict[str, Any]:
    """Load and validate the versioned benchmark case registry."""
    path = Path(path)
    _require(path.is_file(), f"Benchmark registry not found: {path}")
    try:
        with path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except (yaml.YAMLError, OSError) as exc:
        raise ValidationError(f"Cannot read registry {path}: {exc}") from exc
    _require(isinstance(data, dict), f"Registry {path} must contain one mapping")
    _require(set(data) <= {"version", "synthetic", "cases", "provenance"},
             f"Registry {path} has unknown top-level keys: "
             f"{sorted(set(data) - {'version', 'synthetic', 'cases', 'provenance'})}")
    _require(data.get("version") == REGISTRY_VERSION, f"Registry version must be {REGISTRY_VERSION}")
    synthetic = data.get("synthetic")
    _require(isinstance(synthetic, bool), "Registry must declare an explicit boolean synthetic flag")
    provenance = None if synthetic else _validate_provenance(data, f"Registry {path}")
    cases = data.get("cases")
    _require(isinstance(cases, list) and cases, "Registry requires a non-empty cases list")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for case in cases:
        _require(isinstance(case, dict), "Each registry case must be a mapping")
        allowed = {"id", "name", "category", "description", "files_touched",
                   "lines_estimate", "read_only", "risk", "uncertainty",
                   "dependencies", "acceptance"}
        unknown = sorted(set(case) - allowed)
        _require(not unknown, f"Registry case {case.get('id')!r} has unknown keys: {unknown}")
        case_id = case.get("id")
        _require(isinstance(case_id, str) and case_id, "Registry case id must be a string")
        _require(case_id not in seen, f"Duplicate registry case id: {case_id}")
        seen.add(case_id)
        _require(isinstance(case.get("name"), str) and case["name"], f"{case_id}: name required")
        category = case.get("category")
        _require(category in CATEGORIES, f"{case_id}: category must be one of {CATEGORIES}")
        _number(case.get("files_touched"), f"{case_id}: files_touched")
        _number(case.get("lines_estimate"), f"{case_id}: lines_estimate")
        risk = case.get("risk")
        _require(risk in RISKS, f"{case_id}: risk must be one of {RISKS}")
        uncertainty = _number(case.get("uncertainty"), f"{case_id}: uncertainty")
        _require(0.0 <= uncertainty <= 1.0, f"{case_id}: uncertainty must be in [0, 1]")
        _number(case.get("dependencies"), f"{case_id}: dependencies")
        if "read_only" in case:
            _require(isinstance(case["read_only"], bool), f"{case_id}: read_only must be bool")
        acceptance = case.get("acceptance")
        _require(isinstance(acceptance, list) and acceptance and
                 all(isinstance(item, str) and item for item in acceptance),
                 f"{case_id}: acceptance must be a list of criteria strings")
        validated.append(dict(case))
    return {"version": REGISTRY_VERSION, "synthetic": synthetic,
            "provenance": provenance,
            "cases": sorted(validated, key=lambda c: c["id"])}


def _validate_run(record: Any, agent: str, source: str, *, synthetic: bool,
                  provenance: dict[str, Any] | None) -> dict[str, Any]:
    _require(isinstance(record, dict), f"{source}: each run must be a mapping")
    unknown = sorted(set(record) - RUN_KEYS)
    missing = sorted(RUN_REQUIRED_KEYS - set(record))
    _require(not unknown, f"{source}: run {record.get('case')!r} has unknown keys: {unknown}")
    _require(not missing, f"{source}: run {record.get('case')!r} is missing keys: {missing}")
    label = f"{source}:{record.get('case')!r}"
    case = record["case"]
    _require(isinstance(case, str) and case, f"{label}: case id must be a string")
    rep = record["rep"]
    _require(isinstance(rep, int) and not isinstance(rep, bool) and rep >= 1,
             f"{label}: rep must be a positive integer")
    _require(isinstance(record["acceptance"], bool), f"{label}: acceptance must be bool")
    latency = _number(record["latency_seconds"], f"{label}: latency_seconds")
    _number(record["tokens_in"], f"{label}: tokens_in")
    _number(record["tokens_out"], f"{label}: tokens_out")
    retries = record["retries"]
    _require(isinstance(retries, int) and not isinstance(retries, bool) and retries >= 0,
             f"{label}: retries must be a non-negative integer")
    _number(record["human_minutes"], f"{label}: human_minutes")
    injected = record["bugs_injected"]
    found = record["bugs_found"]
    _require(isinstance(injected, int) and not isinstance(injected, bool) and injected >= 0,
             f"{label}: bugs_injected must be a non-negative integer")
    _require(isinstance(found, int) and not isinstance(found, bool) and found >= 0,
             f"{label}: bugs_found must be a non-negative integer")
    _require(found <= injected, f"{label}: bugs_found exceeds bugs_injected")
    verdict = record["verifier_verdict"]
    _require(verdict in VERIFIER_VERDICTS, f"{label}: verifier_verdict must be one of {VERIFIER_VERDICTS}")
    # cost: strict nullability only. A number means known cost; null means
    # unknown. Strings like "unknown" or "free" are rejected so the ambiguity
    # cannot hide inside the data.
    cost = record.get("cost")
    _require(cost is None or isinstance(cost, (int, float)) and not isinstance(cost, bool),
             f"{label}: cost must be a number or null (unknown); it never implies free")
    if cost is not None and cost < 0:
        raise ValidationError(f"{label}: cost must be >= 0")
    rework = record.get("rework_minutes", 0)
    rework = _number(rework, f"{label}: rework_minutes")
    if "notes" in record:
        _require(isinstance(record["notes"], str), f"{label}: notes must be a string")
    return {
        "agent": agent,
        "synthetic": synthetic,
        "provenance": provenance,
        "case": case,
        "rep": rep,
        "acceptance": record["acceptance"],
        "latency_seconds": float(latency),
        "tokens_in": record["tokens_in"],
        "tokens_out": record["tokens_out"],
        "tokens_total": record["tokens_in"] + record["tokens_out"],
        "cost": cost,
        "cost_unknown": cost is None,
        "retries": retries,
        "human_minutes": float(record["human_minutes"]),
        "bugs_injected": injected,
        "bugs_found": found,
        "missed_bugs": max(0, injected - found),
        # Independent-verifier quality: a PASS recorded against a failed or
        # incomplete run is a false PASS (a missed failure).
        "false_pass": verdict == "PASS" and not (record["acceptance"] and found == injected),
        "verifier_verdict": verdict,
        "rework_minutes": float(rework),
        "notes": record.get("notes", ""),
    }


def _load_runs_file_with_kind(path: Path) -> tuple[bool, list[dict[str, Any]]]:
    """Load one strict-validated YAML file of recorded runs for one agent.

    Returns the file's explicit synthetic flag and its validated runs. Real
    (synthetic: false) files must carry full provenance or they are rejected.
    """
    path = Path(path)
    _require(path.is_file(), f"Runs file not found: {path}")
    try:
        with path.open(encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except (yaml.YAMLError, OSError) as exc:
        raise ValidationError(f"Cannot read runs file {path}: {exc}") from exc
    _require(isinstance(data, dict), f"Runs file {path} must contain one mapping")
    _require(set(data) <= {"synthetic", "provenance", "agent", "runs"},
             f"Runs file {path} has unknown top-level keys: "
             f"{sorted(set(data) - {'synthetic', 'provenance', 'agent', 'runs'})}")
    synthetic = data.get("synthetic")
    _require(isinstance(synthetic, bool),
             f"Runs file {path} must declare an explicit boolean synthetic flag")
    provenance = None if synthetic else _validate_provenance(data, f"Runs file {path}")
    agent = data.get("agent")
    _require(isinstance(agent, str) and AGENT_PATTERN.fullmatch(agent),
             f"Runs file {path}: agent must be a lowercase identifier")
    runs = data.get("runs")
    _require(isinstance(runs, list) and runs, f"Runs file {path} requires a non-empty runs list")
    validated = [_validate_run(record, agent, str(path), synthetic=synthetic, provenance=provenance)
                 for record in runs]
    return synthetic, validated


def load_runs_file(path: Path) -> list[dict[str, Any]]:
    """Load one strict-validated YAML file of recorded runs for one agent."""
    return _load_runs_file_with_kind(path)[1]


def load_runs(directory: Path) -> list[dict[str, Any]]:
    """Load every ``*.yaml``/``*.yml`` run file from a directory (sorted).

    Mixing synthetic fixture files with real recorded files in one load is
    rejected: they are never silently averaged into one comparison.
    """
    directory = Path(directory)
    _require(directory.is_dir(), f"Runs directory not found: {directory}")
    paths = sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")])
    _require(paths, f"No run files found in {directory}")
    runs: list[dict[str, Any]] = []
    agents: set[str] = set()
    kinds: set[bool] = set()
    for path in paths:
        synthetic, file_runs = _load_runs_file_with_kind(path)
        kinds.add(synthetic)
        for run in file_runs:
            agents.add(run["agent"])
            runs.append(run)
    _require(len(agents) >= 2, f"At least two agents required for comparison, got {sorted(agents)}")
    _require(len(kinds) == 1,
             "Cannot mix synthetic and real run files in one comparison; load them separately "
             f"(got kinds: {['real' if not kind else 'synthetic' for kind in sorted(kinds)]})")
    return runs


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _case_key(runs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        grouped.setdefault(run["case"], []).append(run)
    return grouped


def _aggregate_group(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate repeated runs of one group (agent or agent+case)."""
    grouped = _case_key(runs)
    case_ids = sorted(grouped)
    per_case_rates = [_mean([1.0 if run["acceptance"] else 0.0 for run in grouped[case]]) for case in case_ids]

    def case_mean(field: str) -> float:
        return _mean([_mean([float(run[field]) for run in grouped[case]]) for case in case_ids])

    cost_means: list[float] = []
    unknown_cost_runs = 0
    for case in case_ids:
        known = [float(run["cost"]) for run in grouped[case] if not run["cost_unknown"]]
        if known:
            cost_means.append(_mean(known))
        unknown_cost_runs += sum(1 for run in grouped[case] if run["cost_unknown"])
    full_passes = [run for run in runs if run["verifier_verdict"] == "PASS"]
    false_passes = [run for run in runs if run["false_pass"]]
    run_count = len(runs)
    acceptance_rate = _mean(per_case_rates) if case_ids else 0.0
    missed = sum(run["missed_bugs"] for run in runs)
    # Cost per accepted task: every incurred attempt (including failed ones)
    # is summed, then divided by the number of accepted runs. Any unknown cost
    # keeps the whole figure unknown; it is never averaged as zero or free.
    accepted_count = sum(1 for run in runs if run["acceptance"])
    if any(run["cost_unknown"] for run in runs):
        cost_total: float | None = None
        cost_per_accepted: float | None = None
        cost_per_accepted_state = "unknown"
    elif run_count == 0:
        cost_total, cost_per_accepted, cost_per_accepted_state = None, None, "no_runs"
    elif accepted_count == 0:
        # Nothing accepted: there is no meaningful per-accepted figure, and
        # the incurred cost may still be positive — never reported as 0.
        cost_total = round(sum(float(run["cost"]) for run in runs), 4)
        cost_per_accepted, cost_per_accepted_state = None, "no_accepted"
    else:
        cost_total = round(sum(float(run["cost"]) for run in runs), 4)
        cost_per_accepted = round(sum(float(run["cost"]) for run in runs) / accepted_count, 4)
        cost_per_accepted_state = "known"
    return {
        "run_count": run_count,
        "case_count": len(case_ids),
        "acceptance_rate": round(acceptance_rate, 4),
        "latency_mean_seconds": round(case_mean("latency_seconds"), 2),
        "tokens_mean": round(case_mean("tokens_total"), 1),
        # Unknown cost is surfaced explicitly: no mean is implied and nothing
        # is treated as free.
        "cost_mean": round(_mean(cost_means), 4) if cost_means else None,
        "cost_unknown_runs": unknown_cost_runs,
        "cost_state": "unknown" if unknown_cost_runs else ("known" if cost_means else "no_cost_recorded"),
        # Total incurred cost (failed attempts included) per accepted run.
        "cost_total": cost_total,
        "accepted_runs": accepted_count,
        "cost_per_accepted": cost_per_accepted,
        "cost_per_accepted_state": cost_per_accepted_state,
        "retries_mean": round(case_mean("retries"), 3),
        "human_minutes_mean": round(case_mean("human_minutes"), 2),
        "false_pass_count": len(false_passes),
        "false_pass_rate": round(len(false_passes) / len(full_passes), 4) if full_passes else 0.0,
        "missed_bugs_total": missed,
        "missed_bugs_per_run": round(missed / run_count, 3) if run_count else 0.0,
        "rework_minutes_total": round(sum(run["rework_minutes"] for run in runs), 2),
        "score": None,  # filled by score()
    }


def score(metrics: dict[str, Any]) -> float | None:
    """Deterministic composite score; documented in ``SCORE_FORMULA``."""
    return round(
        100 * metrics["acceptance_rate"]
        - 50 * metrics["false_pass_rate"]
        - 3 * metrics["missed_bugs_per_run"]
        - 0.03 * metrics["latency_mean_seconds"]
        - 1.5 * metrics["retries_mean"]
        - 0.5 * metrics["human_minutes_mean"],
        2,
    )


def _agent_order(metrics: dict[str, Any]) -> tuple[Any, ...]:
    """Deterministic preference: quality first, then cheaper/slower controls."""
    cost = metrics["cost_mean"]
    return (
        -metrics["acceptance_rate"],
        -metrics["false_pass_rate"],
        -metrics["missed_bugs_per_run"],
        metrics["latency_mean_seconds"],
        cost if cost is not None else float("inf"),
        metrics["human_minutes_mean"],
    )


def build_report(registry: dict[str, Any], runs: list[dict[str, Any]],
                 baseline: str = BASELINE_AGENT) -> dict[str, Any]:
    """Aggregate validated runs into a report structure ready for output.

    Comparability contract: every agent must cover the same cases with the
    same rep counts, and synthetic fixture data may never be mixed with real
    recordings. Real runs carry per-agent provenance into the report.
    """
    kinds = {run["synthetic"] for run in runs}
    _require(len(kinds) == 1,
             "Cannot mix synthetic and real runs in one report; load them separately")
    synthetic = kinds.pop()
    case_ids = {case["id"] for case in registry["cases"]}
    unknown_cases = sorted({run["case"] for run in runs} - case_ids)
    _require(not unknown_cases, f"Runs reference cases missing from the registry: {unknown_cases}")
    covered = {run["case"] for run in runs}
    uncovered = sorted(case_ids - covered)
    _require(not uncovered,
             f"Registry cases without recorded runs (comparable reports cover every case): {uncovered}")
    if not synthetic:
        missing_provenance = sorted({run["agent"] for run in runs if run["provenance"] is None})
        _require(not missing_provenance, f"Real runs without provenance: {missing_provenance}")
    agents = sorted({run["agent"] for run in runs})
    _require(baseline in agents, f"Baseline agent {baseline!r} has no recorded runs")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        grouped.setdefault(run["agent"], []).append(run)
    # Comparability: same cases, same rep counts, same rep indices per agent.
    coverage = {agent: {case: sorted(run["rep"] for run in agent_runs if run["case"] == case)
                        for case in sorted({run["case"] for run in agent_runs})}
                for agent, agent_runs in grouped.items()}
    reference_agent, reference_coverage = agents[0], coverage[agents[0]]
    for agent in agents[1:]:
        _require(set(coverage[agent]) == set(reference_coverage),
                 f"Non-comparable run sets: {agent} and {reference_agent} cover different cases "
                 f"({sorted(coverage[agent])} vs {sorted(reference_coverage)}); split the report")
        for case, reps in coverage[agent].items():
            _require(reps == reference_coverage[case],
                     f"Non-comparable run sets: {agent} has reps {reps} for {case} while "
                     f"{reference_agent} has {reference_coverage[case]}")
    agent_metrics = {agent: _aggregate_group(agent_runs) for agent, agent_runs in grouped.items()}
    for metrics in agent_metrics.values():
        metrics["score"] = score(metrics)

    case_blocks: dict[str, Any] = {}
    winners: dict[str, str] = {}
    for case in case_ids:
        subset = {agent: _aggregate_group([run for run in grouped[agent] if run["case"] == case])
                  for agent in agents if any(run["case"] == case for run in grouped[agent])}
        for metrics in subset.values():
            metrics["score"] = score(metrics)
        case_blocks[case] = subset
        winner = min(subset, key=lambda agent: (_agent_order(subset[agent]), agent))
        winners[case] = winner

    def _delta(agent: str, key: str, digits: int) -> float | str:
        agent_value, base_value = agent_metrics[agent][key], agent_metrics[baseline][key]
        if agent_value is None or base_value is None:
            return "unknown"  # never assume either side is free
        return round(agent_value - base_value, digits)

    base = agent_metrics[baseline]
    comparison: dict[str, Any] = {}
    for agent in agents:
        metrics = agent_metrics[agent]
        cost_delta: float | str
        if metrics["cost_mean"] is None or base["cost_mean"] is None:
            cost_delta = "unknown"  # never assume either side is free
        else:
            cost_delta = round(metrics["cost_mean"] - base["cost_mean"], 4)
        cost_accepted_delta: float | str
        if metrics["cost_per_accepted_state"] != "known" or base["cost_per_accepted_state"] != "known":
            cost_accepted_delta = metrics["cost_per_accepted_state"]
        else:
            cost_accepted_delta = round(metrics["cost_per_accepted"] - base["cost_per_accepted"], 4)
        comparison[agent] = {
            "acceptance_delta": round(metrics["acceptance_rate"] - base["acceptance_rate"], 4),
            "latency_delta_seconds": round(metrics["latency_mean_seconds"] - base["latency_mean_seconds"], 2),
            "cost_delta_vs_baseline": cost_delta,
            "cost_per_accepted_delta_vs_baseline": cost_accepted_delta,
            "false_pass_delta": round(metrics["false_pass_rate"] - base["false_pass_rate"], 4),
            "missed_bugs_delta": round(metrics["missed_bugs_per_run"] - base["missed_bugs_per_run"], 3),
            "human_minutes_delta": round(metrics["human_minutes_mean"] - base["human_minutes_mean"], 2),
        }

    caveats = [
        "Cost entries marked unknown are excluded from means; unknown is not free.",
        SCORE_FORMULA,
    ]
    if synthetic:
        caveats.insert(0, "SYNTHETIC FIXTURE DATA: none of these numbers are real model measurements.")
    else:
        caveats.insert(0, "REAL recorded runs; provenance is attached per agent in `provenance`.")
        caveats.append("Real measurements are only comparable across identical case and rep coverage.")
    if any(m["cost_unknown_runs"] for m in agent_metrics.values()):
        caveats.append("Some recorded runs have unknown cost; their cost stays unknown in every aggregate, "
                       "including the total cost per accepted run.")
    if any(m["false_pass_count"] for m in agent_metrics.values()):
        caveats.append("False PASS counts a verifier PASS recorded against a failed or incomplete run.")
    provenance: dict[str, Any] | None = None
    if not synthetic:
        provenance = {}
        for agent in agents:
            agent_prov = next(run["provenance"] for run in grouped[agent])
            _require(agent_prov is not None, f"Real runs without provenance: {agent}")
            _require(all(run["provenance"] == agent_prov for run in grouped[agent]),
                     f"Provenance differs across runs of agent {agent}; split the report")
            provenance[agent] = agent_prov
    report = {
        "report_version": REPORT_VERSION,
        "registry_version": registry["version"],
        "synthetic": synthetic,
        "data_kind": "synthetic" if synthetic else "real",
        "provenance": provenance,
        "baseline_agent": baseline,
        "score_formula": SCORE_FORMULA,
        "agents": agent_metrics,
        "comparison_vs_baseline": comparison,
        "cases": case_blocks,
        "case_winners": winners,
        "caveats": caveats,
    }
    return report


def write_report(report: dict[str, Any], out: str) -> Path | None:
    """Write the report artifact to an explicit path (or print to stdout)."""
    if out == "-":
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return None
    path = Path(out).expanduser()
    if path.exists() and path.is_dir():
        raise ValidationError(f"Report output must be a file path, not an existing directory: {path}")
    atomic_write(path, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return path


# ---------------------------------------------------------------------------
# CLI entry (dispatch target for a later `dado evaluate ...` subcommand)
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dado evaluate",
        description="Aggregate recorded benchmark runs (synthetic fixtures or provenance-backed "
                    "real recordings) into a report; synthetic output is always labelled")
    sub = parser.add_subparsers(dest="evaluate_command", required=True)
    report = sub.add_parser("report", help="aggregate recorded runs by agent and case")
    report.add_argument("--project", help="project root (default: current directory)")
    report.add_argument("--registry", help="registry YAML (default: <project>/evaluations/registry.yaml)")
    report.add_argument("--runs", help="runs directory (default: <project>/evaluations/runs)")
    report.add_argument("--out", required=True,
                        help="explicit report artifact path (JSON), or - for stdout; never implicit")
    report.add_argument("--baseline", default=BASELINE_AGENT,
                        help=f"reference agent for comparison (default: {BASELINE_AGENT})")
    report.set_defaults(func=_cmd_report)
    return parser


def _payment(metrics: dict[str, Any]) -> str:
    if metrics["cost_per_accepted_state"] == "known":
        return f"{metrics['cost_per_accepted']}"
    if metrics["cost_per_accepted_state"] == "no_accepted":
        return "no-accepted-runs (incurred cost may still be positive)"
    return "unknown"


def _cmd_report(args: argparse.Namespace) -> int:
    root = Path(args.project) if args.project else Path.cwd()
    registry_path = Path(args.registry) if args.registry else root / "evaluations" / "registry.yaml"
    runs_dir = Path(args.runs) if args.runs else root / "evaluations" / "runs"
    registry = load_registry(registry_path)
    runs = load_runs(runs_dir)
    report = build_report(registry, runs, baseline=args.baseline)
    # Prominent data-kind banner on stdout: synthetic fixture numbers must never
    # be readable as measured results. It comes before any tabular output.
    if report["synthetic"]:
        print("SYNTHETIC DATA: every number below is synthetic fixture data; "
              "no real model was measured.")
    else:
        models = ", ".join(
            f"{agent}={prov['model']}#{prov['variant'] or 'model-default'}"
            for agent, prov in report["provenance"].items())
        print(f"REAL recorded runs (provenance: {models}).")
    path = write_report(report, args.out)
    print(f"Report: {len(registry['cases'])} benchmark cases, {len(runs)} recorded runs, "
          f"{len(report['agents'])} agents.")
    for agent, metrics in sorted(report["agents"].items()):
        print(f"  [{report['data_kind']}] {agent}: score {metrics['score']}, "
              f"acceptance {metrics['acceptance_rate']:.0%}, "
              f"latency {metrics['latency_mean_seconds']}s, cost mean {metrics['cost_mean']}, "
              f"cost per accepted {_payment(metrics)}, "
              f"false PASS {metrics['false_pass_count']}")
    print(f"Written to: {path}" if path else "Report on stdout.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """`dado evaluate ...` entry point; returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DadoError as exc:
        print(f"dado: error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"dado: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
