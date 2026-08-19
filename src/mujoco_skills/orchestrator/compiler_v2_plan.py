"""Compiler V2 coordination provenance and resubmission preparation.

Compiler-completed plans are user-visible and are routinely submitted again by
edit, sync, and pure-append turns.  This module separates durable user intent
from coordination artifacts that Compiler V2 is expected to regenerate.
"""

from __future__ import annotations

import copy
from typing import Any


COMPILER_V2_SOURCE = "compiler_v2"
USER_SOURCE = "user"
GENERATED_AFTER_FIELD = "compiler_v2_after"
GENERATED_VIA_FIELD = "compiler_v2_via"
REPAIR_KIND_FIELD = "compiler_v2_repair"
PROMOTED_FROM_FIELD = "promoted_from"
DETOUR_PARENT_GROUP_FIELD = "compiler_v2_parent_group"
DETOUR_OVERRIDE_FIELDS_FIELD = "compiler_v2_detour_override_fields"
DETOUR_OVERRIDDEN_FIELD = "compiler_v2_detour_overridden"
DETOUR_OVERRIDES_FIELD = "_compiler_v2_detour_overrides"


def mark_generated_step(step: dict, repair_kind: str) -> dict:
    """Mark an inserted coordination step without changing its public shape."""
    step["source"] = COMPILER_V2_SOURCE
    step[REPAIR_KIND_FIELD] = str(repair_kind)
    return step


def add_generated_after(step: dict, dependency_id: str) -> None:
    """Add an effective dependency while recording artifact-level ownership."""
    dependencies = list(dict.fromkeys([*(step.get("after") or []), dependency_id]))
    generated = list(dict.fromkeys([
        *(step.get(GENERATED_AFTER_FIELD) or []), dependency_id,
    ]))
    step["after"] = dependencies
    step[GENERATED_AFTER_FIELD] = generated


def mark_generated_via(step: dict, via_points: list | None) -> None:
    """Install reroute geometry while retaining the authored path to restore."""
    if GENERATED_VIA_FIELD not in step:
        step[GENERATED_VIA_FIELD] = copy.deepcopy(step.get("via_points"))
    if via_points:
        step["via_points"] = copy.deepcopy(via_points)
    else:
        step.pop("via_points", None)
    step.pop("route", None)


def reroute_authored_via_points(step: dict) -> list | None:
    """Return only durable path constraints for a fresh reroute attempt.

    Once a repair has run, ``via_points`` contains generated geometry while
    ``compiler_v2_via`` retains the pre-repair authored value.  Reading the
    public field again would make consecutive repairs accumulate old detours.
    """
    value = (
        step.get(GENERATED_VIA_FIELD)
        if GENERATED_VIA_FIELD in step
        else step.get("via_points")
    )
    return copy.deepcopy(value)


def apply_detour_override(step: dict, overrides: dict[str, dict]) -> bool:
    """Apply a same-conflict temporary detour edit to a regenerated step."""
    step_id = str(step.get("id") or "")
    override = overrides.get(step_id)
    if override is None and "#detour_" in step_id:
        override = overrides.get(step_id.replace("#detour_", "#go_away_"))
    if override is None:
        return False
    fields = []
    if "standoff" in override:
        step["standoff"] = copy.deepcopy(override["standoff"])
        fields.append("standoff")
    if "via_points" in override:
        if override["via_points"]:
            step["via_points"] = copy.deepcopy(override["via_points"])
        else:
            step.pop("via_points", None)
        fields.append("via_points")
    if not fields:
        return False
    step[DETOUR_OVERRIDE_FIELDS_FIELD] = fields
    step[DETOUR_OVERRIDDEN_FIELD] = True
    return True


def promote_edited_artifact(step: dict, edit_op: str) -> None:
    """Turn a manually touched generated artifact into durable user intent."""
    if (step.get("source") == COMPILER_V2_SOURCE
            and step.get(REPAIR_KIND_FIELD) in ("detour", "go_away")):
        # A detour is editable execution geometry, not durable author intent.
        # Remember only the field the user touched; preparation removes the
        # generated step and carries this override into the next compile. If
        # the same conflict does not regenerate the detour, the override dies
        # with it instead of becoming an authored navigation task.
        field = {
            "set_step_via": "via_points",
            "set_step_standoff": "standoff",
        }.get(edit_op)
        if field:
            fields = list(step.get(DETOUR_OVERRIDE_FIELDS_FIELD) or [])
            if field not in fields:
                fields.append(field)
            step[DETOUR_OVERRIDE_FIELDS_FIELD] = fields
            step[DETOUR_OVERRIDDEN_FIELD] = True
            return
    if step.get("source") == COMPILER_V2_SOURCE:
        step["source"] = USER_SOURCE
        step[PROMOTED_FROM_FIELD] = COMPILER_V2_SOURCE
    if edit_op == "set_step_after":
        # The edit supplies the complete desired edge set.  None of those
        # edges may subsequently be stripped as an old compiler conclusion.
        step.pop(GENERATED_AFTER_FIELD, None)
    if edit_op == "set_step_via":
        step.pop(GENERATED_VIA_FIELD, None)


def _looks_like_legacy_departure(step: dict) -> bool:
    sid = str(step.get("id") or "")
    group = str(step.get("group") or "")
    return "#go_to_rest" in sid or group.endswith("#go_to_rest")


def _legacy_departure_is_user_touched(step: dict) -> bool:
    return (
        step.get("source") == USER_SOURCE
        or step.get(PROMOTED_FROM_FIELD) == COMPILER_V2_SOURCE
        or bool(step.get("via_points"))
    )


def _prepare_steps(steps: list[dict], *, remove_legacy_departures: bool) -> tuple[list[dict], dict]:
    kept: list[dict] = []
    removed_ids: list[str] = []
    legacy_ids: list[str] = []
    detour_overrides: list[dict] = []

    for raw_step in steps:
        step = copy.deepcopy(raw_step)
        generated = step.get("source") == COMPILER_V2_SOURCE
        legacy = (
            remove_legacy_departures
            and _looks_like_legacy_departure(step)
            and not _legacy_departure_is_user_touched(step)
        )
        if generated or legacy:
            sid = str(step.get("id") or "")
            if (generated and sid
                    and step.get(REPAIR_KIND_FIELD) in ("detour", "go_away")
                    and step.get(DETOUR_OVERRIDDEN_FIELD)):
                override = {"id": sid}
                for field in step.get(DETOUR_OVERRIDE_FIELDS_FIELD) or []:
                    override[field] = copy.deepcopy(step.get(field))
                detour_overrides.append(override)
            if sid:
                removed_ids.append(sid)
                if legacy and not generated:
                    legacy_ids.append(sid)
            continue
        kept.append(step)

    removed_set = set(removed_ids)
    removed_after: list[dict[str, str]] = []
    for step in kept:
        generated_after = set(step.pop(GENERATED_AFTER_FIELD, []) or [])
        stale = generated_after | removed_set
        authored_after = []
        for dependency in step.get("after") or []:
            if dependency in stale:
                removed_after.append({
                    "step": str(step.get("id") or ""),
                    "after": str(dependency),
                })
            elif dependency not in authored_after:
                authored_after.append(dependency)
        if authored_after:
            step["after"] = authored_after
        else:
            step.pop("after", None)
        if GENERATED_VIA_FIELD in step:
            authored_via = step.pop(GENERATED_VIA_FIELD)
            if authored_via:
                step["via_points"] = copy.deepcopy(authored_via)
            else:
                step.pop("via_points", None)
        # A completed route is derived output.  Authored path intent lives in
        # via_points and therefore survives this removal.
        step.pop("route", None)

    return kept, {
        "removed_step_ids": removed_ids,
        "removed_legacy_step_ids": legacy_ids,
        "removed_after_edges": removed_after,
        "detour_overrides": detour_overrides,
    }


def prepare_compiler_v2_input(
    plan: dict,
    *,
    remove_legacy_departures: bool = True,
) -> tuple[dict, dict[str, Any]]:
    """Return a clean plan copy plus an auditable preparation report.

    Both nested AuthoredPlan and flat per-robot forms are accepted.  Tagged V2
    coordination is always regenerated.  Legacy resolver departures are only
    removed when they have no evidence of an explicit user edit.
    """
    result = copy.deepcopy(plan)
    carried_overrides = list(result.pop(DETOUR_OVERRIDES_FIELD, []) or [])
    report: dict[str, Any] = {
        "removed_step_ids": [],
        "removed_legacy_step_ids": [],
        "removed_after_edges": [],
        "detour_overrides": carried_overrides,
    }

    def merge(part: dict) -> None:
        for key in report:
            report[key].extend(part[key])

    if "tasks" in result:
        tasks = []
        for task in result.get("tasks") or []:
            prepared_steps, part = _prepare_steps(
                task.get("steps") or [],
                remove_legacy_departures=remove_legacy_departures,
            )
            merge(part)
            if prepared_steps:
                task["steps"] = prepared_steps
                tasks.append(task)
        result["tasks"] = tasks
    else:
        for robot, steps in list(result.items()):
            if robot == DETOUR_OVERRIDES_FIELD:
                continue
            prepared_steps, part = _prepare_steps(
                steps or [],
                remove_legacy_departures=remove_legacy_departures,
            )
            merge(part)
            result[robot] = prepared_steps

    # A dependency can point to a generated step in another task/robot, so a
    # second global pass is required after every removed id is known.
    removed = set(report["removed_step_ids"])
    containers = (
        [task.get("steps") or [] for task in result.get("tasks") or []]
        if "tasks" in result else [
            steps for key, steps in result.items()
            if key != DETOUR_OVERRIDES_FIELD
        ]
    )
    recorded = {
        (entry["step"], entry["after"]) for entry in report["removed_after_edges"]
    }
    for steps in containers:
        for step in steps:
            after = []
            for dependency in step.get("after") or []:
                if dependency in removed:
                    key = (str(step.get("id") or ""), str(dependency))
                    if key not in recorded:
                        report["removed_after_edges"].append({
                            "step": key[0], "after": key[1],
                        })
                        recorded.add(key)
                elif dependency not in after:
                    after.append(dependency)
            if after:
                step["after"] = after
            else:
                step.pop("after", None)

    if report["detour_overrides"]:
        # Last edit wins if the same generated step was carried through more
        # than one preparation boundary (compound turn + skill service).
        by_id = {
            override["id"]: override
            for override in report["detour_overrides"]
            if override.get("id")
        }
        result[DETOUR_OVERRIDES_FIELD] = list(by_id.values())
        report["detour_overrides"] = list(by_id.values())

    return result, report


DEFERRED_CLOSE_FACILITY_FIELD = "_compiler_v2_deferred_close_facility"


def mark_automatic_close_owners_deferred(
    plan: dict, manifest: dict,
) -> tuple[dict, list[dict]]:
    """Mark unlocked close groups for time-based ownership in Compiler V2.

    The semantic/frontend contract still carries a provisional ``robot``.
    Compiler V2 ignores that value for an unlocked close and binds the group
    only after every placement completion has an actual scheduled end time.
    User-assigned (``robot_locked``) closes remain untouched.
    """
    result = copy.deepcopy(plan)
    tasks = result.get("tasks")
    if not isinstance(tasks, list):
        return result, []

    close_facility_by_skill = {}
    for facility, spec in (manifest.get("facilities") or {}).items():
        close_skill = (
            ((spec.get("articulation") or {}).get("skills") or {}).get("close")
        )
        if close_skill:
            close_facility_by_skill[close_skill] = facility

    placement_facilities = {
        step.get("dest")
        for task in tasks
        for step in (task.get("steps") or [])
        if step.get("op") == "place" and step.get("dest")
    }

    deferred = []
    for task in tasks:
        if bool(task.get("robot_locked", False)):
            continue
        close_facility = None
        for step in task.get("steps") or []:
            close_facility = close_facility_by_skill.get(
                step.get("name", step.get("op")))
            if close_facility is not None:
                break
        if (close_facility is None
                or close_facility not in placement_facilities):
            continue
        task[DEFERRED_CLOSE_FACILITY_FIELD] = close_facility
        deferred.append({
            "facility": close_facility,
            "close_task": task.get("task"),
            "provisional_robot": task.get("robot"),
        })

    return result, deferred


def select_deferred_close_owner(
    placement_completions: list[dict], completed_end: dict[str, float],
    eligible_robots: set[str] | None = None,
) -> dict | None:
    """Return the close owner after all placements have finished.

    Prefer the actual last placer when its robot has the close replay. If that
    robot is unsupported, use the latest eligible placer; when no eligible
    robot placed an object, hand the close to the first supported robot while
    retaining the actual last placement as the completion trigger.
    """
    if not placement_completions or any(
        entry["step"] not in completed_end for entry in placement_completions
    ):
        return None
    latest = max(
        placement_completions,
        key=lambda entry: (
            float(completed_end[entry["step"]]),
            str(entry["robot"]),
            str(entry["step"]),
        ),
    )
    if eligible_robots is None or latest["robot"] in eligible_robots:
        return latest
    eligible_placements = [
        entry for entry in placement_completions
        if entry["robot"] in eligible_robots
    ]
    if eligible_placements:
        return max(
            eligible_placements,
            key=lambda entry: (
                float(completed_end[entry["step"]]),
                str(entry["robot"]),
                str(entry["step"]),
            ),
        )
    if not eligible_robots:
        return None
    return {**latest, "robot": sorted(eligible_robots)[0]}
