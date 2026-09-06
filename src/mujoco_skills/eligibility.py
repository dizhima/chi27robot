"""Deterministic robot-assignment eligibility from a skills manifest.

This module is intentionally pure: it does not call an LLM, mutate a plan, or
compile motion. NL authoring, timeline edits, the resolver, and compiler
preflight can therefore consume the same structured decision in later M3
stages without duplicating policy.
"""

from __future__ import annotations

from typing import Any


def _allow(robot: str, operation: str, target: str | None = None,
           eligible_robots=None) -> dict[str, Any]:
    return {
        "eligible": True,
        "code": "ELIGIBLE",
        "robot": robot,
        "operation": operation,
        "target": target,
        "eligible_robots": sorted(eligible_robots or [robot]),
        "details": {},
    }


def _deny(code: str, robot: str, operation: str,
          target: str | None = None, eligible_robots=None,
          details=None) -> dict[str, Any]:
    return {
        "eligible": False,
        "code": code,
        "robot": robot,
        "operation": operation,
        "target": target,
        "eligible_robots": sorted(eligible_robots or []),
        "details": dict(details or {}),
    }


def _robots(manifest: dict) -> dict:
    return manifest.get("robots") or {}


def _known_robot(manifest: dict, robot: str, operation: str, target=None):
    if robot in _robots(manifest):
        return None
    return _deny("UNKNOWN_ROBOT", robot, operation, target)


def supports_operation(manifest: dict, robot: str, operation: str) -> dict:
    """Check a generated primitive against the robot descriptor.

    Legacy Panda descriptors have no ``supported_ops`` and remain permissive;
    morphology-aware descriptors are explicit and therefore authoritative.
    """
    unknown = _known_robot(manifest, robot, operation)
    if unknown:
        return unknown
    supported = _robots(manifest)[robot].get("supported_ops")
    if supported is None or operation in supported:
        eligible = [
            robot_id for robot_id, descriptor in _robots(manifest).items()
            if descriptor.get("supported_ops") is None
            or operation in descriptor.get("supported_ops", [])
        ]
        return _allow(robot, operation, eligible_robots=eligible)
    eligible = [
        robot_id for robot_id, descriptor in _robots(manifest).items()
        if descriptor.get("supported_ops") is None
        or operation in descriptor.get("supported_ops", [])
    ]
    return _deny(
        "OPERATION_UNSUPPORTED", robot, operation,
        eligible_robots=eligible,
        details={"robot_type": _robots(manifest)[robot].get("type")},
    )


def _reachability_decision(manifest: dict, robot: str, operation: str,
                           target: str, specification: dict) -> dict:
    reachable = specification.get("reachable_by")
    if reachable is None:  # backward-compatible legacy manifest
        reachable = list(_robots(manifest))
    if robot in reachable:
        return _allow(
            robot, operation, target, eligible_robots=reachable)
    reason = (specification.get("unreachable") or {}).get(robot) or {}
    details = dict(reason.get("details") or {})
    details["robot_type"] = _robots(manifest)[robot].get("type")
    default_code = (
        "OBJECT_UNREACHABLE" if operation == "pick"
        else "FACILITY_UNREACHABLE")
    return _deny(
        reason.get("code", default_code), robot, operation, target,
        eligible_robots=reachable, details=details)


def can_pick(manifest: dict, robot: str, object_name: str) -> dict:
    unknown = _known_robot(manifest, robot, "pick", object_name)
    if unknown:
        return unknown
    primitive = supports_operation(manifest, robot, "pick")
    if not primitive["eligible"]:
        return {**primitive, "target": object_name}
    object_spec = (manifest.get("objects") or {}).get(object_name)
    if object_spec is None:
        return _deny("UNKNOWN_OBJECT", robot, "pick", object_name)
    if not object_spec.get("pickable", True):
        return _deny("OBJECT_NOT_PICKABLE", robot, "pick", object_name)
    return _reachability_decision(
        manifest, robot, "pick", object_name, object_spec.get("pick") or {})


def can_place(manifest: dict, robot: str, facility: str) -> dict:
    unknown = _known_robot(manifest, robot, "place", facility)
    if unknown:
        return unknown
    primitive = supports_operation(manifest, robot, "place")
    if not primitive["eligible"]:
        return {**primitive, "target": facility}
    facility_spec = (manifest.get("facilities") or {}).get(facility)
    if facility_spec is None:
        return _deny("UNKNOWN_FACILITY", robot, "place", facility)
    place = facility_spec.get("place")
    if place is None:
        return _deny("FACILITY_NOT_PLACEABLE", robot, "place", facility)
    return _reachability_decision(
        manifest, robot, "place", facility, place)


def can_execute_skill(manifest: dict, robot: str, skill_name: str) -> dict:
    unknown = _known_robot(manifest, robot, skill_name, skill_name)
    if unknown:
        return unknown
    skill = next(
        (candidate for candidate in manifest.get("skills", [])
         if candidate.get("name") == skill_name),
        None,
    )
    if skill is None:
        return _deny("UNKNOWN_SKILL", robot, skill_name, skill_name)
    eligible = skill.get("robots") or []
    if robot in eligible:
        return _allow(
            robot, skill_name, skill_name, eligible_robots=eligible)
    return _deny(
        "ARTICULATION_UNSUPPORTED", robot, skill_name, skill_name,
        eligible_robots=eligible,
        details={
            "facility": skill.get("facility"),
            "robot_type": _robots(manifest)[robot].get("type"),
        },
    )


def validate_assignment(manifest: dict, robot: str, step: dict) -> dict:
    """Return the single structured M3 decision for one assigned step."""
    operation = step.get("op")
    if operation == "pick":
        return can_pick(manifest, robot, step.get("object"))
    if operation == "place":
        return can_place(manifest, robot, step.get("dest"))

    skill_name = (
        step.get("name") or step.get("skill")
        if operation == "skill"
        else operation
    )
    if any(skill.get("name") == skill_name
           for skill in manifest.get("skills", [])):
        return can_execute_skill(manifest, robot, skill_name)

    if operation in {"open", "close"}:
        facility = step.get("facility") or step.get("target")
        facility_spec = (
            (manifest.get("facilities") or {}).get(facility) or {})
        skill_name = ((facility_spec.get("articulation") or {}).get("skills")
                      or {}).get(operation)
        if skill_name is None:
            return _deny(
                "FACILITY_ARTICULATION_UNAVAILABLE", robot, operation,
                facility)
        decision = can_execute_skill(manifest, robot, skill_name)
        return {**decision, "operation": operation, "target": facility,
                "details": {**decision["details"],
                            "required_skill": skill_name}}

    return supports_operation(manifest, robot, operation)


def _field(value, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def validate_semantic_action(manifest: dict, action) -> dict:
    """Validate one AugmentedAction without decomposing or compiling it."""
    robot = _field(action, "robot")
    operation = _field(action, "op")
    if operation == "move":
        pick = can_pick(manifest, robot, _field(action, "object"))
        if not pick["eligible"]:
            return pick
        return can_place(manifest, robot, _field(action, "dest"))
    if operation in {"open", "close"}:
        return validate_assignment(manifest, robot, {
            "op": operation,
            "facility": _field(action, "facility"),
        })
    if operation == "go_to":
        return supports_operation(manifest, robot, "navigate")
    return supports_operation(manifest, robot, operation)


def plan_assignment_violations(manifest: dict, plan: dict) -> list[dict]:
    """Validate nested or flat executable plans and return every rejection."""
    violations = []
    if not isinstance(plan, dict):
        return [_deny("INVALID_PLAN", "", "", details={
            "expected": "object"})]

    if isinstance(plan.get("tasks"), list):
        entries = []
        for task_index, task in enumerate(plan["tasks"]):
            if not isinstance(task, dict):
                continue
            task_id = task.get("task")
            task_robot = task.get("robot")
            for step_index, step in enumerate(task.get("steps") or []):
                if isinstance(step, dict):
                    entries.append((
                        task_robot or step.get("robot"), step, task_id,
                        f"tasks[{task_index}].steps[{step_index}]"))
    else:
        entries = []
        for robot, steps in plan.items():
            if robot not in _robots(manifest) or not isinstance(steps, list):
                continue
            for step_index, step in enumerate(steps):
                if isinstance(step, dict):
                    entries.append((
                        robot, step, None, f"{robot}[{step_index}]"))

    for robot, step, task_id, path in entries:
        decision = validate_assignment(manifest, robot, step)
        if decision["eligible"]:
            continue
        violations.append({
            **decision,
            "task_id": task_id,
            "step_id": step.get("id"),
            "path": path,
        })
    return violations


def semantic_assignment_violations(manifest: dict, actions) -> list[dict]:
    violations = []
    for index, action in enumerate(actions or []):
        decision = validate_semantic_action(manifest, action)
        if decision["eligible"]:
            continue
        violations.append({
            **decision,
            "action_id": _field(action, "id"),
            "path": f"actions[{index}]",
        })
    return violations


def format_eligibility_rejection(decision: dict, manifest: dict) -> str:
    """Stable English message shared by NL fallback, edits, and compiler."""
    robot = str(decision.get("robot") or "the selected robot")
    target = decision.get("target")
    object_spec = (manifest.get("objects") or {}).get(target, {})
    facility_spec = (manifest.get("facilities") or {}).get(target, {})
    target_label = (
        object_spec.get("label") or facility_spec.get("label") or target
        or "the requested target")
    details = decision.get("details") or {}
    robot_type = str(details.get("robot_type") or
                     (_robots(manifest).get(robot) or {}).get("type") or
                     "robot")
    code = decision.get("code")
    eligible = decision.get("eligible_robots") or []
    alternatives = (
        f" Eligible robots: {', '.join(eligible)}."
        if eligible else " No robot in this scene is eligible."
    )
    if code == "FACILITY_ABOVE_REACHABLE_HEIGHT":
        reason = (
            f"the {target_label} placement is above the {robot_type} "
            "robot's reachable height")
    elif code == "OBJECT_ABOVE_REACHABLE_HEIGHT":
        reason = (
            f"the {target_label} is above the {robot_type} robot's "
            "reachable height")
    elif code == "ARTICULATION_UNSUPPORTED":
        reason = f"the {robot_type} robot cannot execute this articulation"
    elif code == "OPERATION_UNSUPPORTED":
        reason = (
            f"the {robot_type} robot does not support "
            f"{decision.get('operation')}")
    elif code == "FACILITY_ARTICULATION_UNAVAILABLE":
        reason = f"the {target_label} has no executable articulation skill"
    else:
        reason = f"{target_label} is not eligible for this assignment"
    return f"Cannot assign this task to {robot}: {reason}.{alternatives}"


class AssignmentEligibilityError(ValueError):
    """Expected policy rejection, distinct from malformed input or failures."""

    def __init__(self, decision: dict, manifest: dict):
        self.decision = dict(decision)
        self.message = format_eligibility_rejection(self.decision, manifest)
        super().__init__(self.message)

    def as_dict(self) -> dict:
        return {
            **self.decision,
            "code": "assignment_ineligible",
            "reason_code": self.decision.get("code"),
            "message": self.message,
        }


def require_plan_assignments(manifest: dict, plan: dict) -> None:
    violations = plan_assignment_violations(manifest, plan)
    if violations:
        raise AssignmentEligibilityError(violations[0], manifest)


def require_semantic_assignments(manifest: dict, actions) -> None:
    violations = semantic_assignment_violations(manifest, actions)
    if violations:
        raise AssignmentEligibilityError(violations[0], manifest)
