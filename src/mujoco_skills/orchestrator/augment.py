"""Deterministic semantic augmentation used as an authoring-loop code tool."""

from __future__ import annotations

from collections import defaultdict

from mujoco_skills.orchestrator.schema import (
    AugmentedAction,
    SemanticTask,
    robot_ids_from_manifest,
)


# Estimated semantic work for deterministic authoring-time allocation. A move
# expands to two navigations, pick, place, and reset; articulation/go-to actions
# are materially smaller.
_WORK = {
    "move": 5.0,
    "open": 2.0,
    "close": 2.0,
    "go_to": 2.0,
}


def augment(
    actions: list[SemanticTask],
    manifest: dict,
    existing: list[AugmentedAction] | None = None,
) -> list[AugmentedAction]:
    """Expand move intents into ordered, conservative semantic sessions.

    This is deliberately a pure function: it performs no LLM, HTTP, filesystem,
    clock, random, or global-state access. The same actions and manifest always
    produce the same AugmentedAction list.

    `existing` is the current plan's actions (this turn's untouched work).
    New moves that share a source or destination form an affinity group. The
    group prefers one owner, while unrelated groups are assigned to the
    least-loaded robot using estimated semantic work. An explicit robot on any
    intent owns and locks the whole compatible group, including newly generated
    source/destination open and close actions.

    If the destination already exists in the plan, only new moves are returned.
    Existing open/close actions are reused, and a manually deleted close is not
    silently restored.
    """
    if not actions:
        return []

    current = existing or []
    robot_ids = robot_ids_from_manifest(manifest)
    load = {robot_id: 0.0 for robot_id in robot_ids}
    robot_order = {robot_id: index for index, robot_id in enumerate(robot_ids)}
    for action in current:
        if action.robot in load:
            load[action.robot] += _WORK.get(action.op, 1.0)

    objects = manifest.get("objects", {})
    facilities = manifest.get("facilities", {})
    seen_intent_ids: set[str] = set()
    by_dest: dict[str, list[SemanticTask]] = defaultdict(list)

    for action in actions:
        if action.action != "move":
            raise ValueError(f"augment only accepts move intents, got {action.action!r}")
        if not action.id or action.id in seen_intent_ids:
            raise ValueError(f"duplicate or empty semantic intent id {action.id!r}")
        if action.object not in objects:
            raise ValueError(f"unknown object {action.object!r}")
        if action.robot is not None and action.robot not in load:
            raise ValueError(f"invalid robot {action.robot!r}")
        facility = facilities.get(action.dest)
        if not facility or facility.get("place") is None:
            raise ValueError(f"destination {action.dest!r} is not placeable")
        seen_intent_ids.add(action.id)
        by_dest[action.dest].append(action)

    assignment, assignment_locked = _assign_affinity_groups(
        actions, current, objects, robot_ids, load, robot_order
    )

    result: list[AugmentedAction] = []
    used_action_ids: set[str] = {action.id for action in current}
    # Source containers are shared-world resources.  A single augmentation can
    # contain moves to several destinations (and assign them to both robots),
    # so keep one source session across the whole batch instead of reopening the
    # same fridge/cabinet once per destination.
    generated_source_open_ids: dict[str, str] = {}
    pending_source_closes: dict[str, dict] = {}
    for dest, session in by_dest.items():
        facility = facilities[dest]
        place = facility.get("place") or {}
        articulation = facility.get("articulation") or {}
        skills = articulation.get("skills") or {}
        destination_initially_open = articulation.get("initial_state") == "open"
        anchor = session[0].id
        existing_destination = any(
            action.dest == dest or action.facility == dest
            for action in current
        )
        assigned_moves = [(intent, assignment[intent.id]) for intent in session]
        destination_locked = (
            len({robot for _, robot in assigned_moves}) == 1
            and all(assignment_locked[intent.id] for intent, _ in assigned_moves)
        )

        if (not existing_destination
                and place.get("requires_open") is not None
                and not destination_initially_open):
            if not skills.get("open"):
                raise ValueError(
                    f"destination {dest!r} requires opening but has no open skill"
                )
            open_robot = assigned_moves[0][1]
            result.append(
                AugmentedAction(
                    id=_unique_id(f"{anchor}:open", used_action_ids),
                    robot=open_robot,
                    op="open",
                    facility=dest,
                    robot_locked=destination_locked,
                )
            )
            load[open_robot] += _WORK["open"]

        # Source access is independent of destination access. Counter/surface
        # homes have no requires_open and therefore remain plain moves. Fridge,
        # cabinet, and drawer homes are handled generically from the manifest;
        # no facility names are hard-coded here.
        by_source: dict[str | None, list[tuple[SemanticTask, str]]] = defaultdict(list)
        for intent, robot in assigned_moves:
            source = objects[intent.object].get("home_facility")
            # Moving within one facility is already protected by the
            # destination envelope above; do not emit a duplicate source pair.
            by_source[None if source == dest else source].append((intent, robot))

        for source, source_moves in by_source.items():
            source_locked = (
                len({robot for _, robot in source_moves}) == 1
                and all(assignment_locked[intent.id] for intent, _ in source_moves)
            )
            source_skills = {}
            source_requires_open = False
            source_initially_open = False
            if source is not None:
                source_facility = facilities.get(source)
                if source_facility is None:
                    raise ValueError(
                        f"object {source_moves[0][0].object!r} has unknown "
                        f"home facility {source!r}"
                    )
                source_articulation = source_facility.get("articulation") or {}
                source_skills = source_articulation.get("skills") or {}
                # Source access is an articulation concern, not a placement
                # concern.  A scene may intentionally expose a fridge only as
                # an object home (place=null), while its door still has to be
                # opened before a pick.  Prefer an explicit access marker when
                # present, and otherwise treat a stateful articulation as an
                # access-controlled container.
                source_access = source_facility.get("access") or {}
                source_requires_open = (
                    source_access.get("requires_open") is not None
                    or source_articulation.get("initial_state")
                    in {"open", "closed"}
                )
                source_initially_open = (
                    source_articulation.get("initial_state") == "open")

            source_open_id: str | None = None
            source_move_ids: list[str] = []
            existing_source_open = next(
                (
                    action
                    for action in current
                    if action.op == "open" and action.facility == source
                ),
                None,
            )
            existing_source_close = next(
                (
                    action
                    for action in current
                    if action.op == "close" and action.facility == source
                ),
                None,
            )
            existing_source_move = any(
                action.op == "move"
                and action.object in objects
                and objects[action.object].get("home_facility") == source
                and action.dest != source
                for action in current
            )
            existing_source_session = bool(
                existing_source_open
                or existing_source_close
                or existing_source_move
            )
            if existing_source_open is not None:
                source_open_id = existing_source_open.id
            elif source is not None and source in generated_source_open_ids:
                source_open_id = generated_source_open_ids[source]
            elif (source_requires_open
                    and not source_initially_open
                    and not existing_source_session):
                if not source_skills.get("open"):
                    raise ValueError(
                        f"source {source!r} requires opening but has no open skill"
                    )
                source_anchor = source_moves[0][0].id
                open_robot = source_moves[0][1]
                source_open_id = _unique_id(
                    f"{source_anchor}:source:open", used_action_ids
                )
                result.append(
                    AugmentedAction(
                        id=source_open_id,
                        robot=open_robot,
                        op="open",
                        facility=source,
                        robot_locked=source_locked,
                    )
                )
                generated_source_open_ids[source] = source_open_id
                load[open_robot] += _WORK["open"]

            for intent, robot in source_moves:
                move_id = _unique_id(intent.id, used_action_ids)
                source_move_ids.append(move_id)
                result.append(
                    AugmentedAction(
                        id=move_id,
                        robot=robot,
                        op="move",
                        object=intent.object,
                        dest=intent.dest,
                        serves=intent.id,
                        robot_locked=assignment_locked[intent.id],
                        # List order only serializes one robot's work. An
                        # explicit semantic edge is required when another
                        # robot performs a pick from the same opened source.
                        after=[source_open_id] if source_open_id else None,
                    )
                )

            if (source_requires_open
                    and source_skills.get("close")
                    and not existing_source_session):
                pending = pending_source_closes.setdefault(
                    source,
                    {
                        "anchor": source_moves[0][0].id,
                        "move_ids": [],
                        "robot": source_moves[-1][1],
                        "locked": source_locked,
                    },
                )
                pending["move_ids"].extend(source_move_ids)
                pending["robot"] = source_moves[-1][1]
                pending["locked"] = pending["locked"] and source_locked

        if not existing_destination and skills.get("close"):
            close_robot = assigned_moves[-1][1]
            result.append(
                AugmentedAction(
                    id=_unique_id(f"{anchor}:close", used_action_ids),
                    robot=close_robot,
                    op="close",
                    facility=dest,
                    robot_locked=destination_locked,
                )
            )
            load[close_robot] += _WORK["close"]

    for source, pending in pending_source_closes.items():
        close_robot = pending["robot"]
        result.append(
            AugmentedAction(
                id=_unique_id(
                    f"{pending['anchor']}:source:close", used_action_ids
                ),
                robot=close_robot,
                op="close",
                facility=source,
                robot_locked=pending["locked"],
                # Keep the source open until every shared transfer has
                # completed, including moves assigned to another robot or
                # headed to a different destination.
                after=pending["move_ids"],
            )
        )
        load[close_robot] += _WORK["close"]

    return result


def _assign_affinity_groups(
    intents: list[SemanticTask],
    existing: list[AugmentedAction],
    objects: dict,
    robot_ids: tuple[str, ...],
    load: dict[str, float],
    robot_order: dict[str, int],
) -> tuple[dict[str, str], dict[str, bool]]:
    """Assign connected source/destination groups with stable ownership."""
    parents = list(range(len(intents)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    first_for_key: dict[tuple[str, str], int] = {}
    intent_keys: list[set[tuple[str, str]]] = []
    for index, intent in enumerate(intents):
        keys = {("dest", intent.dest)}
        source = objects[intent.object].get("home_facility")
        if source:
            keys.add(("source", source))
        intent_keys.append(keys)
        for key in keys:
            if key in first_for_key:
                union(first_for_key[key], index)
            else:
                first_for_key[key] = index

    components: dict[int, list[int]] = defaultdict(list)
    for index in range(len(intents)):
        components[find(index)].append(index)

    existing_owners: dict[tuple[str, str], list[str]] = defaultdict(list)
    for action in existing:
        if action.op != "move" or action.robot not in load:
            continue
        if action.dest:
            existing_owners[("dest", action.dest)].append(action.robot)
        descriptor = objects.get(action.object or "", {})
        source = descriptor.get("home_facility")
        if source:
            existing_owners[("source", source)].append(action.robot)

    assigned: dict[str, str] = {}
    locked: dict[str, bool] = {}
    for indices in components.values():
        explicit = [
            intents[index].robot
            for index in indices
            if intents[index].robot is not None
        ]
        explicit_set = set(explicit)
        keys = set().union(*(intent_keys[index] for index in indices))

        if len(explicit_set) == 1:
            owner = explicit[0]
            lock_group = True
        else:
            candidates = [
                robot
                for key in keys
                for robot in existing_owners.get(key, [])
            ]
            if candidates:
                counts = {robot: candidates.count(robot) for robot in robot_ids}
                owner = min(
                    robot_ids,
                    key=lambda robot: (-counts[robot], load[robot], robot_order[robot]),
                )
            elif explicit:
                counts = {robot: explicit.count(robot) for robot in robot_ids}
                owner = min(
                    robot_ids,
                    key=lambda robot: (-counts[robot], load[robot], robot_order[robot]),
                )
            else:
                owner = min(robot_ids, key=lambda robot: (load[robot], robot_order[robot]))
            lock_group = False

        for index in indices:
            intent = intents[index]
            robot = intent.robot or owner
            assigned[intent.id] = robot
            # One unambiguous explicit owner expresses ownership of the whole
            # affinity group, not merely the individual move carrying the field.
            locked[intent.id] = lock_group or intent.robot is not None
            load[robot] += _WORK["move"]

    return assigned, locked


def _unique_id(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}:{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate
