"""Stage 3: deterministic AugmentedAction -> AuthoredPlan templates."""

from __future__ import annotations

from dataclasses import fields

from mujoco_skills.orchestrator.schema import AugmentedAction

RESET = {"op": "reset", "retreat": 0.18, "preserve_yaw": True}


def decompose(
    actions: list[AugmentedAction],
    manifest: dict,
    scene_refs: list | None = None,
) -> dict:
    """Expand each action to exactly one authored task with stable grouped steps.

    ``scene_refs`` (this turn's Cursor-style scene references) resolves any
    `place_at_pin` handle on a move to a concrete xy anchor. Resolution is
    THIS-TURN-ONLY by design: pins are not persisted across turns, so a stale
    handle from an earlier turn's current_plan simply fails to resolve here and
    the place step falls back to the default distribution (no at_anchor). This
    is a known limitation, not a bug -- carrying pins across turns would need a
    separate persisted pin registry, out of scope for this feature.
    """
    pin_xy = {
        str(ref.get("handle")): [float(ref["xyz"][0]), float(ref["xyz"][1])]
        for ref in (scene_refs or [])
        if isinstance(ref, dict)
        and ref.get("kind") == "position"
        and ref.get("handle")
        and isinstance(ref.get("xyz"), list)
        and len(ref["xyz"]) >= 2
    }
    _validate_semantic_after(actions)
    tasks = []
    robot_ends_reset: dict[str, bool] = {}
    for action in actions:
        steps = _steps(
            action,
            manifest,
            previous_action_ended_reset=robot_ends_reset.get(action.robot, False),
            pin_xy=pin_xy,
        )
        tasks.append({
            "task": action.id,
            "robot": action.robot,
            "robot_locked": action.robot_locked,
            "steps": steps,
        })
        robot_ends_reset[action.robot] = bool(steps and steps[-1]["op"] == "reset")
    # Semantic precedence is deliberately expanded after every action has its
    # full step template: a dependent task starts after the predecessor task's
    # FINAL step, not after its first navigation or pick.
    step_bounds = {
        task["task"]: (task["steps"][0]["id"], task["steps"][-1]["id"])
        for task in tasks
        if task.get("steps")
    }
    action_by_id = {action.id: action for action in actions}
    for task in tasks:
        action = action_by_id[task["task"]]
        predecessors = action.after or []
        if not predecessors:
            continue
        first_step = task["steps"][0]
        existing = list(first_step.get("after") or [])
        for predecessor in predecessors:
            predecessor_last = step_bounds[predecessor][1]
            if predecessor_last not in existing:
                existing.append(predecessor_last)
        first_step["after"] = existing
    return {"tasks": tasks}


def _validate_semantic_after(actions: list[AugmentedAction]) -> None:
    ids = {action.id for action in actions}
    if len(ids) != len(actions):
        raise ValueError("duplicate semantic action ids")
    graph: dict[str, list[str]] = {}
    for action in actions:
        predecessors = list(action.after or [])
        if action.id in predecessors:
            raise ValueError(f"semantic action {action.id!r} cannot depend on itself")
        unknown = [item for item in predecessors if item not in ids]
        if unknown:
            raise ValueError(f"semantic action {action.id!r} depends on unknown ids {unknown}")
        if len(set(predecessors)) != len(predecessors):
            raise ValueError(f"semantic action {action.id!r} repeats a dependency")
        graph[action.id] = predecessors
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(action_id: str) -> None:
        if action_id in visiting:
            raise ValueError("semantic action dependencies contain a cycle")
        if action_id in visited:
            return
        visiting.add(action_id)
        for predecessor in graph[action_id]:
            visit(predecessor)
        visiting.remove(action_id)
        visited.add(action_id)
    for action_id in graph:
        visit(action_id)


def _action_identity(action: AugmentedAction) -> tuple:
    """Full-field identity for D1b's pure-append test.

    EVERY field is compared, not a hand-picked subset, because `_steps` reads
    far more of the action than task identity suggests: `facility` (open/close
    targets), `via_points` (an explicitly routed path), `place_at_pin` (a
    scene-ref-bound destination), `target`, `serves`. A subset check would let
    "route robot0 around the left side, and also move the bowl" pass the
    pure-append test on its unchanged-looking prefix, then reuse the PREVIOUS
    task's steps for that action -- silently discarding the reroute the user
    just asked for.

    Comparing all fields can only err toward falling back to a clean
    recompute, which is correct-but-slower; the subset erred toward silently
    wrong. Using `fields()` rather than a literal list also means a field
    added to AugmentedAction later is covered without anyone remembering to
    come back here.
    """
    return tuple(
        _hashable(getattr(action, spec.name)) for spec in fields(action)
    )


def _hashable(value):
    """`via_points` is a list of lists; tuples so identities stay comparable."""
    if isinstance(value, list):
        return tuple(_hashable(item) for item in value)
    return value


def append_promotion_reason(
    previous_actions: list[AugmentedAction],
    previous_plan: dict | None,
    new_actions: list[AugmentedAction],
) -> str | None:
    """``None`` when D1b's promotion applies, else WHY it does not.

    Split out from ``merge_appended_tasks`` because "the fast path silently
    did not happen" is indistinguishable from "it happened and did not help"
    by looking at the resulting plan -- both just look like a full re-resolve.
    The reason string is logged per turn and carried in ``turn_result`` so a
    regression here is visible instead of merely slow. A prefix mismatch names
    the index and field, which is the whole diagnosis in one line.
    """
    if not previous_actions:
        return "no_previous_actions"
    if not isinstance(previous_plan, dict) or not isinstance(
        previous_plan.get("tasks"), list
    ):
        return "no_previous_plan"
    if len(new_actions) <= len(previous_actions):
        return "no_additions"
    for index, (before, after) in enumerate(zip(previous_actions, new_actions)):
        for spec in fields(before):
            old = _hashable(getattr(before, spec.name))
            new = _hashable(getattr(after, spec.name))
            if old != new:
                return f"prefix_mismatch[{index}].{spec.name}: {old!r} -> {new!r}"
    return None


def merge_appended_tasks(
    previous_actions: list[AugmentedAction],
    previous_plan: dict | None,
    new_actions: list[AugmentedAction],
    manifest: dict,
    scene_refs: list | None = None,
) -> dict | None:
    """D1b's pure-append promotion: reuse a previous turn's resolved plan
    instead of re-deriving (and re-resolving) it from scratch.

    Returns the merged plan (``previous_plan``'s tasks plus the newly
    appended ones) when ``new_actions`` is exactly ``previous_actions`` with
    more actions added at the end -- same ids, same op/object/dest/robot/
    robot_locked, same order, nothing reordered/edited/removed. Returns
    ``None`` for every other case (including "nothing new"), which tells the
    caller to fall back to a clean ``decompose(new_actions, ...)`` exactly as
    before this promotion existed.
    """
    if append_promotion_reason(previous_actions, previous_plan, new_actions) is not None:
        return None
    previous_tasks = previous_plan["tasks"]

    # Decompose the FULL new action list, not just the appended tail, then
    # slice off the prefix. `_steps` thread per-robot `previous_action_ended_
    # reset` state ACROSS actions (see decompose() above): decomposing only
    # the appended actions in isolation would start that state at False for
    # the first appended action of every robot, even when its real
    # predecessor (the last *previous* action for that robot) ended in a
    # reset. Decomposing from the full list keeps that carried state correct
    # and then slicing is just bookkeeping, not a second source of truth.
    full_plan = decompose(new_actions, manifest, scene_refs)
    appended_tasks = full_plan["tasks"][len(previous_actions):]

    gaining_robots = {task["robot"] for task in appended_tasks}
    merged_tasks = _strip_trailing_departures(previous_tasks, gaining_robots)
    merged_tasks = _invalidate_changed_place_groups(
        merged_tasks, previous_actions, new_actions
    )
    return {"tasks": merged_tasks + appended_tasks}


def _placement_group(action: AugmentedAction) -> tuple[str, str | None] | None:
    """The compiler slot group affected by one semantic move.

    Unpinned moves share the destination-wide automatic layout. Moves using
    the same semantic pin share a local fan-out around that pin. Other action
    kinds do not produce a place step.
    """
    if action.op != "move" or not action.dest:
        return None
    return action.dest, action.place_at_pin


def _invalidate_changed_place_groups(
    previous_tasks: list[dict],
    previous_actions: list[AugmentedAction],
    new_actions: list[AugmentedAction],
) -> list[dict]:
    """Clear completed drop points when an automatic slot group changes size.

    A resolved plan echoes each compiler-selected place point back as ``at``.
    The pure-append fast path intentionally reuses that resolved prefix, but an
    echoed ``at`` looks like a user-authored fixed point to the next compile.
    Consequently, adding a third sink move kept the first two objects at their
    two-object coordinates and laid out only the newcomer -- often directly on
    top of an existing object.

    Cardinality is semantic state, so detect the changed groups from the full
    old/new action lists and invalidate every reused place step in those
    groups. The normal compiler then sees the whole group without fixed ``at``
    overrides and assigns all N slots together. Tasks outside an affected
    group (including resolver repairs) remain verbatim.
    """
    old_counts: dict[tuple[str, str | None], int] = {}
    new_counts: dict[tuple[str, str | None], int] = {}
    for action in previous_actions:
        key = _placement_group(action)
        if key is not None:
            old_counts[key] = old_counts.get(key, 0) + 1
    for action in new_actions:
        key = _placement_group(action)
        if key is not None:
            new_counts[key] = new_counts.get(key, 0) + 1
    changed = {
        key for key in old_counts.keys() | new_counts.keys()
        if old_counts.get(key, 0) != new_counts.get(key, 0)
    }
    if not changed:
        return previous_tasks

    group_by_action = {
        action.id: _placement_group(action) for action in previous_actions
    }
    rewritten_tasks = []
    for task in previous_tasks:
        key = group_by_action.get(task.get("task"))
        if key not in changed:
            rewritten_tasks.append(task)
            continue
        task_changed = False
        rewritten_steps = []
        for step in task.get("steps", []):
            if step.get("op") == "place" and step.get("dest") == key[0]:
                clean = dict(step)
                clean.pop("at", None)
                clean.pop("_slot", None)
                clean.pop("_slot_count", None)
                rewritten_steps.append(clean)
                task_changed = task_changed or clean != step
            else:
                rewritten_steps.append(step)
        rewritten_tasks.append(
            {**task, "steps": rewritten_steps} if task_changed else task
        )
    return rewritten_tasks


def strip_stale_departures(plan: dict) -> dict:
    """Drop EVERY robot's trailing departure anchor from an already-resolved
    plan, for the compound turn's structural-edit path.

    A departure anchor encodes one coordination decision -- "this robot parks
    here so that one can enter" -- derived from who does what, in what order.
    A structural edit (reassigning a task, repinning an `after` edge) changes
    exactly those premises, so every such anchor in the plan is a conclusion
    drawn from a scene that no longer exists. The resolver only ever ADDS
    repairs; nothing withdraws them, so without this the anchor survives as
    historical inertia -- a pointless parking leg plus a cross-robot edge that
    needlessly stretches the makespan.

    Deliberately broader than the pure-append path's ``gaining_robots``
    filter: an anchor is a relationship between two robots (the parker and
    the waiter), and reassigning a task can invalidate it via EITHER end, so
    "robots this edit touched" under-covers. Over-stripping is the safe
    direction -- resolve re-derives an anchor that is still warranted (one
    extra attempt), whereas keeping a stale one silently over-constrains the
    plan. Spatial edits (waypoint/standoff/place anchor) do not call this:
    they move a path, not a coordination decision.
    """
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        return plan
    robots = {task.get("robot") for task in tasks if task.get("robot")}
    tasks = _strip_deadlock_yields(tasks)
    return {**plan, "tasks": _strip_trailing_departures(tasks, robots)}


def _strip_deadlock_yields(tasks: list[dict]) -> list[dict]:
    """Withdraw resolver-generated ``<move-step>#yield`` coordination.

    ``insert_yield`` moves the yielding step's original dependencies onto the
    temporary parking step, then adds park -> winner -> resumed-move edges.
    A structural edit invalidates that conclusion.  Remove the parking task,
    restore the yielding move's original ``after`` list, and purge the
    winner's reference to the removed step.  Pure-append reuse does not call
    this helper, so an unchanged verified prefix keeps its coordination.
    """
    removed: set[str] = set()
    restored_after: dict[str, list[str]] = {}
    kept = []
    for task in tasks:
        task_id = task.get("task")
        steps = task.get("steps")
        if (isinstance(task_id, str) and task_id.endswith("#yield")
                and isinstance(steps, list) and len(steps) == 1
                and steps[0].get("id") == task_id):
            removed.add(task_id)
            original_id = task_id[:-len("#yield")]
            restored_after[original_id] = list(steps[0].get("after", []))
            continue
        kept.append(task)
    if not removed:
        return list(tasks)

    rewritten_tasks = []
    for task in kept:
        steps = task.get("steps")
        if not isinstance(steps, list):
            rewritten_tasks.append(task)
            continue
        changed = False
        rewritten_steps = []
        for step in steps:
            step_id = step.get("id")
            if step_id in restored_after:
                rewritten = {**step, "after": restored_after[step_id]}
                changed = changed or rewritten != step
                rewritten_steps.append(rewritten)
                continue
            after = step.get("after")
            if after and any(dependency in removed for dependency in after):
                rewritten_steps.append({
                    **step,
                    "after": [
                        dependency for dependency in after
                        if dependency not in removed
                    ],
                })
                changed = True
            else:
                rewritten_steps.append(step)
        rewritten_tasks.append(
            {**task, "steps": rewritten_steps} if changed else task
        )
    return rewritten_tasks


def _strip_trailing_departures(tasks: list[dict], gaining_robots: set[str]) -> list[dict]:
    """Drop a robot's trailing ``<robot>#go_to_rest`` task before grafting new
    work onto it.

    The resolver only ever appends a departure anchor as a robot's LAST act
    (`_departure_prerequisite_candidates` in resolver_v2.py looks at
    ``robot_steps[-1]``); it is meaningless anywhere else. If we grafted new
    tasks after it unconditionally, every turn that adds work for a robot
    which was previously parked would accumulate another
    work -> park -> work -> park leg forever. Stripping it here is safe
    because the resolver re-inserts the anchor if the merged plan still needs
    one -- `apply_insert_go_to` is idempotent and
    `_departure_prerequisite_candidates` already skips a robot whose flat
    plan ends with a duplicate ``departure_id`` (``if departure_id in
    by_id: continue``). Only robots gaining new tasks this turn are touched,
    and only their trailing anchor -- a robot with no new work keeps its
    parked state exactly as the user last saw it.

    Removing the task is only half of it. `apply_insert_go_to` writes a
    navigate + reset PAIR, and the anchor exists precisely because some other
    robot's step carries ``after: [<robot>#go_to_rest]`` -- that waiter is
    usually not in ``gaining_robots``, so its edge would outlive the step it
    points at and `compile_plan` would refuse the plan outright ("step 'X'
    depends on unknown step id(s)", `_container_dependency_order` in
    skill_generators.py). So every reference to a removed step is purged in
    the same pass.
    """
    result = list(tasks)
    removed_step_ids: set[str] = set()
    for robot in gaining_robots:
        indices = [index for index, task in enumerate(result) if task.get("robot") == robot]
        if not indices:
            continue
        last_index = indices[-1]
        task = result[last_index]
        if task.get("task") != f"{robot}#go_to_rest":
            continue
        removed_step_ids.update(
            step["id"] for step in task.get("steps", []) or [] if step.get("id")
        )
        result.pop(last_index)
    if not removed_step_ids:
        return result
    return [_without_dangling_after(task, removed_step_ids) for task in result]


def _without_dangling_after(task: dict, removed_step_ids: set[str]) -> dict:
    """Copy-on-write: only the task/steps that actually referenced a removed
    step are rebuilt, so an untouched task stays identical (by identity) to
    what the caller handed in."""
    steps = task.get("steps")
    if not isinstance(steps, list):
        return task
    rewritten = []
    changed = False
    for step in steps:
        after = step.get("after")
        if after and any(dep in removed_step_ids for dep in after):
            rewritten.append(
                {**step, "after": [dep for dep in after if dep not in removed_step_ids]}
            )
            changed = True
        else:
            rewritten.append(step)
    return {**task, "steps": rewritten} if changed else task


def _steps(
    action: AugmentedAction,
    manifest: dict,
    *,
    previous_action_ended_reset: bool,
    pin_xy: dict[str, list[float]] | None = None,
) -> list[dict]:
    if action.op == "move":
        if not action.object or not action.dest:
            raise ValueError(f"move {action.id!r} requires object and dest")
        pick = {"op": "pick", "object": action.object}
        object_spec = manifest.get("objects", {}).get(action.object, {})
        pick_cfg = object_spec.get("pick") or {}
        configured_grasp = pick_cfg.get("grasp_mode")
        if configured_grasp is not None:
            pick["grasp_mode"] = configured_grasp
            if configured_grasp == "horizontal":
                pick["return_to_ready"] = pick_cfg.get("return_to_ready", True)
                pick["post_grasp_lift"] = pick_cfg.get("post_grasp_lift", 0.0)
        if "grasp_offset" in pick_cfg:
            pick["grasp_offset"] = list(pick_cfg["grasp_offset"])
        if "ready_torso" in pick_cfg:
            pick["ready_torso"] = float(pick_cfg["ready_torso"])
        label = str(object_spec.get("label", ""))
        grasp_name = f"{action.object} {label}".lower().replace("_", " ")
        if (configured_grasp is None
                and any(kind in grasp_name for kind in ("condiment", "canned food"))):
            pick.update({"grasp_mode": "horizontal", "return_to_ready": True})
        place = {"op": "place", "object": action.object, "dest": action.dest}
        anchor = (pin_xy or {}).get(action.place_at_pin) if action.place_at_pin else None
        if anchor is not None:
            place["at_anchor"] = anchor
        raw = [
            {"op": "navigate", "target": action.object},
            pick,
            {"op": "navigate", "target": action.dest},
            place,
            dict(RESET),
        ]
    elif action.op == "open":
        facility, skill = _articulation(action, manifest, "open")
        place = manifest["facilities"][facility].get("place") or {}
        # Front-access replay entry/exit poses (notably OpenFridge) end in a
        # narrow doorway where any backward base retreat collides. Keep the
        # reset semantic/arm reset, but do not translate the base there.
        open_reset = (
            {"op": "reset", "retreat": 0.0, "preserve_yaw": True}
            if place.get("access") == "front"
            else dict(RESET)
        )
        raw = [
            {"op": "navigate", "target": facility},
            {"op": skill},
            open_reset,
        ]
    elif action.op == "close":
        facility, skill = _articulation(action, manifest, "close")
        leading_reset = (
            {"op": "reset", "retreat": 0.0, "preserve_yaw": True}
            if previous_action_ended_reset
            else dict(RESET)
        )
        raw = [
            leading_reset,
            {"op": "navigate", "target": facility},
            {"op": skill},
            dict(RESET),
        ]
    elif action.op == "go_to":
        if not action.target:
            raise ValueError(f"go_to {action.id!r} requires target")
        navigate = {"op": "navigate", "target": action.target}
        if action.via_points is not None:
            navigate["via_points"] = action.via_points
        raw = [navigate, dict(RESET)]
    else:
        raise ValueError(f"unsupported augmented action op {action.op!r}")

    return [
        {
            "id": f"{action.id}:s{index}",
            "group": action.id,
            **step,
        }
        for index, step in enumerate(raw)
    ]


def _articulation(
    action: AugmentedAction,
    manifest: dict,
    operation: str,
) -> tuple[str, str]:
    if not action.facility:
        raise ValueError(f"{operation} {action.id!r} requires facility")
    facility = manifest.get("facilities", {}).get(action.facility)
    if not facility:
        raise ValueError(f"unknown facility {action.facility!r}")
    skill = ((facility.get("articulation") or {}).get("skills") or {}).get(operation)
    if not skill:
        raise ValueError(f"facility {action.facility!r} has no {operation} skill")
    return action.facility, skill
