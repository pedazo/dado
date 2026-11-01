from __future__ import annotations

import json

import pytest
import yaml

from dado import policy
from dado.errors import DadoError
from dado.policy import BudgetExhaustedError, BudgetLedger


def _base(**overrides) -> dict:
    context = {
        "category": "small_edit", "files_touched": 1, "lines_estimate": 3,
        "uncertainty": 0.1, "dependencies": 0,
        "budget": {"cost": 1.5, "time_minutes": 45, "retries": 2},
    }
    context.update(overrides)
    return context


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def test_small_clean_change_eligible_for_direct_handoff() -> None:
    decision = policy.decide(_base())
    assert decision["route"] == "direct_handoff"
    assert decision["executor"] == "worker" and decision["mode"] == "implementation"
    assert decision["size"] == "small" and decision["risk"] == "low"
    assert decision["confirmation_required"] is True
    assert any(reason.startswith("R4") for reason in decision["reasons"])


def test_read_only_lookup_goes_to_explorer_without_confirmation() -> None:
    decision = policy.decide(_base(read_only=True, files_touched=0, lines_estimate=0))
    assert decision["route"] == "direct_handoff"
    assert decision["executor"] == "explorer" and decision["mode"] == "local_small_read"
    assert decision["confirmation_required"] is False
    assert any(reason.startswith("R1") for reason in decision["reasons"])
    assert decision["budget"]["reservations"][0]["phase"] == "exploration"


def test_category_floor_never_drops_conservative_routing() -> None:
    expected = {
        "multfile_bug": "tracked_small_edit",
        "transversal_change": "full_dado",
        "parallel_research": "full_dado",
        "sequential_workflow": "full_dado",
        "interrupted_resume": "tracked_small_edit",
    }
    for category, route in expected.items():
        decision = policy.decide(_base(category=category, read_only=False))
        assert decision["route"] == route, category
    research = policy.decide(_base(category="parallel_research"))
    assert any(reason.startswith("R7") for reason in research["reasons"])


def test_risk_size_uncertainty_and_dependencies_push_routing_up() -> None:
    # A small change with moderate risk/uncertainty keeps tracked guarantees (R5).
    moderate = policy.decide(_base(risk="medium", uncertainty=0.5))
    assert moderate["route"] == "tracked_small_edit"
    assert any(reason.startswith("R5") for reason in moderate["reasons"])
    # R4 fails with elevated risk/uncertainty/deps, so it conservatively routes up.
    assert policy.decide(_base(risk="high"))["route"] == "full_dado"
    assert policy.decide(_base(uncertainty=0.9))["route"] == "full_dado"
    assert policy.decide(_base(dependencies=2))["route"] == "full_dado"
    large = policy.decide(_base(category="small_edit", files_touched=9, lines_estimate=400))
    assert large["route"] == "full_dado"
    assert any(reason.startswith("R8") for reason in large["reasons"])


def test_decisions_are_pure_serializable_and_explainable() -> None:
    decision = policy.decide(_base())
    # The policy decides only; it spawns nothing and never touches the network.
    assert json.loads(json.dumps(decision)) == decision
    assert all(isinstance(reason, str) for reason in decision["reasons"])
    assert decision["pending_real_evaluation"] is True


# ---------------------------------------------------------------------------
# Budget ledger semantics
# ---------------------------------------------------------------------------

def test_ledger_reservations_hold_and_charges_release_capacity() -> None:
    ledger = BudgetLedger(cost=10.0, time_minutes=60, retries=2)
    reservation = ledger.reserve("planning", cost=2.0, time_minutes=10)
    assert ledger.status()["cost"]["reserved_known"] == 2.0
    charge = ledger.charge(reservation["id"], cost=1.5, time_minutes=8, retries=1)
    assert charge["cost_state"] == "known"
    assert ledger.release(reservation["id"]) is False  # already charged
    status = ledger.status()
    assert status["cost"]["remaining_known"] == pytest.approx(10.0 - 1.5)
    assert status["retries"]["spent"] == 1
    assert ledger.release("RES-999") is False


def test_ledger_enforces_known_caps() -> None:
    ledger = BudgetLedger(cost=5.0, time_minutes=30, retries=1)
    with pytest.raises(BudgetExhaustedError):
        ledger.reserve("planning", cost=6.0, time_minutes=1)
    ledger.reserve("planning", cost=4.0, time_minutes=10)
    ledger.reserve("execution", cost=1.0, time_minutes=10)
    with pytest.raises(BudgetExhaustedError):
        ledger.reserve("escalation", cost=0.5, time_minutes=1)
    with pytest.raises(DadoError):
        ledger.reserve("retry-work", retries=2)
    with pytest.raises(DadoError):
        ledger.charge("RES-missing", cost=0.0)


def test_unknown_cost_is_tracked_as_unknown_never_as_free() -> None:
    ledger = BudgetLedger()  # no cost cap given: unknown, not unlimited
    reservation = ledger.reserve("execution", cost=None, time_minutes=5)
    assert reservation["cost_state"] == "unknown"
    status = ledger.status()
    assert status["cost"]["state"] == "unknown"
    assert status["cost"]["remaining_known"] is None
    assert status["cost"]["note"] == policy.COST_NOTE
    assert "not free" in status["cost"]["note"]
    # An unknown amount on a reservation stuck against a known cap blocks the cap math.
    capped = BudgetLedger(cost=3.0)
    unknown = capped.reserve("execution", cost=None)
    with pytest.raises(BudgetExhaustedError):
        capped.charge(unknown["id"], cost=4.0)  # known charge beyond the cap


def test_charge_replaces_held_reservation_without_double_counting() -> None:
    # Regression: a held reservation of 0.9 against a cap of 1.0 followed by a
    # charge of 0.7 used to reject (0.9 + 0.7 = 1.6 > 1.0) because the charge
    # check saw the reservation and the actuals together. Charging replaces the
    # reservation atomically instead.
    ledger = BudgetLedger(cost=1.0, time_minutes=60, retries=5)
    reservation = ledger.reserve("execution", cost=0.9, time_minutes=10)
    charge = ledger.charge(reservation["id"], cost=0.7, time_minutes=25)
    assert charge["cost_state"] == "known" and charge["cost"] == 0.7
    status = ledger.status()
    assert status["cost"]["reserved_known"] == 0.0
    assert status["cost"]["spent_known"] == pytest.approx(0.7)
    assert status["cost"]["remaining_known"] == pytest.approx(0.3)
    assert status["reservations"] == []
    # Actuals may also exceed the reservation they replace, within the cap.
    higher = ledger.reserve("retry", cost=0.1)
    charged = ledger.charge(higher["id"], cost=0.3)
    assert charged["cost"] == 0.3
    assert ledger.status()["cost"]["remaining_known"] == pytest.approx(0.0)


def test_failed_charge_restores_reservation_atomically() -> None:
    ledger = BudgetLedger(cost=1.0)
    reservation = ledger.reserve("execution", cost=0.9)
    with pytest.raises(BudgetExhaustedError):
        ledger.charge(reservation["id"], cost=1.2)  # beyond the cap
    # The reservation is restored exactly as it was held; no spend was recorded.
    status = ledger.status()
    assert [item["id"] for item in status["reservations"]] == [reservation["id"]]
    assert status["cost"]["spent_known"] == 0.0
    # The ledger is still usable afterwards.
    ledger.charge(reservation["id"], cost=1.0, time_minutes=0)
    assert ledger.status()["cost"]["remaining_known"] == pytest.approx(0.0)
    assert ledger.status()["reservations"] == []


# ---------------------------------------------------------------------------
# Finite, non-negative amount validation (no negative/nan/inf cap bypass)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["cost", "time_minutes"])
@pytest.mark.parametrize("bad", [-1, -0.001, float("nan"), float("inf"), float("-inf"), True])
def test_ledger_rejects_invalid_caps_per_field(field, bad) -> None:
    kwargs = {"cost": None, "time_minutes": None, "retries": 1}
    kwargs[field] = bad
    with pytest.raises(DadoError):
        BudgetLedger(**kwargs)


@pytest.mark.parametrize("bad", [-1, 0.5, 2.5, float("nan"), float("inf"), True, "2"])
def test_ledger_rejects_invalid_retry_caps_and_keeps_unknown(bad) -> None:
    with pytest.raises(DadoError):
        BudgetLedger(retries=bad)
    assert BudgetLedger(retries=None).status()["retries"]["cap"] is None  # unknown retries stay unknown


@pytest.mark.parametrize("kind", ["negative_cost", "nan_cost", "inf_time",
                                  "minus_inf_time", "bool_cost", "string_cost", "fractional_retries"])
def test_reserve_and_charge_reject_bad_amounts(kind) -> None:
    bad_kwargs = {"negative_cost": {"cost": -5.0},
                  "nan_cost": {"cost": float("nan")},
                  "inf_time": {"time_minutes": float("inf")},
                  "minus_inf_time": {"time_minutes": float("-inf")},
                  "bool_cost": {"cost": True},
                  "string_cost": {"cost": "0.5"},
                  "fractional_retries": {"retries": 2.5}}[kind]
    ledger = BudgetLedger(cost=10.0, time_minutes=60, retries=5)
    # Bad amounts can never enter the ledger on reservation...
    with pytest.raises(DadoError):
        ledger.reserve("phase", **bad_kwargs)
    clean = ledger.status()
    assert clean["cost"]["reserved_known"] == 0.0 and clean["reservations"] == []
    # ...or on charge (a charge of -5 must not farm budget credit).
    reservation = ledger.reserve("phase", cost=0.5, time_minutes=1.0)
    for bad_charge in ({"cost": -5.0}, {"cost": float("nan")}, {"time_minutes": float("inf")},
                       {"retries": 2.5}, {"cost": "0.5"}):
        with pytest.raises(DadoError):
            ledger.charge(reservation["id"], **bad_charge)
    status = ledger.status()
    assert status["cost"]["spent_known"] == 0.0
    assert status["reservations"][0]["id"] == reservation["id"]


def test_zero_amounts_are_valid_and_mean_zero() -> None:
    ledger = BudgetLedger(cost=0.0, time_minutes=0.0, retries=0)
    reservation = ledger.reserve("phase", cost=0.0, time_minutes=0.0, retries=0)
    charge = ledger.charge(reservation["id"], cost=0.0, time_minutes=0.0, retries=0)
    assert charge["cost_state"] == "known" and charge["cost"] == 0.0
    assert ledger.status()["cost"]["remaining_known"] == 0.0
    with pytest.raises(BudgetExhaustedError):  # zero caps are still hard caps
        ledger.reserve("other", cost=0.001, time_minutes=0.0, retries=0)


def test_nan_or_inf_caps_are_refused_via_cli(capsys) -> None:
    for bad in ("nan", "inf", "-inf"):
        rc = policy.main(["decide", "--category", "small_edit", f"--cost-budget={bad}"])
        captured = capsys.readouterr()
        assert rc == 2 and "finite" in captured.err, bad
        assert not captured.out.strip()
    rc = policy.main(["decide", "--category", "small_edit", "--uncertainty=nan"])
    captured = capsys.readouterr()
    assert rc == 2 and captured.err.startswith("dado: error:") and "finite" in captured.err


def test_bad_context_budget_amounts_are_rejected() -> None:
    for mutation in ({"cost": float("nan")}, {"cost": -1.0}, {"cost": float("inf")},
                     {"time_minutes": -5.0}, {"time_minutes": float("nan")},
                     {"retries": -1}, {"retries": 1.5}, {"retries": float("inf")},
                     {"cost": True}):
        with pytest.raises(DadoError):
            policy.decide(_base(budget=mutation))
    # The decision context gates every non-budget number against NaN/inf too.
    with pytest.raises(DadoError):
        policy.decide(_base(uncertainty=float("nan")))
    with pytest.raises(DadoError):
        policy.decide(_base(uncertainty=float("inf")))
    with pytest.raises(DadoError):
        policy.decide(_base(uncertainty=-1.0))
    with pytest.raises(DadoError):
        policy.decide(_base(files_touched=-1))
    with pytest.raises(DadoError):
        policy.decide(_base(files_touched=float("nan")))
    with pytest.raises(DadoError):
        policy.decide(_base(dependencies=float("inf")))
    with pytest.raises(DadoError):
        policy.decide(_base(escalation={"approved": True, "cap_minutes": float("nan")}))


# ---------------------------------------------------------------------------
# Policy budget and escalation
# ---------------------------------------------------------------------------

def test_policy_reserves_bounded_phases_within_declared_caps() -> None:
    direct = policy.decide(_base(budget={"cost": 2.0, "time_minutes": 45, "retries": 2}))
    assert direct["budget"]["cost"]["cap"] == 2.0 and direct["budget"]["cost"]["state"] == "known"
    assert direct["budget"]["time_minutes"]["reserved"] <= 45
    assert {item["phase"] for item in direct["budget"]["reservations"]} == {"execution"}
    tracked = policy.decide(_base(category="multfile_bug", uncertainty=0.4,
                                  budget={"cost": 2.0, "time_minutes": 45, "retries": 2}))
    phases = {item["phase"] for item in tracked["budget"]["reservations"]}
    assert phases == {"planning", "execution"}


def test_policy_flags_time_cap_too_small_for_conservative_defaults() -> None:
    decision = policy.decide(_base(category="multfile_bug", uncertainty=0.4,
                                   budget={"time_minutes": 15, "retries": 2}))
    reservations = decision["budget"]["reservations"]
    assert sum(item["time_minutes"] for item in reservations) <= 15
    # Direct handoffs reserve a single short phase, so no warning fires there.
    assert any("exceed the declared" in warning for warning in decision["warnings"])


def test_policy_missing_cost_is_explicitly_unknown_not_free() -> None:
    decision = policy.decide(_base(budget={}))
    assert decision["budget"]["cost"]["state"] == "unknown"
    assert any("unknown, not free" in warning for warning in decision["warnings"])
    assert any(item.get("cost") is None for item in decision["budget"]["reservations"])


def test_escalation_requires_explicit_opt_in_with_cap() -> None:
    ignored = policy.decide(_base(escalation={"approved": False, "cap_minutes": 10}))
    assert ignored["escalation"]["approved"] is False
    assert any("without approval" in warning for warning in ignored["warnings"])

    approved = policy.decide(_base(escalation={"approved": True, "cap_minutes": 30,
                                               "extra_retries": 1}))
    assert approved["escalation"]["approved"] is True
    assert approved["escalation"]["cap"] == {"cap_cost": None, "cap_minutes": 30.0, "extra_retries": 1}
    assert "explicit human opt-in" in approved["escalation"]["reason"]


def test_unknown_context_and_budget_keys_are_rejected() -> None:
    with pytest.raises(DadoError, match="unknown keys"):
        policy.decide(_base(spawn_agents=True))
    with pytest.raises(DadoError, match="unknown keys"):
        policy.decide(_base(budget={"cost": 1.0, "delegate": "all"}))
    with pytest.raises(DadoError, match="uncertainty"):
        policy.decide(_base(uncertainty=1.5))


def test_calibration_only_real_measurements_lift_the_pending_flag() -> None:
    synthetic = policy.decide(_base(calibration={"synthetic": True}))
    assert synthetic["pending_real_evaluation"] is True
    assert any("synthetic" in warning for warning in synthetic["warnings"])
    real = policy.decide(_base(calibration={"synthetic": False}))
    assert real["pending_real_evaluation"] is False
    assert any(reason.startswith("R9") for reason in real["reasons"])


# ---------------------------------------------------------------------------
# CLI entry (`dado policy ...` later dispatch target)
# ---------------------------------------------------------------------------

def test_main_prints_yaml_decision(tmp_path, capsys) -> None:
    rc = policy.main(["decide", "--category", "small_edit", "--files", "1", "--lines", "3",
                      "--uncertainty", "0.05", "--cost-budget", "1.5"])
    assert rc == 0
    decision = yaml.safe_load(capsys.readouterr().out)
    assert decision["route"] == "direct_handoff"
    assert decision["budget"]["cost"]["cap"] == 1.5


def test_main_supports_json_escalation_and_errors(capsys) -> None:
    rc = policy.main(["decide", "--read-only", "--json",
                      "--escalation-approved", "--escalation-cap", "10"])
    decision = json.loads(capsys.readouterr().out)
    assert rc == 0 and decision["executor"] == "explorer"
    assert decision["escalation"]["approved"] is True

    rc = policy.main(["decide", "--category", "small_edit", "--uncertainty", "3.0"])
    captured = capsys.readouterr()
    assert rc == 2 and captured.err.startswith("dado: error: ")


def test_classify_size_boundaries() -> None:
    assert policy.classify_size(1, 3) == "small"
    assert policy.classify_size(2, 40) == "small"
    assert policy.classify_size(3, 50) == "medium"
    assert policy.classify_size(6, 50) == "large"
    assert policy.classify_size(1, 300) == "large"
