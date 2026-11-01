"""Pure, explainable routing and budget policy for DADO decisions.

This module is deliberately side-effect-free: it decides and explains, and it
never spawns agents, subprocesses or orchestrations. Inputs are plain dicts so
`dado work`/`dado evaluate` integrations can wire it up later, and the output
is a serializable decision record with every rule that fired.

Cost semantics: a missing cost budget is recorded as *unknown*, never as free
or unlimited. Reservations hold declared capacity while a phase runs; unknown
costs stay unknown through every aggregate.

Routing defaults are conservative until a real (non-synthetic) evaluation
calibration exists; see ``pending_real_evaluation`` in every decision.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any

import yaml

from .errors import DadoError, ValidationError

ROUTES = ("direct_handoff", "tracked_small_edit", "full_dado")
ROUTE_RANK = {route: rank for rank, route in enumerate(ROUTES)}
MODES = ("local_small_read", "implementation")
EXECUTORS = ("explorer", "worker", "master")
CATEGORIES = ("small_edit", "multfile_bug", "transversal_change",
              "parallel_research", "sequential_workflow", "interrupted_resume")
RISKS = ("low", "medium", "high")

# Representative risk per benchmark category when none is supplied explicitly.
CATEGORY_RISK = {
    "small_edit": "low",
    "multfile_bug": "medium",
    "transversal_change": "high",
    "parallel_research": "low",
    "sequential_workflow": "medium",
    "interrupted_resume": "medium",
}
# Conservative route floors: a category can push a decision up, never down.
CATEGORY_ROUTE_FLOOR = {
    "multfile_bug": "tracked_small_edit",
    "transversal_change": "full_dado",
    "parallel_research": "full_dado",
    "sequential_workflow": "full_dado",
    "interrupted_resume": "tracked_small_edit",
}
DEFAULT_TIME_CAPS = {"direct_handoff": 30.0, "tracked_small_edit": 45.0, "full_dado": 90.0}
DEFAULT_RETRIES = {"direct_handoff": 1, "tracked_small_edit": 2, "full_dado": 3}
PLANNING_MINUTES = {"direct_handoff": 0.0, "tracked_small_edit": 10.0, "full_dado": 20.0}
COST_NOTE = "missing cost is unknown, not free"


class BudgetExhaustedError(DadoError):
    """A declared budget cap is exhausted or overcommitted."""


def _budget_number(value: Any, name: str, *, integer: bool = False,
                   allow_none: bool = True) -> float | int | None:
    """Shared budget-amount validation: finite, non-negative, never a bool.

    ``None`` is allowed only where callers pass it deliberately (unknown cost
    or unknown cap); NaN and ±inf are rejected everywhere so a single NaN can
    never poison or bypass a cap comparison.
    """
    if value is None:
        if allow_none:
            return None
        raise ValidationError(f"{name} must be a non-negative {'integer' if integer else 'number'}, not None")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be a non-negative {'integer' if integer else 'number'}")
    if not math.isfinite(value):
        raise ValidationError(f"{name} must be a finite number (NaN/±inf rejected)")
    if value < 0:
        raise ValidationError(f"{name} must be >= 0")
    if integer and isinstance(value, float):
        # A float integral value (2.0) still violates retry integer accounting.
        raise ValidationError(f"{name} must be an integer, got {value}")
    return value


# ---------------------------------------------------------------------------
# Budget ledger with reservation semantics
# ---------------------------------------------------------------------------

class BudgetLedger:
    """In-memory reservation ledger for cost/time/retry budgets.

    A cap of ``None`` means the capacity is *unknown*. Unknown capacity (and
    unknown-cost reservations or charges) are tracked explicitly as unknown;
    they are never reported as zero, unlimited or free. ``reserve`` and
    ``charge`` validate known amounts against known caps and raise
    ``BudgetExhaustedError`` when a cap would be exceeded.
    """

    def __init__(self, *, cost: float | None = None, time_minutes: float | None = None,
                 retries: int | None = 1) -> None:
        # Caps may be None (unknown), but every present amount is validated as
        # finite and non-negative; bools are rejected. Unknown capacity is
        # tracked explicitly as unknown, never unlimited, free or zero.
        self.cost_cap = _budget_number(cost, "budget.cost")
        self.time_cap = _budget_number(time_minutes, "budget.time_minutes")
        self.retries_cap = None if retries is None else _budget_number(retries, "budget.retries", integer=True)
        self._reservations: list[dict[str, Any]] = []
        self._spent: list[dict[str, Any]] = []
        # Unknown means "not recorded", never zero: any explicit unknown cost
        # (or an unknown cap) marks the whole cost dimension as unknown.
        self._unknown_cost = cost is None

    # -- internals ----------------------------------------------------------
    def _known_reserved(self, key: str) -> float:
        return float(sum(item[key] for item in self._reservations if item[key] is not None))

    def _known_spent(self, key: str) -> float:
        return float(sum(item[key] for item in self._spent if item[key] is not None))

    def _check(self, label: str, cost: float | None, time_minutes: float | None,
               retries: int | None) -> None:
        # Receipt-level validation: every amount re-entering the check, even
        # from persisted replays, must be finite and non-negative.
        cost = _budget_number(cost, f"{label} cost")
        _budget_number(time_minutes, f"{label} time_minutes")
        if retries is not None:
            _budget_number(retries, f"{label} retries", integer=True, allow_none=False)
        for name, cap, amount in (("cost", self.cost_cap, cost),
                                  ("time_minutes", self.time_cap, time_minutes)):
            if cap is None or amount is None:
                continue  # unknown capacity stays unknown; never treated as free
            knowntotal = self._known_reserved(name) + self._known_spent(name)
            if knowntotal + amount > cap + 1e-9:
                raise BudgetExhaustedError(
                    f"{label}: {name} budget exhausted or overcommitted "
                    f"(cap {cap}, committed {knowntotal}, additionally requested {amount})")
        if self.retries_cap is not None:
            total = self._known_reserved("retries") + self._known_spent("retries") + (retries or 0)
            if total > self.retries_cap:
                raise BudgetExhaustedError(
                    f"{label}: retries budget exhausted (cap {self.retries_cap}, "
                    f"committed {total})")

    # -- reservation API ----------------------------------------------------
    _UNSET: Any = object()  # sentinel: "no cost committed here", not "cost unknown"

    def reserve(self, phase: str, *, cost: float | None = _UNSET,
                time_minutes: float = 0.0, retries: int = 0) -> dict[str, Any]:
        """Hold capacity for a phase; an explicit ``cost=None`` stays unknown."""
        self._require_phase(phase)
        explicit_unknown = cost is None
        if cost is self._UNSET:
            cost = None  # no cost committed here: cost_state "none"
        elif not explicit_unknown:
            _budget_number(cost, f"reserve({phase}) cost", allow_none=False)
        _budget_number(time_minutes, f"reserve({phase}) time_minutes")
        retries = _budget_number(retries, f"reserve({phase}) retries", integer=True)
        reservation = {
            "id": f"RES-{len(self._reservations) + 1:03d}",
            "phase": phase,
            "cost": None if explicit_unknown or cost is None else cost,
            "cost_state": "unknown" if explicit_unknown else ("none" if cost is None else "known"),
            "time_minutes": time_minutes,
            "retries": retries,
        }
        self._check(f"reserve({phase})", cost, time_minutes, retries)
        if explicit_unknown:
            self._unknown_cost = True
        self._reservations.append(reservation)
        return reservation

    # -- get-level helpers ---------------------------------------------------
    @staticmethod
    def _require_phase(phase: Any) -> str:
        if not isinstance(phase, str) or not phase.strip():
            raise ValidationError("budget phase must be a non-empty string")
        return phase

    @staticmethod
    def _require_reservation_id(reservation_id: Any) -> str:
        if not isinstance(reservation_id, str) or not reservation_id.strip():
            raise ValidationError("reservation id must be a non-empty string")
        return reservation_id

    def charge(self, reservation_id: str, *, cost: float | None = _UNSET,
               time_minutes: float = 0.0, retries: int = 0) -> dict[str, Any]:
        """Replace a held reservation with actuals atomically.

        The reservation's committed amounts are removed in the same check that
        validates the charged amounts, so held capacity is never counted twice.
        If the cap check fails the reservation is restored untouched.
        """
        index = next((item for item, reservation in enumerate(self._reservations)
                      if reservation["id"] == reservation_id), None)
        if index is None:
            raise ValidationError(f"Unknown reservation: {reservation_id}")
        reservation = self._reservations[index]
        self._require_reservation_id(reservation_id)
        explicit_unknown = cost is None
        if cost is not self._UNSET and not explicit_unknown:
            _budget_number(cost, f"charge({reservation_id}) cost", allow_none=False)
        _budget_number(time_minutes, f"charge({reservation_id}) time_minutes")
        retries = _budget_number(retries, f"charge({reservation_id}) retries", integer=True)
        charged_cost = None if cost is self._UNSET else cost
        charged = {"id": reservation_id, "phase": reservation["phase"],
                   "cost": charged_cost,
                   "cost_state": "unknown" if explicit_unknown
                   else ("none" if charged_cost is None else "known"),
                   "time_minutes": time_minutes, "retries": retries}
        # Atomic replacement: drop the reservation *before* the cap check so its
        # amounts are not double-counted, and restore it verbatim on failure.
        self._reservations.pop(index)
        try:
            self._check(f"charge({reservation_id})", charged_cost, time_minutes, retries)
        except BaseException:
            self._reservations.insert(index, reservation)
            raise
        if explicit_unknown:
            self._unknown_cost = True
        self._spent.append(charged)
        return charged

    def release(self, reservation_id: str) -> bool:
        """Drop an unused reservation without recording spend."""
        kept = [item for item in self._reservations if item["id"] != reservation_id]
        if len(kept) == len(self._reservations):
            return False
        self._reservations = kept
        return True

    def status(self) -> dict[str, Any]:
        known_reserved_cost = self._known_reserved("cost")
        known_spent_cost = self._known_spent("cost")
        return {
            "cost": {
                "cap": self.cost_cap,
                "state": "unknown" if self._unknown_cost else "known",
                "reserved_known": None if self._unknown_cost else known_reserved_cost,
                "spent_known": None if self._unknown_cost else known_spent_cost,
                "remaining_known": (None if self._unknown_cost or self.cost_cap is None
                                    else round(self.cost_cap - known_reserved_cost - known_spent_cost, 4)),
                "note": COST_NOTE if self._unknown_cost else None,
            },
            "time_minutes": {
                "cap": self.time_cap,
                "reserved": self._known_reserved("time_minutes"),
                "spent": self._known_spent("time_minutes"),
            },
            "retries": {
                "cap": self.retries_cap,
                "reserved": self._known_reserved("retries"),
                "spent": self._known_spent("retries"),
            },
            "reservations": list(self._reservations),
        }


# ---------------------------------------------------------------------------
# Decision policy
# ---------------------------------------------------------------------------

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _lookup_option(name: str, value: Any, options: tuple[str, ...]) -> str:
    _require(isinstance(value, str) and value in options,
             f"{name} must be one of {options}")
    return value


def _nonneg(value: Any, name: str, default: Any = None, integer: bool = False) -> Any:
    """Decision-context amount validation; delegates to the ledger validator.

    NaN and ±inf are rejected here as well, so a NaN CLI cost cap can neither
    bypass cap math nor poison persisted ledger state.
    """
    if value is None:
        return default
    return _budget_number(value, name, integer=integer, allow_none=False)


def classify_size(files_touched: int, lines_estimate: int) -> str:
    """Bounded, explainable size bucket from file/line estimates."""
    if files_touched <= 2 and lines_estimate <= 40:
        return "small"
    if files_touched >= 6 or lines_estimate >= 300:
        return "large"
    return "medium"


def decide(context: dict[str, Any]) -> dict[str, Any]:
    """Route a change and allocate a bounded budget, with reasons.

    Contract:
    - pure; no agents are spawned or contacted here — the routing *decision*
      names the executor, the orchestration layer stays outside;
    - unknown budget dimensions stay unknown (never free/unlimited);
    - escalation only applies when explicitly approved with a cap.

    Recognized context keys: ``category``, ``files_touched``, ``lines_estimate``,
    ``read_only``, ``risk``, ``uncertainty``, ``dependencies``, ``budget``
    (``cost``, ``time_minutes``, ``retries``), ``escalation`` (``approved``,
    ``cap_cost``, ``cap_minutes``, ``extra_retries``) and ``calibration``
    (baseline report data; synthetic calibration never disables the pending flag).
    """
    _require(isinstance(context, dict), "Policy context must be a mapping")
    known = {"category", "files_touched", "lines_estimate", "read_only", "risk",
             "uncertainty", "dependencies", "budget", "escalation", "calibration"}
    unknown_keys = sorted(set(context) - known)
    _require(not unknown_keys, f"Policy context has unknown keys: {unknown_keys}")

    category = context.get("category")
    _require(category in CATEGORIES or category is None, f"category must be one of {CATEGORIES}")
    files = int(_nonneg(context.get("files_touched", 0), "files_touched") or 0)
    lines = int(_nonneg(context.get("lines_estimate", 0), "lines_estimate") or 0)
    read_only = context.get("read_only", False)
    _require(isinstance(read_only, bool), "read_only must be bool")
    dependencies = int(_nonneg(context.get("dependencies", 0), "dependencies") or 0)
    risk = _lookup_option("risk", context.get("risk", CATEGORY_RISK.get(category, "medium")), RISKS)
    uncertainty = _nonneg(context.get("uncertainty", 0.5), "uncertainty", 0.5) or 0.0
    _require(0.0 <= uncertainty <= 1.0, "uncertainty must be in [0, 1]")
    size = classify_size(files, lines)
    reasons: list[str] = []
    warnings: list[str] = []

    # --- routing rules (ordered, each documented) ---
    route: str
    executor: str
    mode: str
    if read_only:
        route, executor, mode = "direct_handoff", "explorer", "local_small_read"
        reasons.append(
            f"R1 read-only changes are collected via explorer handoff (read_only=True, "
            f"files={files}, lines={lines}); no work state is touched")
        if uncertainty > 0.6:
            warnings.append("High uncertainty on a read-only lookup: treat explorer output as evidence, not conclusion.")
    else:
        category_floor = CATEGORY_ROUTE_FLOOR.get(category)
        if size == "large":
            route = "full_dado"
            reasons.append(f"R8 large change (files={files}, lines={lines}) always requires full DADO routing")
        elif category_floor:
            route = category_floor
            if category == "multfile_bug":
                reasons.append(f"R2 multi-file defect '{category}' always enters tracked or full routing on its category risk")
            else:
                reasons.append(f"R3 category '{category}' routes at minimum '{category_floor}' (conservative default pending real evaluation)")
        elif size == "small" and risk == "low" and uncertainty <= 0.30 and dependencies == 0:
            route = "direct_handoff"
            reasons.append(
                f"R4 small low-risk isolated change (size={size}, files={files}, lines={lines}, "
                f"risk=low, uncertainty={uncertainty}, deps=0) is eligible for a direct handoff")
        elif size == "small" and risk == "medium" and uncertainty <= 0.60 and dependencies <= 1:
            route = "tracked_small_edit"
            reasons.append(
                f"R5 small change with moderate risk keeps tracked guarantees "
                f"(risk={risk}, uncertainty={uncertainty}, deps={dependencies})")
        else:
            route = "full_dado"
            reasons.append(
                f"R6 uncertain or unbounded change escalates to full DADO routing "
                f"(size={size}, risk={risk}, uncertainty={uncertainty}, deps={dependencies})")
        if category_floor == "full_dado" and route == "full_dado" and category == "parallel_research":
            reasons.append(f"R7 parallel research '{category}' is delegated to explorer agents under full tracking")
        executor, mode = ("worker", "implementation") if route == "direct_handoff" else (
            ("worker", "implementation") if route == "tracked_small_edit" else ("master", "implementation"))

    # --- budget caps (unknown stays unknown) ---
    budget = context.get("budget") or {}
    _require(isinstance(budget, dict), "budget must be a mapping")
    unknown_budget_keys = sorted(set(budget) - {"cost", "time_minutes", "retries"})
    _require(not unknown_budget_keys, f"budget has unknown keys: {unknown_budget_keys}")
    cost_cap = _nonneg(budget.get("cost"), "budget.cost")
    time_cap = _nonneg(budget.get("time_minutes"), "budget.time_minutes", DEFAULT_TIME_CAPS[route])
    retries_cap = int(_nonneg(budget.get("retries"), "budget.retries",
                              DEFAULT_RETRIES[route], integer=True))
    ledger = BudgetLedger(cost=cost_cap, time_minutes=time_cap, retries=retries_cap)
    if read_only:
        explorer_minutes = min(time_cap, 15.0) if time_cap is not None else 15.0
        ledger.reserve("exploration", time_minutes=explorer_minutes)
    else:
        execution_default = {"direct_handoff": 10.0, "tracked_small_edit": 30.0, "full_dado": 60.0}[route]
        if time_cap is None:
            planning, execution = PLANNING_MINUTES[route], execution_default
        else:
            execution = min(time_cap, execution_default)
            planning = min(PLANNING_MINUTES[route], max(0.0, time_cap - execution))
        if planning > 0:
            ledger.reserve("planning", time_minutes=planning)
        ledger.reserve("execution", time_minutes=execution if time_cap is None
                       else min(time_cap, execution_default))
        if cost_cap is None:
            warnings.append(
                f"Cost budget not provided: cost stays unknown ({COST_NOTE}); spending is "
                "recorded, never implied free.")
        if time_cap is not None and time_cap < PLANNING_MINUTES[route] + execution_default:
            warnings.append(
                f"Conservative phase defaults ({PLANNING_MINUTES[route] + execution_default:.0f} min) "
                f"exceed the declared time cap ({time_cap:.0f} min); scope or budget needs an "
                "explicit decision.")
    ledger_status = ledger.status()

    # --- escalation: explicit opt-in only ---
    escalation = context.get("escalation")
    escalation_out: dict[str, Any] = {"approved": False, "cap": None, "reason": None}
    if escalation is not None:
        _require(isinstance(escalation, dict), "escalation must be a mapping")
        _require(set(escalation) <= {"approved", "cap_cost", "cap_minutes", "extra_retries"},
                 "escalation has unknown keys")
        if escalation.get("approved") is True:
            cap_minutes = _nonneg(escalation.get("cap_minutes"), "escalation.cap_minutes")
            cap_cost = _nonneg(escalation.get("cap_cost"), "escalation.cap_cost")
            extra_retries = int(_nonneg(escalation.get("extra_retries", 0),
                                        "escalation.extra_retries", 0, integer=True))
            escalation_ledger = BudgetLedger(cost=cap_cost, time_minutes=cap_minutes,
                                             retries=extra_retries)
            try:
                escalation_ledger.reserve("escalation", cost=None if cap_cost is None else 0.0,
                                          time_minutes=cap_minutes or 0.0, retries=extra_retries)
            except BudgetExhaustedError as exc:
                warnings.append(f"Escalation cap rejected its own reservation: {exc}")
                escalation_out["reason"] = "declared escalation cap is exhausted"
            else:
                escalation_out = {
                    "approved": True,
                    "cap": {"cap_cost": cap_cost, "cap_minutes": cap_minutes, "extra_retries": extra_retries},
                    "reason": "explicit human opt-in with a bounded cap",
                }
        else:
            escalation_out["reason"] = "escalation is gated behind explicit approval; ignored"
            warnings.append("Escalation requested without approval; it stays disabled.")

    # --- calibration against a real evaluation report (optional) ---
    calibration = context.get("calibration")
    pending_real_evaluation = True
    if calibration is not None:
        _require(isinstance(calibration, dict), "calibration must be a mapping")
        synthetic = calibration.get("synthetic", True)
        _require(isinstance(synthetic, bool), "calibration.synthetic must be bool")
        if not synthetic:
            pending_real_evaluation = False
            reasons.append("R9 routing thresholds are calibrated by a real (non-synthetic) evaluation report")
        else:
            warnings.append("Calibration data is synthetic; thresholds stay conservative pending a real evaluation.")

    return {
        "route": route,
        "mode": mode,
        "executor": executor,
        "category": category,
        "risk": risk,
        "size": size,
        "uncertainty": uncertainty,
        "dependencies": dependencies,
        "read_only": read_only,
        "reasons": reasons,
        "warnings": warnings,
        "budget": {
            **ledger_status,
            "cost_note": COST_NOTE,
        },
        "escalation": escalation_out,
        "confirmation_required": route == "direct_handoff" and not read_only,
        "pending_real_evaluation": pending_real_evaluation,
    }


# ---------------------------------------------------------------------------
# CLI entry (dispatch target for a later `dado policy ...` subcommand)
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dado policy",
        description="Pure, explainable routing and budget decision (no orchestration)")
    sub = parser.add_subparsers(dest="policy_command", required=True)
    decide_cmd = sub.add_parser("decide", help="route a change and allocate a bounded budget")
    decide_cmd.add_argument("--category", choices=CATEGORIES)
    decide_cmd.add_argument("--files", type=int, default=0, help="estimated files touched")
    decide_cmd.add_argument("--lines", type=int, default=0, help="estimated lines touched")
    decide_cmd.add_argument("--read-only", action="store_true", help="read-only lookup; delegates to explorer")
    decide_cmd.add_argument("--risk", choices=RISKS, help="explicit risk override")
    decide_cmd.add_argument("--uncertainty", type=float, default=0.5, help="0.0..1.0")
    decide_cmd.add_argument("--deps", type=int, default=0, help="number of dependencies")
    decide_cmd.add_argument("--cost-budget", type=float, help="known cost cap; omit to keep cost unknown (never free)")
    decide_cmd.add_argument("--time-budget", type=float, help="time cap in minutes")
    decide_cmd.add_argument("--retries-budget", type=int, help="retry cap")
    decide_cmd.add_argument("--escalation-approved", action="store_true", help="explicit opt-in for escalation")
    decide_cmd.add_argument("--escalation-cap", type=float, help="escalation time cap in minutes")
    decide_cmd.add_argument("--json", action="store_true", help="print JSON instead of YAML")
    decide_cmd.set_defaults(func=_cmd_decide)
    return parser


def _cmd_decide(args: argparse.Namespace) -> int:
    context: dict[str, Any] = {
        "files_touched": args.files,
        "lines_estimate": args.lines,
        "uncertainty": args.uncertainty,
        "dependencies": args.deps,
        "budget": {},
    }
    if args.category is not None:
        context["category"] = args.category
    if args.read_only:
        context["read_only"] = True
    if args.risk is not None:
        context["risk"] = args.risk
    if args.cost_budget is not None:
        context["budget"]["cost"] = args.cost_budget
    if args.time_budget is not None:
        context["budget"]["time_minutes"] = args.time_budget
    if args.retries_budget is not None:
        context["budget"]["retries"] = args.retries_budget
    if args.escalation_approved or args.escalation_cap is not None:
        context["escalation"] = {"approved": args.escalation_approved,
                                 "cap_minutes": args.escalation_cap}
    decision = decide(context)
    payload = json.dumps(decision, indent=2, ensure_ascii=False) if args.json else yaml.safe_dump(
        decision, sort_keys=False, allow_unicode=True, width=100)
    print(payload.rstrip())
    if args.escalation_cap is not None and not args.escalation_approved:
        print("warning: escalation requested without --escalation-approved; it was ignored.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    """`dado policy ...` entry point; returns a process exit code."""
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
