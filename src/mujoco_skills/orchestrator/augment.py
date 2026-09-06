"""Deterministic semantic augmentation used as an authoring-loop code tool."""

from __future__ import annotations

from collections import defaultdict
from itertools import product

from mujoco_skills.eligibility import (
    AssignmentEligibilityError,
    can_execute_skill,
    can_pick,
    can_place,
)
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

# Exact search keeps allocation independent of the order in which the LLM
# happened to mention tasks. Larger requests fall back to deterministic LPT
# scheduling so authoring latency cannot grow exponentially.
_MAX_EXACT_ASSIGNMENTS = 100_000


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
    New moves with the exact same (source, destination) route form an affinity
    group. Compatible groups keep one owner, while all groups are assigned
    globally to minimize peak load and load spread. An explicit robot on any
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

    eligible_by_intent = {}
    for intent in actions:
        eligible = []
        first_rejection = None
        for robot in robot_ids:
            pick = can_pick(manifest, robot, intent.object)
            place = can_place(manifest, robot, intent.dest)
            rejection = pick if not pick["eligible"] else place
            if rejection["eligible"]:
                eligible.append(robot)
            elif first_rejection is None:
                first_rejection = rejection
        eligible_by_intent[intent.id] = tuple(eligible)
        if intent.robot is not None and intent.robot not in eligible:
            pick = can_pick(manifest, intent.robot, intent.object)
            decision = (
                pick if not pick["eligible"]
                else can_place(manifest, intent.robot, intent.dest))
            raise AssignmentEligibilityError(decision, manifest)
        if not eligible:
            decision = dict(first_rejection or {})
            decision.update({
                "eligible": False,
                "code": "NO_ELIGIBLE_ROBOT",
                "robot": intent.robot or "",
                "operation": "move",
                "target": intent.dest,
                "eligible_robots": [],
            })
            raise AssignmentEligibilityError(decision, manifest)

    assignment, assignment_locked = _assign_affinity_groups(
        actions, current, objects, robot_ids, load, robot_order,
        eligible_by_intent,
    )

    result: list[AugmentedAction] = []
    used_action_ids: set[str] = {action.id for action in current}
    # Source containers are shared-world resources.  A single augmentation can
    # contain moves to several destinations (and assign them across robots),
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
        destination_articulation_robot = None
        destination_open_id = None
        destination_move_ids = []
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
            open_robot = _articulation_robot(
                manifest, skills["open"], assigned_moves[0][1],
                load, robot_order)
            destination_articulation_robot = open_robot
            destination_open_id = _unique_id(
                f"{anchor}:open", used_action_ids)
            result.append(
                AugmentedAction(
                    id=destination_open_id,
                    robot=open_robot,
                    op="open",
                    facility=dest,
                    robot_locked=(
                        destination_locked
                        and open_robot == assigned_moves[0][1]),
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
                open_robot = _articulation_robot(
                    manifest, source_skills["open"], source_moves[0][1],
                    load, robot_order)
                source_open_id = _unique_id(
                    f"{source_anchor}:source:open", used_action_ids
                )
                result.append(
                    AugmentedAction(
                        id=source_open_id,
                        robot=open_robot,
                        op="open",
                        facility=source,
                        robot_locked=(
                            source_locked
                            and open_robot == source_moves[0][1]),
                    )
                )
                generated_source_open_ids[source] = source_open_id
                load[open_robot] += _WORK["open"]

            for intent, robot in source_moves:
                move_id = _unique_id(intent.id, used_action_ids)
                source_move_ids.append(move_id)
                destination_move_ids.append(move_id)
                prerequisites = [
                    dependency
                    for dependency in (destination_open_id, source_open_id)
                    if dependency is not None
                ]
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
                        after=prerequisites or None,
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
            close_robot = _articulation_robot(
                manifest, skills["close"],
                destination_articulation_robot or assigned_moves[-1][1],
                load, robot_order)
            result.append(
                AugmentedAction(
                    id=_unique_id(f"{anchor}:close", used_action_ids),
                    robot=close_robot,
                    op="close",
                    facility=dest,
                    robot_locked=(
                        destination_locked
                        and close_robot == assigned_moves[-1][1]),
                    # List order is not a cross-robot synchronization edge.
                    # The support robot may close only after every mover has
                    # completed this destination session.
                    after=destination_move_ids or None,
                )
            )
            load[close_robot] += _WORK["close"]

    for source, pending in pending_source_closes.items():
        source_skills = (
            (facilities[source].get("articulation") or {}).get("skills")
            or {})
        close_robot = _articulation_robot(
            manifest, source_skills["close"], pending["robot"],
            load, robot_order)
        result.append(
            AugmentedAction(
                id=_unique_id(
                    f"{pending['anchor']}:source:close", used_action_ids
                ),
                robot=close_robot,
                op="close",
                facility=source,
                robot_locked=(
                    pending["locked"] and close_robot == pending["robot"]),
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
    eligible_by_intent: dict[str, tuple[str, ...]],
) -> tuple[dict[str, str], dict[str, bool]]:
    """Assign exact-route groups with deterministic global load balancing.

    A route group is indivisible only when all of its moves have a common
    eligible owner and its explicit assignments are compatible. Otherwise it
    is split into singleton allocation units so grouping never makes a set of
    individually feasible moves infeasible.
    """
    route_groups: dict[tuple[str | None, str], list[int]] = defaultdict(list)
    for index, intent in enumerate(intents):
        source = objects[intent.object].get("home_facility")
        route_groups[(source, intent.dest)].append(index)

    existing_owners: dict[tuple[str | None, str], list[str]] = defaultdict(list)
    for action in existing:
        if (action.op != "move" or action.robot not in load
                or not action.dest):
            continue
        descriptor = objects.get(action.object or "", {})
        source = descriptor.get("home_facility")
        existing_owners[(source, action.dest)].append(action.robot)

    units: list[dict] = []
    for route, indices in route_groups.items():
        common_eligible = set(robot_ids)
        for index in indices:
            common_eligible &= set(eligible_by_intent[intents[index].id])
        explicit = {
            intents[index].robot
            for index in indices
            if intents[index].robot is not None
        }
        compatible_explicit = (
            len(explicit) <= 1
            and (not explicit or next(iter(explicit)) in common_eligible)
        )
        if common_eligible and compatible_explicit:
            candidates = tuple(
                robot for robot in robot_ids if robot in common_eligible)
            lock_group = bool(explicit)
            if explicit:
                candidates = (next(iter(explicit)),)
            else:
                prior_owners = set(existing_owners.get(route, []))
                if len(prior_owners) == 1:
                    prior_owner = next(iter(prior_owners))
                    if prior_owner in common_eligible:
                        candidates = (prior_owner,)
            units.append({
                "route": route,
                "indices": tuple(indices),
                "candidates": candidates,
                "locked": lock_group,
            })
            continue

        # Conflicting hard assignments or an empty eligibility intersection
        # mean this route cannot safely remain one batch.
        for index in indices:
            intent = intents[index]
            candidates = (
                (intent.robot,)
                if intent.robot is not None
                else tuple(eligible_by_intent[intent.id])
            )
            units.append({
                "route": route,
                "indices": (index,),
                "candidates": candidates,
                "locked": intent.robot is not None,
            })

    def route_key(unit: dict) -> tuple[str, str, tuple[str, ...]]:
        source, dest = unit["route"]
        ids = tuple(sorted(intents[index].id for index in unit["indices"]))
        return (source or "", dest, ids)

    # Canonical ordering makes both exact search and its tie-break independent
    # of prompt/task mention order. LPT also gives the bounded fallback a good
    # approximation of the minimax assignment.
    units.sort(key=lambda unit: (
        -len(unit["indices"]), route_key(unit)))

    def preference_penalty(unit: dict, owner: str) -> int:
        owners = existing_owners.get(unit["route"], [])
        return len(owners) - owners.count(owner)

    combination_count = 1
    for unit in units:
        combination_count *= len(unit["candidates"])
        if combination_count > _MAX_EXACT_ASSIGNMENTS:
            break

    owners: tuple[str, ...]
    if combination_count <= _MAX_EXACT_ASSIGNMENTS:
        best_score = None
        best_owners = None
        for candidate_owners in product(*(
                unit["candidates"] for unit in units)):
            projected = dict(load)
            affinity_penalty = 0
            for unit, owner in zip(units, candidate_owners):
                projected[owner] += len(unit["indices"]) * _WORK["move"]
                affinity_penalty += preference_penalty(unit, owner)
            values = tuple(projected[robot] for robot in robot_ids)
            score = (
                max(values),
                max(values) - min(values),
                sum(value * value for value in values),
                affinity_penalty,
                tuple(robot_order[owner] for owner in candidate_owners),
            )
            if best_score is None or score < best_score:
                best_score = score
                best_owners = candidate_owners
        owners = best_owners or ()
    else:
        chosen = []
        projected = dict(load)
        for unit in units:
            owner = min(
                unit["candidates"],
                key=lambda robot: (
                    projected[robot]
                    + len(unit["indices"]) * _WORK["move"],
                    preference_penalty(unit, robot),
                    robot_order[robot],
                ),
            )
            chosen.append(owner)
            projected[owner] += len(unit["indices"]) * _WORK["move"]
        owners = tuple(chosen)

    assigned: dict[str, str] = {}
    locked: dict[str, bool] = {}
    for unit, owner in zip(units, owners):
        for index in unit["indices"]:
            intent = intents[index]
            assigned[intent.id] = owner
            # One unambiguous explicit owner expresses ownership of the whole
            # affinity group, not merely the individual move carrying the field.
            locked[intent.id] = unit["locked"] or intent.robot is not None
            load[owner] += _WORK["move"]

    return assigned, locked


def _articulation_robot(manifest: dict, skill_name: str, preferred: str,
                        load: dict[str, float], robot_order: dict[str, int]):
    decision = can_execute_skill(manifest, preferred, skill_name)
    eligible = tuple(decision.get("eligible_robots") or [])
    if decision["eligible"]:
        return preferred
    if not eligible:
        raise AssignmentEligibilityError(decision, manifest)
    return min(
        eligible,
        key=lambda robot: (load.get(robot, 0.0), robot_order.get(robot, 10**6)),
    )


def _unique_id(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}:{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate
