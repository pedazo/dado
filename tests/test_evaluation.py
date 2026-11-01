from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
import yaml

from dado import evaluation
from dado.errors import DadoError, ValidationError

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "evaluations"


def _run(**overrides) -> dict:
    record = {
        "case": "CASE-small-edit", "rep": 1, "acceptance": True,
        "latency_seconds": 100, "tokens_in": 100, "tokens_out": 50, "cost": 1.0,
        "retries": 0, "human_minutes": 1, "bugs_injected": 0, "bugs_found": 0,
        "verifier_verdict": "PASS",
    }
    record.update(overrides)
    return record


def _runs_file(tmp_path: Path, agent: str, runs: list[dict]) -> Path:
    path = tmp_path / f"runs-{agent}.yaml"
    path.write_text(yaml.safe_dump({"synthetic": True, "agent": agent, "runs": runs}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Fixture registry
# ---------------------------------------------------------------------------

def test_fixture_registry_loads_and_covers_all_categories() -> None:
    registry = evaluation.load_registry(FIXTURES / "registry.yaml")
    assert registry["version"] == 1 and registry["synthetic"] is True
    categories = {case["category"] for case in registry["cases"]}
    assert categories == set(evaluation.CATEGORIES)
    assert [case["id"] for case in registry["cases"]] == sorted(case["id"] for case in registry["cases"])


@pytest.mark.parametrize("mutation", [
    {"version": 2},
    {"synthetic": False},
    {"cases": []},
])
def test_registry_rejects_invalid_shape(tmp_path, mutation) -> None:
    data = copy.deepcopy(yaml.safe_load((FIXTURES / "registry.yaml").read_text(encoding="utf-8")))
    data.update(mutation)
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(DadoError):
        evaluation.load_registry(path)


def test_registry_rejects_unknown_key_duplicate_id_and_bad_fields(tmp_path) -> None:
    base_case = {
        "id": "CASE-a", "name": "a", "category": "small_edit", "files_touched": 1,
        "lines_estimate": 1, "risk": "low", "uncertainty": 0.1, "dependencies": 0,
        "acceptance": ["criterion"],
    }
    base = {"version": 1, "synthetic": True, "cases": [base_case]}

    def with_case(case: dict) -> dict:
        return dict(base, cases=[case])

    for data in (
        dict(base, bogus=1),
        dict(base, cases=[base_case, base_case]),
        with_case({**base_case, "risk": "extreme"}),
        with_case({**base_case, "uncertainty": 1.5}),
        with_case({**base_case, "category": "spooky"}),
        with_case({**base_case, "acceptance": []}),
    ):
        path = tmp_path / "registry.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        with pytest.raises(DadoError):
            evaluation.load_registry(path)


# ---------------------------------------------------------------------------
# Strict run validation
# ---------------------------------------------------------------------------

def test_fixture_runs_load_strictly_and_pair_with_registry() -> None:
    registry = evaluation.load_registry(FIXTURES / "registry.yaml")
    runs = evaluation.load_runs(FIXTURES / "runs")
    agents = {run["agent"] for run in runs}
    assert agents == {"single_agent", "dado", "dado_variant_deep_verify"}
    assert len(runs) == 3 * 6 * 2  # three agents, six cases, two reps each
    case_ids = {case["id"] for case in registry["cases"]}
    assert {run["case"] for run in runs} == case_ids


def test_run_validation_rejects_sloppy_cost_and_bugs(tmp_path) -> None:
    agent_file = _runs_file(tmp_path, "agent_a", [_run()])
    assert evaluation.load_runs_file(agent_file)[0]["cost_unknown"] is False
    for mutation in (
        {"cost": "unknown"},          # string ambiguity must be rejected
        {"cost": 1.0, "bug": 1},      # unknown key
        {"rep": 0},
        {"acceptance": "yes"},
        {"bugs_injected": 2, "bugs_found": 3},
        {"verifier_verdict": "MAYBE"},
        {"cost": -1.0},
        {"tokens_in": True},
    ):
        path = _runs_file(tmp_path, "agent_b", [_run(**mutation)])
        with pytest.raises(DadoError):
            evaluation.load_runs_file(path)


def test_run_validation_requires_envelope_flags(tmp_path) -> None:
    path = tmp_path / "runs.yaml"
    path.write_text(yaml.safe_dump({"agent": "agent_c", "runs": [_run()]}), encoding="utf-8")
    with pytest.raises(DadoError, match="synthetic"):
        evaluation.load_runs_file(path)
    path.write_text(yaml.safe_dump({"synthetic": True, "agent": "Bad Agent", "runs": [_run()]}), encoding="utf-8")
    with pytest.raises(DadoError, match="agent"):
        evaluation.load_runs_file(path)


# ---------------------------------------------------------------------------
# Per-run metrics and aggregation
# ---------------------------------------------------------------------------

def test_evaluate_detects_false_pass_and_missed_bugs() -> None:
    run = evaluation._validate_run(_run(acceptance=False, bugs_injected=2, bugs_found=0), "x", "test", synthetic=True, provenance=None)
    assert run["false_pass"] is True
    assert run["missed_bugs"] == 2
    assert run["verifier_verdict"] == "PASS"
    clean = evaluation._validate_run(_run(bugs_injected=1, bugs_found=1), "x", "test", synthetic=True, provenance=None)
    assert clean["false_pass"] is False and clean["missed_bugs"] == 0
    fail = evaluation._validate_run(_run(verifier_verdict="FAIL"), "x", "test", synthetic=True, provenance=None)
    assert fail["false_pass"] is False


def test_unknown_cost_never_becomes_zero_or_free() -> None:
    run = evaluation._validate_run(_run(cost=None), "x", "test", synthetic=True, provenance=None)
    assert run["cost_unknown"] is True and run["cost"] is None
    metrics = evaluation._aggregate_group([run])
    assert metrics["cost_mean"] is None
    assert metrics["cost_state"] == "unknown"
    assert metrics["cost_unknown_runs"] == 1
    known = evaluation._aggregate_group([run, evaluation._validate_run(_run(rep=2, cost=3.0), "x", "test", synthetic=True, provenance=None)])
    assert known["cost_mean"] == 3.0        # only the known run is averaged
    assert known["cost_unknown_runs"] == 1  # the unknown run is flagged, never merged


def test_aggregation_uses_repeated_runs_per_case_means() -> None:
    runs = [
        evaluation._validate_run(_run(rep=1, latency_seconds=10, tokens_in=10, tokens_out=0), "x", "test", synthetic=True, provenance=None),
        evaluation._validate_run(_run(rep=2, latency_seconds=30, tokens_in=30, tokens_out=0), "x", "test", synthetic=True, provenance=None),
        evaluation._validate_run(_run(rep=1, case="CASE-multfile-bug", latency_seconds=100,
                                      tokens_in=100, tokens_out=0), "x", "test", synthetic=True, provenance=None),
    ]
    metrics = evaluation._aggregate_group(runs)
    # Rep-weighted per-case means: two reps average to 20s, one case 100s.
    assert metrics["latency_mean_seconds"] == 60.0
    assert metrics["case_count"] == 2 and metrics["run_count"] == 3


def test_score_is_deterministic_and_documented() -> None:
    metrics = evaluation._aggregate_group([evaluation._validate_run(_run(), "x", "test", synthetic=True, provenance=None)])
    first, second = evaluation.score(metrics), evaluation.score(metrics)
    assert first == second
    assert "cost" in evaluation.SCORE_FORMULA and "unknown" in evaluation.SCORE_FORMULA


# ---------------------------------------------------------------------------
# Report building and artifact writing
# ---------------------------------------------------------------------------

@pytest.fixture
def report() -> dict:
    registry = evaluation.load_registry(FIXTURES / "registry.yaml")
    runs = evaluation.load_runs(FIXTURES / "runs")
    return evaluation.build_report(registry, runs)


def test_report_aggregates_agents_comparisons_and_winners(report) -> None:
    assert report["synthetic"] is True and report["baseline_agent"] == "single_agent"
    agents = report["agents"]
    assert agents["single_agent"]["acceptance_rate"] == pytest.approx(0.5)
    assert agents["dado"]["acceptance_rate"] == 1.0
    assert agents["single_agent"]["false_pass_rate"] == pytest.approx(0.5)
    assert agents["dado"]["false_pass_count"] == 0
    assert agents["dado"]["cost_mean"] is not None
    assert agents["single_agent"]["cost_unknown_runs"] == 2
    # Fixture data: single_agent carries unknown costs, so its cost per accepted
    # run is unknown instead of silently averaged; dado stays fully known.
    assert agents["single_agent"]["cost_per_accepted_state"] == "unknown"
    assert agents["dado"]["cost_per_accepted_state"] == "known"
    assert agents["dado"]["cost_per_accepted"] == pytest.approx(4.7125)
    # CASE-multfile-bug never succeeds for single_agent: null per-accepted, but
    # the incurred total is recorded, never reported as zero.
    no_accepted = report["cases"]["CASE-multfile-bug"]["single_agent"]
    assert no_accepted["cost_per_accepted_state"] == "no_accepted"
    assert no_accepted["cost_total"] == pytest.approx(5.90)
    assert no_accepted["cost_per_accepted"] is None
    comparison = report["comparison_vs_baseline"]["dado"]
    assert comparison["acceptance_delta"] == pytest.approx(0.5)
    assert comparison["cost_delta_vs_baseline"] == pytest.approx(
        agents["dado"]["cost_mean"] - (agents["single_agent"]["cost_mean"] or 0.0), rel=1e-3)
    assert all(winner == "single_agent" or winner.startswith("dado")
               for winner in report["case_winners"].values())
    assert any("SYNTHETIC" in caveat for caveat in report["caveats"])
    assert any("not free" in caveat for caveat in report["caveats"])


def test_report_requires_baseline_and_known_cases() -> None:
    registry = evaluation.load_registry(FIXTURES / "registry.yaml")
    runs = evaluation.load_runs(FIXTURES / "runs")
    with pytest.raises(DadoError, match="Baseline"):
        evaluation.build_report(registry, runs, baseline="nobody")
    stray = [dict(run, case="CASE-not-a-thing") for run in runs]
    with pytest.raises(DadoError, match="registry"):
        evaluation.build_report(registry, stray)


def test_report_writes_only_to_explicit_path(tmp_path, capsys, report) -> None:
    out = tmp_path / "nested" / "report.json"
    returned = evaluation.write_report(report, str(out))
    assert returned == out and out.is_file()
    assert json.loads(out.read_text(encoding="utf-8")) == report
    # No implicit artifact: nothing is written when the argument is not given.
    assert not any(out.parent != tmp_path and p.name.startswith("report")
                   for p in tmp_path.iterdir())

    assert evaluation.write_report(report, "-") is None
    assert json.loads(capsys.readouterr().out) == report

    with pytest.raises(ValidationError, match="not an existing directory"):
        evaluation.write_report(report, str(tmp_path))


def test_report_is_deterministic() -> None:
    registry = evaluation.load_registry(FIXTURES / "registry.yaml")
    runs = evaluation.load_runs(FIXTURES / "runs")
    assert evaluation.build_report(registry, runs) == evaluation.build_report(registry, runs)


# ---------------------------------------------------------------------------
# CLI entry (`dado evaluate ...` later dispatch target)
# ---------------------------------------------------------------------------

def test_main_writes_report_and_supports_stdout(tmp_path, capsys) -> None:
    out = tmp_path / "report.json"
    rc = evaluation.main(["report", "--project", str(REPO), "--out", str(out)])
    assert rc == 0 and out.is_file()
    stdout = capsys.readouterr().out
    # Synthetic fixture data is announced prominently on every stdout output.
    assert stdout.splitlines()[0].startswith("SYNTHETIC DATA")
    assert stdout.count("[synthetic]") >= len(json.loads(out.read_text(encoding="utf-8"))["agents"])
    assert "36 recorded runs" in stdout

    rc = evaluation.main(["report", "--project", str(REPO), "--out", "-", "--baseline", "dado"])
    stdout = capsys.readouterr().out
    assert rc == 0
    assert stdout.splitlines()[0].startswith("SYNTHETIC DATA")
    parsed = json.loads(stdout[stdout.index("{"):stdout.index("\nReport: ")])
    assert parsed["baseline_agent"] == "dado"


def test_main_prints_real_banner_with_provenance(tmp_path, capsys) -> None:
    data = {"version": 1, "synthetic": False, "provenance": dict(REAL_PROVENANCE),
            "cases": [{**REAL_CASE, "id": "CASE-real-a"}, {**REAL_CASE, "id": "CASE-real-b"}]}
    registry_path = tmp_path / "registry-print.yaml"
    registry_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    runs_dir = tmp_path / "runs-print"
    runs_dir.mkdir()
    _real_runs(runs_dir, "real_agent_base", [_real_rep("CASE-real-a"), _real_rep("CASE-real-b")])
    _real_runs(runs_dir, "real_agent_b", [_real_rep("CASE-real-a"), _real_rep("CASE-real-b")])
    rc = evaluation.main(["report", "--project", str(tmp_path),
                          "--registry", str(registry_path), "--runs", str(runs_dir), "--out", "-",
                          "--baseline", "real_agent_base"])
    stdout = capsys.readouterr().out
    assert rc == 0
    assert stdout.splitlines()[0].startswith("REAL recorded runs (provenance:")
    assert "model-x" in stdout.splitlines()[0]
    assert stdout.count("[real]") == 2  # one tagged summary line per agent


def test_main_reports_errors_without_traceback(tmp_path, capsys) -> None:
    rc = evaluation.main(["report", "--project", str(tmp_path), "--out", str(tmp_path / "r.json")])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.err.startswith("dado: error:")
    assert not (tmp_path / "r.json").exists()


# ---------------------------------------------------------------------------
# Real (synthetic: false) records with mandatory provenance
# ---------------------------------------------------------------------------

REAL_PROVENANCE = {
    "model": "provider/model-x",
    "variant": None,
    "harness_revision": "harness-2026.11.0",
    "initial_repository": "0f3c9d1e2b4a5c6d7e8f90123456789abcdef012",
    "tool_budget": {"timeout_seconds": 600, "retries": 3},
}

REAL_CASE = {
    "id": "CASE-real-a", "name": "real case a", "category": "small_edit",
    "description": "case used by real-record tests", "files_touched": 1,
    "lines_estimate": 3, "risk": "low", "uncertainty": 0.1, "dependencies": 0,
    "acceptance": ["criterion met"],
}


def _real_registry(tmp_path: Path, provenance: dict | None = None, **case_overrides) -> Path:
    data = {"version": 1, "synthetic": False,
            "provenance": dict(provenance or REAL_PROVENANCE),
            "cases": [{**REAL_CASE, **case_overrides}]}
    path = tmp_path / "registry-real.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _real_runs(tmp_path: Path, agent: str, runs: list[dict],
               provenance: dict | None = None) -> Path:
    data = {"synthetic": False, "provenance": dict(provenance or REAL_PROVENANCE),
            "agent": agent, "runs": runs}
    path = tmp_path / f"runs-real-{agent}.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _real_rep(case: str, agent_unused: str = "", rep: int = 1, *, acceptance: bool = True,
              cost: float | None = 1.0, latency: int = 100) -> dict:
    return {
        "case": case, "rep": rep, "acceptance": acceptance, "latency_seconds": latency,
        "tokens_in": 100, "tokens_out": 50, "cost": cost, "retries": 0,
        "human_minutes": 0, "bugs_injected": 0, "bugs_found": 0,
        "verifier_verdict": "PASS",
    }


def test_real_registry_and_runs_load_with_provenance(tmp_path) -> None:
    registry = evaluation.load_registry(_real_registry(tmp_path))
    assert registry["synthetic"] is False and registry["provenance"]["model"] == "provider/model-x"
    path = _real_runs(tmp_path, "real_agent", [_real_rep("CASE-real-a"), _real_rep("CASE-real-a", rep=2)])
    runs = evaluation.load_runs_file(path)
    assert all(run["synthetic"] is False for run in runs)
    assert all(run["provenance"]["model"] == "provider/model-x" for run in runs)


@pytest.mark.parametrize("missing_key", list(evaluation.PROVENANCE_KEYS))
def test_real_records_without_provenance_are_rejected(tmp_path, missing_key) -> None:
    provenance = {k: v for k, v in REAL_PROVENANCE.items() if k != missing_key}
    registry_path = _real_registry(tmp_path / "reg", provenance=provenance)
    with pytest.raises(DadoError, match=missing_key):
        evaluation.load_registry(registry_path)
    runs_path = _real_runs(tmp_path, "real_agent", [_real_rep("CASE-real-a")], provenance=provenance)
    with pytest.raises(DadoError, match=missing_key):
        evaluation.load_runs_file(runs_path)


def test_ambiguous_synthetic_flag_is_rejected(tmp_path) -> None:
    registry_path = _real_registry(tmp_path)
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    data["synthetic"] = "no"
    registry_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(DadoError, match="explicit boolean synthetic"):
        evaluation.load_registry(registry_path)
    path = tmp_path / "runs.yaml"
    path.write_text(yaml.safe_dump({"agent": "real_agent", "runs": [_real_rep("c")]}), encoding="utf-8")
    with pytest.raises(DadoError, match="explicit boolean synthetic"):
        evaluation.load_runs_file(path)


def test_synthetic_and_real_runs_cannot_mix_silently(tmp_path) -> None:
    synthetic_dir, real_dir, mixed_dir = tmp_path / "s", tmp_path / "r", tmp_path / "m"
    for directory in (synthetic_dir, real_dir, mixed_dir):
        directory.mkdir()
    for agent in ("single_agent", "agent_two"):
        (synthetic_dir / f"syn-{agent}.yaml").write_text(
            yaml.safe_dump({"synthetic": True, "agent": agent, "runs": [_run()]}), encoding="utf-8")
        (real_dir / f"real-{agent}.yaml").write_text(
            yaml.safe_dump({"synthetic": False, "provenance": REAL_PROVENANCE,
                            "agent": agent, "runs": [_real_rep("CASE-real-a")]}), encoding="utf-8")
    shutil.copy(synthetic_dir / "syn-single_agent.yaml", mixed_dir / "synthetic.yaml")
    shutil.copy(synthetic_dir / "syn-agent_two.yaml", mixed_dir / "synthetic-two.yaml")
    shutil.copy(real_dir / "real-single_agent.yaml", mixed_dir / "real.yaml")
    shutil.copy(real_dir / "real-agent_two.yaml", mixed_dir / "real-two.yaml")
    assert len(evaluation.load_runs(synthetic_dir)) == 2
    assert len(evaluation.load_runs(real_dir)) == 2
    with pytest.raises(DadoError, match="mix"):
        evaluation.load_runs(mixed_dir)


def test_real_report_carries_provenance_and_data_kind(tmp_path) -> None:
    registry = evaluation.load_registry(_real_registry(tmp_path))
    registry = {**registry, "cases": [
        {**REAL_CASE, "id": "CASE-real-a"},
        {**REAL_CASE, "id": "CASE-real-b"},
    ]}
    runs = [
        *evaluation.load_runs_file(_real_runs(
            tmp_path, "real_agent_base",
            [_real_rep("CASE-real-a"), _real_rep("CASE-real-b", cost=3.0)])),
        *evaluation.load_runs_file(_real_runs(
            tmp_path, "real_agent_b",
            [_real_rep("CASE-real-a", cost=1.0),
             _real_rep("CASE-real-b", acceptance=False, cost=4.0)])),
    ]
    report = evaluation.build_report(registry, runs, baseline="real_agent_base")
    assert report["synthetic"] is False and report["data_kind"] == "real"
    assert report["provenance"]["real_agent_base"]["model"] == "provider/model-x"
    assert any("REAL recorded runs" in caveat for caveat in report["caveats"])
    # Cost per accepted: agent_b incurred 5.0 total (failed attempt included),
    # accepted 1 → 5.0; baseline incurred 4.0, accepted 2 → 2.0.
    agents = report["agents"]
    assert agents["real_agent_base"]["cost_per_accepted"] == pytest.approx(2.0)
    assert agents["real_agent_b"]["cost_total"] == pytest.approx(5.0)
    assert agents["real_agent_b"]["cost_per_accepted"] == pytest.approx(5.0)
    comparison = report["comparison_vs_baseline"]["real_agent_b"]
    assert comparison["cost_per_accepted_delta_vs_baseline"] == pytest.approx(3.0)


def test_real_report_rejects_inconsistent_agent_provenance(tmp_path) -> None:
    registry = evaluation.load_registry(_real_registry(tmp_path))
    base_runs = evaluation.load_runs_file(_real_runs(tmp_path, "real_agent", [_real_rep("CASE-real-a")]))
    shifted = dict(REAL_PROVENANCE, harness_revision="harness-OTHER")
    shifted_runs = evaluation.load_runs_file(
        _real_runs(tmp_path, "real_agent", [_real_rep("CASE-real-a", rep=2)], provenance=shifted))
    with pytest.raises(DadoError, match="differs across runs"):
        evaluation.build_report(registry, base_runs + shifted_runs, baseline="real_agent")


def test_report_requires_comparable_case_and_rep_coverage(tmp_path) -> None:
    """Comparability: same cases (full registry coverage) and same rep counts."""
    def synthetic_case(case_id: str) -> dict:
        return {"id": case_id, "name": case_id, "category": "small_edit",
                "files_touched": 1, "lines_estimate": 3, "risk": "low",
                "uncertainty": 0.1, "dependencies": 0, "acceptance": ["criterion"]}

    def write_registry(case_ids: list[str]) -> dict:
        data = {"version": 1, "synthetic": True,
                "cases": [synthetic_case(case_id) for case_id in case_ids]}
        path = tmp_path / f"registry-{'-'.join(case_ids)}.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return evaluation.load_registry(path)

    def runs_for(agent: str, case_ids: tuple[str, ...], reps: int = 1) -> list[dict]:
        recorded = []
        for case in case_ids:
            for rep in range(1, reps + 1):
                recorded.append(_run(case=case, rep=rep, acceptance=True, cost=1.0))
        return [evaluation._validate_run(record, agent, "test", synthetic=True, provenance=None)
                for record in recorded]

    single = write_registry(["CASE-a"])
    two_cases = write_registry(["CASE-a", "CASE-b"])
    # Same cases, same reps over the full registry: valid.
    evaluation.build_report(two_cases, runs_for("agent_a", ("CASE-a", "CASE-b"), 1)
                            + runs_for("agent_b", ("CASE-a", "CASE-b"), 1), baseline="agent_a")
    # A registry case nobody recorded: rejected rather than silently dropped.
    with pytest.raises(DadoError, match="without recorded runs"):
        evaluation.build_report(two_cases, runs_for("agent_a", ("CASE-a",))
                                + runs_for("agent_b", ("CASE-a",)), baseline="agent_a")
    # Different case coverage between agents: rejected.
    with pytest.raises(DadoError, match="different cases"):
        evaluation.build_report(two_cases, runs_for("agent_a", ("CASE-a", "CASE-b"))
                                + runs_for("agent_b", ("CASE-a",)), baseline="agent_a")
    # Different rep counts on the same case: rejected.
    with pytest.raises(DadoError, match="reps"):
        evaluation.build_report(single, runs_for("agent_a", ("CASE-a",), 2)
                                + runs_for("agent_b", ("CASE-a",), 1), baseline="agent_a")


def test_cost_per_accepted_includes_failed_attempts_and_unknown() -> None:
    # Accepted at 10, failed attempt cost 5 → total 15 / 1 accepted = 15.
    runs = [
        evaluation._validate_run(_run(rep=1, acceptance=True, cost=10.0), "x", "test",
                                 synthetic=True, provenance=None),
        evaluation._validate_run(_run(rep=2, acceptance=False, cost=5.0, verifier_verdict="FAIL"), "x", "test",
                                 synthetic=True, provenance=None),
    ]
    metrics = evaluation._aggregate_group(runs)
    assert metrics["accepted_runs"] == 1
    assert metrics["cost_total"] == pytest.approx(15.0)
    assert metrics["cost_per_accepted"] == pytest.approx(15.0)
    assert metrics["cost_per_accepted_state"] == "known"
    assert metrics["cost_mean"] == 7.5  # plain per-case mean keeps its own meaning

    # Any unknown cost keeps the whole figure unknown; never averaged as zero.
    unknown = evaluation._aggregate_group([
        evaluation._validate_run(_run(rep=1, acceptance=True, cost=None), "x", "test",
                                 synthetic=True, provenance=None)])
    assert unknown["cost_per_accepted"] is None
    assert unknown["cost_per_accepted_state"] == "unknown"

    # No accepted runs: the figure is null but the incurred cost stays positive.
    no_accepted = evaluation._aggregate_group([
        evaluation._validate_run(_run(rep=1, acceptance=False, cost=2.0, verifier_verdict="FAIL"), "x", "test",
                                 synthetic=True, provenance=None)])
    assert no_accepted["cost_total"] == pytest.approx(2.0)
    assert no_accepted["cost_per_accepted"] is None
    assert no_accepted["cost_per_accepted_state"] == "no_accepted"
