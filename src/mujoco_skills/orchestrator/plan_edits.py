"""Backend application of manual plan-edit deltas (compound_turn_integration_
spec.md D2b, revising D2 / §5 item 16).

The frontend's `planEdits.ts` mutations are the browser-side source of truth
while a Gantt/scene edit is in progress; ScenePage keeps the PENDING batch
(not yet applied) and sends it with whichever turn applies it. D2's original
design had the backend replay that batch onto a freshly decomposed authored
plan every turn (`decompose(semanticActions)`) -- correct for a plain author
turn, but wrong for an edit-tail sync: the user edits the RESOLVED plan (D1's
only user-visible plan), which contains resolver artifacts (`go_to_rest`
departure legs, cross-robot `after` edges) a fresh decompose cannot express.
A delta targeting one of those had no target under D2 -- dropped with no
effect -- while the resolved plan's prior repairs were discarded in the same
stroke by the rebuild-from-scratch itself. D2b's fix: `stream_compound_turn`
picks a per-path plan (previous resolved plan for "edit"/"sync", the D1b graft
for a pure-append author turn, a clean recompute otherwise) and THIS module
applies the batch onto whichever one it picked -- see that function's
docstring for the full table. This module itself is plan-agnostic: it walks
whatever `tasks`/`steps` are in the dict it's given, resolver artifacts
included.

Deltas are targeted by ids that must survive onto the plan they are applied
to: a task-level edit targets the task/group id (stable as long as the action
itself is unmodified), a step-level edit targets the step id
(`f"{action_id}:s{index}"` for an authored step, or the resolver's own id for
an artifact like `f"{robot}#go_to_rest"`). When a target does not exist in
that plan -- the action was removed, a field change shifted its step shape,
or (D2b) the whole batch is being dropped because a non-pure-append author
turn replaced the plan out from under it -- the delta is dropped, never
silently: the caller must surface ``dropped_edit_text`` for every dropped
delta (a visible activity line), which is why `replay_edits` returns the
dropped list rather than swallowing it.
"""

from __future__ import annotations

import copy

from mujoco_skills.orchestrator.compiler_v2_plan import promote_edited_artifact


# Deltas that change WHO does what or in WHAT ORDER, as opposed to where a
# path runs. Mirrors the frontend's `draftEditImpact` "structural" bucket
# (ScenePage's editDraft call sites) -- the two lists must stay in step, but
# they are kept separate on purpose: the frontend's drives a button label,
# this one gates a plan rewrite, and a client is not trusted to decide that.
STRUCTURAL_OPS = frozenset({"remove_task", "move_task", "set_task_robot", "set_step_after"})


def semantic_removal_ids(edits: list[dict] | None) -> list[str]:
    """Return the ordered, deduplicated semantic task ids staged for removal."""
    result: list[str] = []
    for edit in edits or []:
        if (edit or {}).get("op") != "remove_task":
            continue
        action_id = ((edit or {}).get("target") or {}).get("actionId")
        if isinstance(action_id, str) and action_id and action_id not in result:
            result.append(action_id)
    return result


def without_semantic_removals(edits: list[dict] | None) -> list[dict]:
    """Deltas left for plan-level replay after semantic removals are applied."""
    return [edit for edit in edits or [] if (edit or {}).get("op") != "remove_task"]


def has_structural_edit(edits: list[dict] | None) -> bool:
    """True when this batch changes the coordination premises (allocation or
    ordering), which is what invalidates the resolver's departure anchors --
    see decompose.strip_stale_departures for why that matters."""
    return any((edit or {}).get("op") in STRUCTURAL_OPS for edit in edits or [])


def _find_step(plan: dict, step_id: str | None) -> dict | None:
    if not step_id:
        return None
    for task in plan.get("tasks", []):
        for step in task.get("steps", []):
            if step.get("id") == step_id:
                return step
    return None


def _known_step_ids(plan: dict) -> set[str]:
    return {
        step["id"]
        for task in plan.get("tasks", [])
        for step in task.get("steps", [])
        if step.get("id")
    }


def _describe_target(edit: dict) -> str:
    target = edit.get("target") or {}
    return (
        target.get("stepId")
        or target.get("actionId")
        or target.get("group")
        or "your edit"
    )


def dropped_edit_text(edit: dict) -> str:
    """User-facing activity line for a delta D2 could not replay.

    Deliberately named after WHAT the user touched, not the internal op --
    the same "effect, not mechanism" rule as resolver_progress_text.
    """
    return (
        f"Your manual change to {_describe_target(edit)} no longer applies "
        "after this update, so it was dropped."
    )


def _effective_edits(edits: list[dict] | None) -> list[dict]:
    """Collapse superseded gestures into the final user-visible changes."""
    latest: dict[tuple[str, str], tuple[int, dict]] = {}
    for index, edit in enumerate(edits or []):
        op = str((edit or {}).get("op") or "")
        target = (edit or {}).get("target") or {}
        target_id = (
            target.get("actionId")
            or target.get("group")
            or target.get("stepId")
            or f"unknown:{index}"
        )
        # A task move already includes allocation and ordering. Keep the key
        # per operation so unrelated route/position edits on the same task are
        # still reported separately.
        latest[(op, str(target_id))] = (index, edit)
    return [item for _index, item in sorted(latest.values(), key=lambda pair: pair[0])]


def _action_label(action_id: str | None, actions: list[dict] | None) -> str:
    if not action_id:
        return "the task"
    action = next(
        (item for item in actions or [] if isinstance(item, dict) and item.get("id") == action_id),
        None,
    )
    if action:
        op = action.get("op")
        if op == "move" and action.get("object") and action.get("dest"):
            return f"{action['object']} → {action['dest']}"
        if op == "open" and action.get("facility"):
            return f"open {action['facility']}"
        if op == "close" and action.get("facility"):
            return f"close {action['facility']}"
        if op == "go_to" and action.get("target"):
            return f"go to {action['target']}"
    return action_id


def _task_for_step(plan: dict | None, step_id: str | None) -> str | None:
    if not isinstance(plan, dict) or not step_id:
        return None
    for task in plan.get("tasks", []):
        if any(step.get("id") == step_id for step in task.get("steps", [])):
            return task.get("task")
    return None


def _task_robot(plan: dict | None, action_id: str | None) -> str | None:
    if not isinstance(plan, dict) or not action_id:
        return None
    task = next(
        (item for item in plan.get("tasks", []) if item.get("task") == action_id),
        None,
    )
    return task.get("robot") if task else None


def applied_edit_message(
    edits: list[dict] | None,
    before_plan: dict | None,
    after_plan: dict | None,
    actions: list[dict] | None,
    *,
    dropped_count: int = 0,
) -> str:
    """Describe successfully applied manual edits without an LLM call.

    Facts come only from the replay-success subset supplied by the compound
    turn. Repeated gestures targeting the same field collapse to the final
    effective edit, and task/step ids are resolved through semantic actions
    and the before/after plans.
    """
    lines: list[str] = []
    for edit in _effective_edits(edits):
        op = edit.get("op")
        target = edit.get("target") or {}
        action_id = target.get("actionId") or target.get("group")

        if op == "remove_task":
            lines.append(f"Removed {_action_label(action_id, actions)}.")
            continue

        if op == "move_task":
            label = _action_label(action_id, actions)
            source_robot = _task_robot(before_plan, action_id)
            robot = _task_robot(after_plan, action_id) or edit.get("robot")
            anchor_id = edit.get("afterActionId")
            position = (
                f" after {_action_label(anchor_id, actions)}"
                if anchor_id
                else " at the start of the task sequence"
            )
            if source_robot and robot and source_robot != robot:
                lines.append(f"Moved {label} to {robot}{position}.")
            else:
                suffix = f" on {robot}" if robot else ""
                lines.append(f"Reordered {label}{position}{suffix}.")
            continue

        if op == "set_task_robot":
            robot = _task_robot(after_plan, action_id) or edit.get("robot")
            lines.append(f"Assigned {_action_label(action_id, actions)} to {robot}.")
            continue

        step_id = target.get("stepId")
        task_id = _task_for_step(after_plan, step_id) or _task_for_step(before_plan, step_id)
        label = _action_label(task_id, actions)

        if op == "set_step_after":
            after_ids = edit.get("after") or []
            if after_ids:
                dependency_task = (
                    _task_for_step(after_plan, after_ids[0])
                    or _task_for_step(before_plan, after_ids[0])
                )
                lines.append(
                    f"Made {label} wait until {_action_label(dependency_task, actions)} finishes."
                )
            else:
                lines.append(f"Removed the manual dependency for {label}.")
        elif op == "set_step_at":
            lines.append(f"Updated the position for {label}.")
        elif op == "set_step_via":
            lines.append(f"Updated the route for {label}.")
        elif op == "set_step_standoff":
            lines.append(f"Updated the approach position for {label}.")

    if lines:
        heading = "Applied your edit:" if len(lines) == 1 else f"Applied {len(lines)} edits:"
        message = "\n".join([heading, *[f"{index}. {line}" for index, line in enumerate(lines, 1)]])
    else:
        message = "No edits were applied."
    if dropped_count:
        plural = "s" if dropped_count != 1 else ""
        message += f"\n{dropped_count} edit{plural} could not be applied."
    return message


def replay_edits(plan: dict, edits: list[dict] | None) -> tuple[dict, list[dict]]:
    """Apply each delta in order onto a copy of ``plan``.

    Returns ``(new_plan, dropped)`` -- ``dropped`` is every delta whose
    target id was not found in ``plan`` (see module docstring). Unknown `op`
    values are also dropped rather than raising: a delta from a newer
    frontend build talking to an older backend must degrade safely, not
    break the turn.
    """
    if not edits:
        return plan, []
    result = copy.deepcopy(plan)
    dropped: list[dict] = []
    for edit in edits:
        op = edit.get("op")
        target = edit.get("target") or {}
        applied = False

        if op == "move_task":
            action_id = target.get("actionId")
            robot = edit.get("robot")
            after_action_id = edit.get("afterActionId")
            tasks = result.get("tasks", [])
            source_index = next(
                (index for index, task in enumerate(tasks)
                 if task.get("task") == action_id), None)
            anchor_index = (
                next((index for index, task in enumerate(tasks)
                      if task.get("task") == after_action_id), None)
                if after_action_id is not None else None
            )
            anchor_valid = (
                after_action_id is None
                or (anchor_index is not None
                    and after_action_id != action_id
                    and tasks[anchor_index].get("robot") == robot)
            )
            if source_index is not None and anchor_valid:
                task = tasks.pop(source_index)
                if task.get("robot") != robot:
                    task["robot"] = robot
                    task["robot_locked"] = True
                if after_action_id is not None:
                    insert_at = next(
                        index for index, item in enumerate(tasks)
                        if item.get("task") == after_action_id
                    ) + 1
                else:
                    insert_at = next(
                        (index for index, item in enumerate(tasks)
                         if item.get("robot") == robot), len(tasks))
                tasks.insert(insert_at, task)
                applied = True

        elif op == "set_task_robot":
            group = target.get("actionId") or target.get("group")
            robot = edit.get("robot")
            for task in result.get("tasks", []):
                if task.get("task") == group:
                    task["robot"] = robot
                    task["robot_locked"] = True
                    applied = True
                    break

        elif op == "set_step_after":
            step = _find_step(result, target.get("stepId"))
            if step is not None:
                promote_edited_artifact(step, op)
                known = _known_step_ids(result)
                after_ids = [
                    a for a in dict.fromkeys(edit.get("after") or [])
                    if a != step.get("id") and a in known
                ]
                if after_ids:
                    step["after"] = after_ids
                else:
                    step.pop("after", None)
                applied = True

        elif op == "set_step_at":
            step = _find_step(result, target.get("stepId"))
            if step is not None:
                promote_edited_artifact(step, op)
                at = edit.get("at")
                if at is not None:
                    step["at"] = list(at)[:2]
                    applied = True

        elif op == "set_step_via":
            step = _find_step(result, target.get("stepId"))
            if step is not None:
                promote_edited_artifact(step, op)
                via = edit.get("via")
                if via:
                    step["via_points"] = [list(point) for point in via]
                else:
                    step.pop("via_points", None)
                    step.pop("route", None)
                applied = True

        elif op == "set_step_standoff":
            # Not one of D2's four enumerated ops, but every planEdits.* call
            # site must push a delta (item 16) and setStepStandoff is a real
            # call site (ScenePage's navigate-standoff drag). No resolver
            # tool moves a navigate's standoff, so this op carries no
            # protected-set entry -- it exists purely so the edit replays
            # instead of being silently lost on the next author turn.
            step = _find_step(result, target.get("stepId"))
            if step is not None:
                promote_edited_artifact(step, op)
                standoff = edit.get("standoff")
                if standoff:
                    step["standoff"] = list(standoff)[:2]
                else:
                    step.pop("standoff", None)
                applied = True

        if not applied:
            dropped.append(edit)

    return result, dropped
