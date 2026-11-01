import pytest
import yaml

from dado.budget import initialize, require_reservation, status, update
from dado.errors import DadoError, ValidationError


def test_budget_replays_after_restart_and_admits_reserved_task(tmp_path):
    path = tmp_path / "budget.yaml"
    initialize(path, cost=1, time_minutes=30, retries=2)
    with pytest.raises(ValidationError, match="reservation"):
        require_reservation(path, "T-001")
    reserved = update(path, "reserve", phase="T-001", cost=.9, time_minutes=10, retries=1)
    require_reservation(path, "T-001")
    update(path, "charge", reservation_id=reserved["result"]["id"], cost=.7, time_minutes=5, retries=0)
    with pytest.raises(ValidationError, match="reservation"):
        require_reservation(path, "T-001")
    assert status(path)["cost"]["spent_known"] == .7


def test_unknown_or_over_cap_reservations_leave_ledger_unchanged(tmp_path):
    path = tmp_path / "budget.yaml"
    initialize(path, cost=0, time_minutes=30, retries=0)
    before = path.read_bytes()
    for cost in (None, 1):
        with pytest.raises(DadoError):
            update(path, "reserve", phase="T-001", cost=cost)
        assert path.read_bytes() == before


@pytest.mark.parametrize("bad_arguments, needle", [
    ({"cost": -5.0}, ">= 0"),                       # credit farming attempt
    ({"cost": float("nan")}, "finite"),
    ({"cost": float("inf")}, "finite"),
    ({"time_minutes": float("nan")}, "finite"),
    ({"time_minutes": -10.0}, ">= 0"),
    ({"retries": 2.5}, "integer"),
    ({"retries": float("inf")}, "finite"),
    ({"cost": True}, "non-negative"),
])
def test_invalid_update_arguments_leave_bytes_unchanged(tmp_path, bad_arguments, needle):
    """Invalid ledger updates are refused before any persistence happens."""
    path = tmp_path / "budget.yaml"
    initialize(path, cost=1.0, time_minutes=60, retries=3)
    reserved = update(path, "reserve", phase="T-001", cost=0.9, time_minutes=10, retries=1)
    boundary = path.read_bytes()
    arguments = {"reservation_id": reserved["result"]["id"],
                 "cost": 0.0, "time_minutes": 1, "retries": 0}
    arguments.update(bad_arguments)
    with pytest.raises(DadoError, match=needle):
        update(path, "charge", **arguments)
    reserve_arguments = {"phase": "T-002", "cost": 0.0, "time_minutes": 1, "retries": 0}
    reserve_arguments.update(bad_arguments)
    with pytest.raises(DadoError):
        update(path, "reserve", **reserve_arguments)
    assert path.read_bytes() == boundary
    # The held reservation survives untouched and the ledger stays operational.
    update(path, "charge", reservation_id=reserved["result"]["id"], cost=0.7, time_minutes=5, retries=0)
    assert status(path)["cost"]["spent_known"] == 0.7


@pytest.mark.parametrize("caps", [
    {"cost": float("nan"), "time_minutes": 30, "retries": 2},
    {"cost": -1.0, "time_minutes": 30, "retries": 2},
    {"cost": True, "time_minutes": 30, "retries": 2},
    {"cost": 1.0, "time_minutes": float("inf"), "retries": 2},
    {"cost": 1.0, "time_minutes": 30, "retries": -1},
    {"cost": 1.0, "time_minutes": 30, "retries": 2.5},
])
def test_invalid_caps_never_create_a_ledger_file(tmp_path, caps):
    path = tmp_path / "budget.yaml"
    with pytest.raises(DadoError):
        initialize(path, **caps)
    assert not path.exists()  # invalid caps must not leave a half-initialized ledger


def test_corrupt_persisted_ledger_is_refused_without_writes(tmp_path):
    """Replay validation also guards persisted files: bad caps or events raise."""
    path = tmp_path / "budget.yaml"
    # Corrupt caps (NaN) survive a round-trip in YAML float notation (.nan).
    corrupt = {"version": 1, "caps": {"cost": float("nan"), "time_minutes": 30, "retries": 2},
               "events": []}
    path.write_text(yaml.safe_dump(corrupt), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(DadoError, match="finite"):
        status(path)
    with pytest.raises(DadoError):
        update(path, "reserve", phase="T-001", cost=0.5, time_minutes=1)
    assert path.read_bytes() == before
    # A persisted event carrying a negative credit-farming charge is refused on replay.
    poisoned = {"version": 1, "caps": {"cost": 1.0, "time_minutes": 30, "retries": 2},
                "events": [{"action": "reserve", "arguments": {"phase": "T-001",
                                                               "cost": 0.9, "time_minutes": 10,
                                                               "retries": 1}},
                           {"action": "charge", "arguments": {"reservation_id": "RES-001",
                                                              "cost": -5.0,
                                                              "time_minutes": 1, "retries": 0}}]}
    path.write_text(yaml.safe_dump(poisoned), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(DadoError, match=">= 0"):
        status(path)
    assert path.read_bytes() == before
