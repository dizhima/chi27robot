"""Agentic semantic authoring loop with a deliberately narrow tool boundary."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, replace

from mujoco_skills.orchestrator import loop
from mujoco_skills.orchestrator.augment import augment
from mujoco_skills.orchestrator.prompt import AUTHORING_PROMPT
from mujoco_skills.orchestrator.providers.base import LLMProvider
from mujoco_skills.orchestrator.schema import (
    AUGMENT_INPUT_SCHEMA,
    PROPOSE_PLAN_SCHEMA,
    REASSIGN_INPUT_SCHEMA,
    REMOVE_TASK_INPUT_SCHEMA,
    REVISE_ORDER_INPUT_SCHEMA,
    SET_PLACE_PIN_INPUT_SCHEMA,
    UPDATE_MOVE_INPUT_SCHEMA,
    AugmentedAction,
    AuthoringResult,
    Message,
    SemanticTask,
    ToolSpec,
)

AUGMENT_TOOL = ToolSpec(
    name="augment",
    description=(
        "Deterministically expand affected move intents into ordered semantic "
        "move/open/close actions. Call this only for newly added or changed "
        "moves, never for unchanged current_plan actions."
    ),
    parameters=AUGMENT_INPUT_SCHEMA,
)

PROPOSE_PLAN_TOOL = ToolSpec(
    name="propose_plan",
    description=(
        "Finish the authoring turn. This submits, never describes, the plan: "
        "it does NOT take an actions payload. status=committed finalizes the "
        "plan exactly as already built by augment/remove_task/update_move/reassign/"
        "revise_order/set_place_pin (reason must be null). status=ungroundable leaves the "
        "existing plan untouched and requires a non-null reason explaining "
        "what could not be grounded."
    ),
    parameters=PROPOSE_PLAN_SCHEMA,
)

REASSIGN_TOOL = ToolSpec(
    name="reassign",
    description=(
        "Atomically reassign exactly the named semantic action(s) to a robot and "
        "place them in that robot's program order. To move a whole destination "
        "workflow, pass every affected move/open/close action id. If the user names "
        "an exact destination-lane slot, pass after_action_id (null means first); "
        "otherwise omit it and the server chooses a topology-safe slot."
    ),
    parameters=REASSIGN_INPUT_SCHEMA,
)

REMOVE_TASK_TOOL = ToolSpec(
    name="remove_task",
    description=(
        "Atomically remove the named existing semantic move task(s). Pass exact "
        "current_plan ids (or ids resolved from plan-task handles), never labels. "
        "The server contracts semantic dependencies and removes an explicit "
        "open/close envelope only when no move remains inside it."
    ),
    parameters=REMOVE_TASK_INPUT_SCHEMA,
)

UPDATE_MOVE_TOOL = ToolSpec(
    name="update_move",
    description=(
        "Atomically update an existing move action's object and/or destination "
        "by its stable action id. The action id, robot assignment, lock, and "
        "semantic dependencies are preserved. Destination changes clean up an "
        "empty old container session, reuse or create the new destination "
        "session, and clear a pin bound to the old destination."
    ),
    parameters=UPDATE_MOVE_INPUT_SCHEMA,
)

REVISE_ORDER_TOOL = ToolSpec(
    name="revise_order",
    description=(
        "Atomically revise the order of existing semantic actions. "
        "Use place_relative for one action relative to another, or set_sequence "
        "for a same-robot list. immediately_before/after means adjacent positional "
        "insertion on one robot; use it for requests phrased as add/insert a task "
        "before/after an anchor. Ordinary before/after means precedence without "
        "adjacency, and cross-robot before/after becomes a semantic precedence "
        "dependency. Existing open/move/close container boundaries "
        "are preserved and unsafe edits are rejected atomically."
    ),
    parameters=REVISE_ORDER_INPUT_SCHEMA,
)

SET_PLACE_PIN_TOOL = ToolSpec(
    name="set_place_pin",
    description=(
        "Bind an existing move action's destination to a scene_refs pin "
        "handle, or clear it with pin=null. The pin must appear in this "
        "turn's scene_refs and its bound facility must match the action's "
        "dest. Never hand-write place_at_pin anywhere else."
    ),
    parameters=SET_PLACE_PIN_INPUT_SCHEMA,
)

# This constant is intentionally reviewable: compile_plan and decompose must
# never enter the authoring loop.
AUTHORING_TOOLS = (
    AUGMENT_TOOL,
    REMOVE_TASK_TOOL,
    UPDATE_MOVE_TOOL,
    REASSIGN_TOOL,
    REVISE_ORDER_TOOL,
    SET_PLACE_PIN_TOOL,
    PROPOSE_PLAN_TOOL,
)


def author(
    messages: list[dict],
    current_plan: list[AugmentedAction],
    provider: LLMProvider,
    manifest: dict,
    *,
    max_iters: int = loop.DEFAULT_MAX_ITERS,
    on_event: Callable[[str, object], None] | None = None,
    scene_refs: list | None = None,
    plan_refs: list[dict] | None = None,
) -> AuthoringResult:
    """Apply the latest NL turn to the authoritative semantic current_plan."""
    latest_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("role") == "user"
        ),
        None,
    )
    if latest_index is None:
        raise ValueError("authoring requires at least one user message")

    scene_refs = scene_refs or []
    plan_refs = _validate_plan_refs(plan_refs or [], current_plan)
    context = _context(current_plan, manifest, scene_refs, plan_refs)
    prior = [
        Message(str(item["role"]), str(item.get("content", "")))
        for item in messages[:latest_index]
        if item.get("role") in {"user", "assistant"}
    ]
    history = [
        Message("system", AUTHORING_PROMPT),
        Message(
            "user",
            "Structured authoritative context (not a user request):\n"
            f"{context}",
        ),
        *prior,
        Message(
            "system",
            "FINAL STATE SYNC: current_plan in the structured context is "
            "authoritative. Apply only the next/latest user message to that "
            "exact plan. Preserve every unmentioned action and every manual "
            "omission.",
        ),
    ]
    executor = _AuthoringExecutor(manifest, current_plan, scene_refs, plan_refs)
    full_history = loop.run(
        str(messages[latest_index].get("content", "")),
        provider,
        list(AUTHORING_TOOLS),
        executor.execute,
        history=history,
        max_iters=max_iters,
        terminal_tools={"propose_plan"},
        on_event=on_event,
    )
    if executor.result is not None:
        return executor.result

    # If the model stopped in prose or exhausted the loop without proposing a
    # plan, force only the semantic terminal tool. No compile/decompose tool is
    # introduced by this recovery path.
    force_tool = getattr(provider, "force_tool", None)
    if callable(force_tool):
        closing_messages = [
            *full_history,
            Message(
                "system",
                "Close now by calling propose_plan with status='committed' "
                "(if the working plan already reflects the request) or "
                "status='ungroundable' with a non-null reason. Do not pass "
                "an actions payload -- propose_plan only submits the working "
                "plan already built by augment/remove_task/update_move/reassign/revise_order/"
                "set_place_pin. Do not call or describe any other operation.",
            ),
        ]
        reply = force_tool(
            closing_messages,
            [PROPOSE_PLAN_TOOL],
            "propose_plan",
        )
        for call in reply.tool_calls or []:
            if call.name != "propose_plan":
                continue
            if on_event:
                on_event("tool_call", call)
            result = executor.execute(call.name, call.arguments)
            if on_event:
                on_event("tool_result", (call.name, result))
            if executor.result is not None:
                return executor.result

    if executor.last_propose_error:
        raise ValueError(
            "authoring loop ended without a valid propose_plan call: "
            f"{executor.last_propose_error}"
        )
    raise ValueError("authoring loop ended without a valid propose_plan call")


def _context(
    current_plan: list[AugmentedAction],
    manifest: dict,
    scene_refs: list | None = None,
    plan_refs: list[dict] | None = None,
) -> str:
    scene_refs = scene_refs or []
    referenced_objects = [
        str(ref.get("name"))
        for ref in scene_refs
        if isinstance(ref, dict) and ref.get("kind") == "object" and ref.get("name")
    ]
    referenced_pins = [
        {"handle": ref.get("handle"), "facility": ref.get("on_facility")}
        for ref in scene_refs
        if isinstance(ref, dict)
        and ref.get("kind") == "position"
        and ref.get("on_facility")
        and ref.get("handle")
    ]
    objects = {
        name: {
            "label": spec.get("label"),
            "home_facility": spec.get("home_facility"),
        }
        for name, spec in manifest.get("objects", {}).items()
    }
    facilities = {}
    for name, spec in manifest.get("facilities", {}).items():
        place = spec.get("place")
        articulation = spec.get("articulation") or {}
        facilities[name] = {
            "label": spec.get("label"),
            "can_place": place is not None,
            "place": (
                {
                    "kind": place.get("kind", "surface"),
                    "access": place.get("access"),
                    "requires_open": place.get("requires_open"),
                }
                if place is not None
                else None
            ),
            "articulation_skills": articulation.get("skills") or {},
        }
    return json.dumps(
        {
            "current_plan": [asdict(action) for action in current_plan],
            "objects": objects,
            "facilities": facilities,
            # Grounding: the objects/pins the user attached to THIS message via
            # scene_refs. Pins here are only the bindable ones (on_facility set,
            # so a handle exists); see prompt.py for how the LLM is told to use
            # "(pin pK)" markers in the user text together with this list.
            "user_referenced": {
                "objects": referenced_objects,
                "pins": referenced_pins,
                "plan_tasks": plan_refs or [],
            },
        },
        ensure_ascii=False,
    )


class _AuthoringExecutor:
    def __init__(
        self,
        manifest: dict,
        current_plan: list[AugmentedAction],
        scene_refs: list | None = None,
        plan_refs: list[dict] | None = None,
    ):
        self.manifest = manifest
        # This ordered list is the sole mutable authoring state.  The LLM may
        # describe a final plan, but cannot use propose_plan to smuggle an
        # arbitrary rewrite around the deterministic mutation tools.
        self.working_actions = list(current_plan)
        self.current_plan = self.working_actions
        self.result: AuthoringResult | None = None
        # Preserved so a loop that never lands a valid propose_plan call can
        # surface the real underlying rejection reason instead of only the
        # unified "authoring loop ended without a valid propose_plan call".
        self.last_propose_error: str | None = None
        self.action_by_id = {action.id: action for action in self.working_actions}
        self.plan_refs = plan_refs or []
        # handle -> facility, built from this turn's bindable scene_refs
        # position pins (on_facility != None). Used only to validate NEW
        # place_at_pin annotations via set_place_pin; never persisted across
        # turns.
        self.pins = {
            str(ref.get("handle")): str(ref.get("on_facility"))
            for ref in (scene_refs or [])
            if isinstance(ref, dict)
            and ref.get("kind") == "position"
            and ref.get("on_facility")
            and ref.get("handle")
        }
        # Pin annotations arriving WITH the current plan were validated the
        # turn they were authored and are persistent semantic state. A later
        # turn's scene_refs won't re-include those handles, so an action
        # echoing its existing annotation must not be re-validated against
        # this turn's (unrelated) pins — that rejected every propose_plan of
        # the follow-up turn.
        self.carried_pins = {
            action.id: action.place_at_pin
            for action in current_plan
            if getattr(action, "place_at_pin", None)
        }

    def execute(self, name: str, arguments: dict) -> dict:
        try:
            if name == "augment":
                intents = _parse_move_intents(arguments.get("actions"), self.manifest)
                actions = augment(
                    intents,
                    self.manifest,
                    existing=list(self.working_actions),
                )
                self.action_by_id.update((action.id, action) for action in actions)
                self.working_actions = _merge_augmented_actions(
                    self.working_actions, actions, self.manifest
                )
                self.action_by_id = {
                    action.id: action for action in self.working_actions
                }
                return {"actions": [asdict(action) for action in actions]}
            if name == "remove_task":
                removed, removed_envelope = self._remove_tasks(arguments.get("task_ids"))
                return {
                    "removed": [asdict(action) for action in removed],
                    "removed_envelope": [asdict(action) for action in removed_envelope],
                }
            if name == "update_move":
                updated = self._update_move(
                    arguments.get("action_id"),
                    object_name=arguments.get("object"),
                    dest=arguments.get("dest"),
                    object_supplied="object" in arguments,
                    dest_supplied="dest" in arguments,
                )
                return {"action": asdict(updated)}
            if name == "reassign":
                actions = self._reassign(
                    arguments.get("action_ids"),
                    arguments.get("robot"),
                    arguments.get("after_action_id"),
                    position_supplied="after_action_id" in arguments,
                )
                return {"actions": [asdict(action) for action in actions]}
            if name == "revise_order":
                actions = self._revise_order(arguments.get("operations"))
                return {"actions": [asdict(action) for action in actions]}
            if name == "set_place_pin":
                updated = self._set_place_pin(
                    arguments.get("action_id"), arguments.get("pin")
                )
                return {"action": asdict(updated)}
            if name == "propose_plan":
                # propose_plan is submit-only: it never receives or parses an
                # `actions` payload. The committed plan is always exactly
                # `working_actions`, the server-side state already mutated by
                # augment/remove_task/update_move/reassign/revise_order/set_place_pin.
                # This removes
                # the second, model-authored copy of the plan that could
                # silently diverge from working_actions and reject a
                # perfectly valid mutation sequence (see
                # docs/propose_plan_actions_removal_handoff.md).
                status = arguments.get("status")
                if status not in {"committed", "ungroundable"}:
                    raise ValueError(
                        f"propose_plan status must be 'committed' or "
                        f"'ungroundable', got {status!r}"
                    )
                message = str(arguments.get("message") or "").strip()
                if not message:
                    raise ValueError("propose_plan requires a non-empty message")
                reason_value = arguments.get("reason")
                reason = str(reason_value).strip() if reason_value is not None else None
                if status == "committed":
                    if reason:
                        raise ValueError(
                            "propose_plan status=committed requires reason to be null"
                        )
                    # Final server-side invariant checks before committing;
                    # never a place to mutate working_actions.
                    _validate_semantic_dependencies(self.working_actions)
                    _validate_same_robot_dependency_order(self.working_actions)
                    reason = None
                else:  # ungroundable
                    if not reason:
                        raise ValueError(
                            "propose_plan status=ungroundable requires a non-empty reason"
                        )
                    # working_actions is left exactly as it was: an
                    # ungroundable request preserves any pre-existing plan
                    # instead of discarding it.
                committed_actions = list(self.working_actions)
                self.result = AuthoringResult(
                    actions=committed_actions,
                    message=message,
                    reason=reason,
                )
                return {
                    "actions": [asdict(action) for action in committed_actions],
                    "message": message,
                    "reason": reason,
                }
            return {"error": f"unknown authoring tool {name!r}"}
        except (KeyError, TypeError, ValueError) as exc:
            message = str(exc)
            if name == "propose_plan":
                self.last_propose_error = message
            return {"error": message}

    def _reassign(
        self,
        raw_ids: object,
        robot: object,
        after_action_id: object = None,
        *,
        position_supplied: bool = False,
    ) -> list[AugmentedAction]:
        """Reassign actions and select a valid destination-lane slot atomically.

        A robot change also creates new per-robot program-order edges. Merely
        replacing ``action.robot`` can therefore close a cycle with implicit
        shared-container edges (for example, place -> open -> earlier place).
        Explicit slots are strict; when no slot is supplied, candidates are
        searched nearest to the actions' prior document position.
        """
        if robot not in {"robot0", "robot1"}:
            raise ValueError(f"invalid robot {robot!r}")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("reassign requires a non-empty action_ids array")
        if any(not isinstance(value, str) or not value for value in raw_ids):
            raise ValueError("reassign action_ids must be non-empty strings")
        if len(set(raw_ids)) != len(raw_ids):
            raise ValueError("reassign action_ids must be unique")
        target_ids = set(raw_ids)
        for aid in raw_ids:
            action = self.action_by_id.get(aid)
            if action is None:
                raise ValueError(f"unknown action id {aid!r}")

        if position_supplied and after_action_id is not None:
            if not isinstance(after_action_id, str) or not after_action_id:
                raise ValueError(
                    "reassign after_action_id must be a non-empty string or null"
                )
            anchor = self.action_by_id.get(after_action_id)
            if anchor is None:
                raise ValueError(
                    f"unknown reassign after_action_id {after_action_id!r}"
                )
            if anchor.id in target_ids:
                raise ValueError("reassign insertion anchor cannot be reassigned")
            if anchor.robot != robot:
                raise ValueError(
                    "reassign insertion anchor must belong to the destination robot"
                )

        original = list(self.working_actions)
        selected = [
            replace(action, robot=robot, robot_locked=True)
            for action in original
            if action.id in target_ids
        ]
        remaining = [action for action in original if action.id not in target_ids]
        original_start = min(
            index for index, action in enumerate(original) if action.id in target_ids
        )

        def inserted_at(index: int) -> list[AugmentedAction]:
            return [*remaining[:index], *selected, *remaining[index:]]

        if position_supplied:
            if after_action_id is None:
                insert_at = next(
                    (index for index, action in enumerate(remaining)
                     if action.robot == robot),
                    len(remaining),
                )
            else:
                insert_at = next(
                    index for index, action in enumerate(remaining)
                    if action.id == after_action_id
                ) + 1
            candidates = [inserted_at(insert_at)]
        else:
            # Preserve the old document position when it is already safe. If
            # not, consider every stable insertion boundary, nearest first.
            rewritten_in_place = [
                replace(action, robot=robot, robot_locked=True)
                if action.id in target_ids else action
                for action in original
            ]
            candidates = [rewritten_in_place]
            insertion_indices = sorted(
                range(len(remaining) + 1),
                key=lambda index: (abs(index - original_start), index),
            )
            candidates.extend(inserted_at(index) for index in insertion_indices)

        failure: ValueError | None = None
        candidate = None
        seen_orders: set[tuple[str, ...]] = set()
        for proposed in candidates:
            order = tuple(action.id for action in proposed)
            if order in seen_orders:
                continue
            seen_orders.add(order)
            try:
                _validate_reassign_topology(proposed, self.manifest)
            except ValueError as exc:
                failure = exc
                continue
            candidate = proposed
            break

        if candidate is None:
            detail = str(failure) if failure is not None else "no candidate slots"
            if position_supplied:
                raise ValueError(f"requested reassign position is invalid: {detail}")
            raise ValueError(f"reassign has no topology-safe position: {detail}")

        self.working_actions = candidate
        self.current_plan = candidate
        self.action_by_id = {action.id: action for action in candidate}
        return [self.action_by_id[action_id] for action_id in raw_ids]

    def _update_move(
        self,
        raw_action_id: object,
        *,
        object_name: object,
        dest: object,
        object_supplied: bool,
        dest_supplied: bool,
    ) -> AugmentedAction:
        """Atomically replace a move's object and/or destination.

        Destination changes can move an action between open/close envelopes.
        Build that candidate on a temporary executor and commit it only after
        the complete semantic graph validates.
        """
        if not isinstance(raw_action_id, str) or not raw_action_id:
            raise ValueError("update_move requires a non-empty action_id")
        original = self.action_by_id.get(raw_action_id)
        if original is None:
            raise ValueError(f"unknown action id {raw_action_id!r}")
        if original.op != "move":
            raise ValueError(
                f"update_move only accepts move actions; "
                f"{raw_action_id!r} is op={original.op!r}"
            )
        if not object_supplied and not dest_supplied:
            raise ValueError("update_move requires object and/or dest")

        next_object = object_name if object_supplied else original.object
        next_dest = dest if dest_supplied else original.dest
        if (
            not isinstance(next_object, str)
            or next_object not in self.manifest.get("objects", {})
        ):
            raise ValueError(f"unknown object {next_object!r}")
        if not isinstance(next_dest, str):
            raise ValueError(f"destination {next_dest!r} is not placeable")
        facility = self.manifest.get("facilities", {}).get(next_dest)
        if not facility or facility.get("place") is None:
            raise ValueError(f"destination {next_dest!r} is not placeable")

        destination_changed = next_dest != original.dest
        updated = replace(
            original,
            object=next_object,
            dest=next_dest,
            place_at_pin=None if destination_changed else original.place_at_pin,
        )
        if not destination_changed:
            self.working_actions = [
                updated if action.id == original.id else action
                for action in self.working_actions
            ]
            self.current_plan = self.working_actions
            self.action_by_id = {action.id: action for action in self.working_actions}
            return updated

        # Reuse the existing deterministic session cleanup on an isolated copy,
        # so no intermediate remove is visible if the update later fails.
        temp = _AuthoringExecutor(self.manifest, list(self.working_actions))
        removal = temp.execute("remove_task", {"task_ids": [original.id]})
        if "error" in removal:
            raise ValueError(removal["error"])

        additions = augment(
            [SemanticTask(original.id, "move", next_object, next_dest)],
            self.manifest,
            existing=list(temp.working_actions),
        )
        rewritten_additions: list[AugmentedAction] = []
        for action in additions:
            if action.op == "move":
                rewritten_additions.append(updated)
            else:
                # These support actions exist exclusively because this update
                # created a fresh destination session. Keep that workflow with
                # the move's existing robot while leaving it user-unlocked.
                rewritten_additions.append(replace(action, robot=original.robot))
        candidate = _merge_augmented_actions(
            temp.working_actions, rewritten_additions, self.manifest
        )

        # remove_task contracts dependencies while the stable move id is
        # temporarily absent. Restore every still-valid original dependency;
        # only references to an envelope that was actually removed stay
        # contracted.
        original_after = {action.id: action.after for action in self.working_actions}
        candidate_ids = {action.id for action in candidate}
        restored: list[AugmentedAction] = []
        for action in candidate:
            if action.id not in original_after:
                restored.append(action)
                continue
            old_after = original_after[action.id] or []
            surviving = [item for item in old_after if item in candidate_ids]
            removed = any(item not in candidate_ids for item in old_after)
            if removed:
                for item in action.after or []:
                    if item in candidate_ids and item not in surviving:
                        surviving.append(item)
            restored.append(replace(action, after=surviving or None))

        _validate_semantic_dependencies(restored)
        _validate_same_robot_dependency_order(restored)
        self.working_actions = restored
        self.current_plan = self.working_actions
        self.action_by_id = {action.id: action for action in restored}
        self.carried_pins.pop(original.id, None)
        return self.action_by_id[original.id]

    def _remove_tasks(
        self, raw_ids: object
    ) -> tuple[list[AugmentedAction], list[AugmentedAction]]:
        """Remove move tasks atomically and keep the semantic graph valid.

        Explicit contiguous open..close envelopes are removed only when every
        move they contain is removed. Incomplete/manual envelopes are left
        untouched. Dependencies through removed actions are contracted onto
        their nearest surviving predecessors so A -> B -> C becomes A -> C
        when B is removed.
        """
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("remove_task requires a non-empty task_ids array")
        if any(not isinstance(value, str) or not value for value in raw_ids):
            raise ValueError("remove_task task_ids must be non-empty strings")
        if len(set(raw_ids)) != len(raw_ids):
            raise ValueError("remove_task task_ids must be unique")

        target_ids = set(raw_ids)
        removed = []
        for action_id in raw_ids:
            action = self.action_by_id.get(action_id)
            if action is None:
                raise ValueError(f"unknown action id {action_id!r}")
            if action.op != "move":
                raise ValueError(
                    "remove_task only accepts semantic move tasks; "
                    f"{action_id!r} is op={action.op!r}"
                )
            removed.append(action)

        # Snapshot envelopes before removing anything. Several targets may
        # share one envelope, so key by its stable endpoint ids.
        envelopes: dict[tuple[str, str], tuple[int, int, list[str]]] = {}
        for action_id in raw_ids:
            session = _session_for(self.working_actions, action_id)
            if session is None:
                continue
            start, end, move_ids = session
            key = (self.working_actions[start].id, self.working_actions[end].id)
            envelopes[key] = (start, end, move_ids)

        envelope_ids: set[str] = set()
        for (open_id, close_id), (_start, _end, move_ids) in envelopes.items():
            if set(move_ids) <= target_ids:
                envelope_ids.update((open_id, close_id))

        all_removed_ids = target_ids | envelope_ids
        by_id = dict(self.action_by_id)

        def surviving_predecessors(action_id: str, trail: set[str]) -> list[str]:
            if action_id not in all_removed_ids:
                return [action_id]
            if action_id in trail:
                raise ValueError("semantic dependencies contain a cycle")
            action = by_id[action_id]
            result: list[str] = []
            for predecessor in action.after or []:
                for survivor in surviving_predecessors(predecessor, trail | {action_id}):
                    if survivor not in result:
                        result.append(survivor)
            return result

        candidate: list[AugmentedAction] = []
        for action in self.working_actions:
            if action.id in all_removed_ids:
                continue
            predecessors: list[str] = []
            for predecessor in action.after or []:
                for survivor in surviving_predecessors(predecessor, set()):
                    if survivor != action.id and survivor not in predecessors:
                        predecessors.append(survivor)
            candidate.append(replace(action, after=predecessors or None))

        _validate_semantic_dependencies(candidate)
        _validate_same_robot_dependency_order(candidate)

        removed_envelope = [
            action for action in self.working_actions if action.id in envelope_ids
        ]
        self.working_actions = candidate
        self.current_plan = self.working_actions
        self.action_by_id = {action.id: action for action in candidate}
        for action_id in all_removed_ids:
            self.carried_pins.pop(action_id, None)
        return removed, removed_envelope
    def _set_place_pin(self, raw_action_id: object, raw_pin: object) -> AugmentedAction:
        """Deterministically bind/clear one move action's place_at_pin.

        Validates: the action exists and is op=="move"; a non-null pin must
        appear in this turn's scene_refs and be bound to the same facility as
        action.dest. A pin unchanged from a prior turn's carried annotation is
        not re-validated against this turn's scene_refs (see
        `self.carried_pins`); explicit ``pin: null`` clears any existing
        annotation.
        """
        if not isinstance(raw_action_id, str) or not raw_action_id:
            raise ValueError("set_place_pin requires a non-empty action_id")
        action = self.action_by_id.get(raw_action_id)
        if action is None:
            raise ValueError(f"unknown action id {raw_action_id!r}")
        if action.op != "move":
            raise ValueError(
                f"place_at_pin is only valid on move actions; "
                f"{raw_action_id!r} is op={action.op!r}"
            )
        if raw_pin is None:
            updated = replace(action, place_at_pin=None)
        else:
            if not isinstance(raw_pin, str) or not raw_pin:
                raise ValueError("set_place_pin pin must be a non-empty string or null")
            if raw_pin != self.carried_pins.get(action.id):
                facility = self.pins.get(raw_pin)
                if facility is None:
                    raise ValueError(f"unknown place_at_pin handle {raw_pin!r}")
                if facility != action.dest:
                    raise ValueError(
                        f"place_at_pin {raw_pin!r} is pinned to {facility!r}, "
                        f"not dest {action.dest!r} on action {raw_action_id!r}"
                    )
            updated = replace(action, place_at_pin=raw_pin)
        self.working_actions = [
            updated if item.id == raw_action_id else item for item in self.working_actions
        ]
        self.action_by_id[raw_action_id] = updated
        return updated

    def _revise_order(self, raw_operations: object) -> list[AugmentedAction]:
        """Apply a batch of semantic-action edits to a copy, then commit atomically."""
        if not isinstance(raw_operations, list) or not raw_operations:
            raise ValueError("revise_order requires a non-empty operations array")
        candidate = list(self.working_actions)
        for raw in raw_operations:
            if not isinstance(raw, dict):
                raise ValueError("revise_order operations must be objects")
            kind = raw.get("type")
            session_constraints = _container_session_constraints(candidate)
            if kind == "place_relative":
                candidate = _place_relative(candidate, raw)
            elif kind == "set_sequence":
                candidate = _set_sequence(candidate, raw)
            else:
                raise ValueError(f"unknown revise_order operation {kind!r}")
            _validate_container_session_constraints(candidate, session_constraints)
            _validate_semantic_dependencies(candidate)
            _validate_same_robot_dependency_order(candidate)
        # Every operation and every final dependency validated before mutation.
        self.working_actions = candidate
        self.action_by_id = {action.id: action for action in candidate}
        return candidate


def remove_tasks(
    current_plan: list[AugmentedAction], task_ids: list[str]
) -> list[AugmentedAction]:
    """Apply the deterministic ``remove_task`` mutation without an LLM loop.

    Manual Gantt deletion uses the same semantic mutation as Author: the batch
    is atomic, dependencies are contracted, and now-empty explicit container
    envelopes are removed. Keeping this as a small public adapter prevents the
    structured Sync path from growing a second implementation of deletion.
    """
    executor = _AuthoringExecutor({}, current_plan)
    result = executor.execute("remove_task", {"task_ids": task_ids})
    if "error" in result:
        raise ValueError(result["error"])
    return list(executor.working_actions)


def _validate_plan_refs(raw_refs: object, current_plan: list[AugmentedAction]) -> list[dict]:
    """Validate the tiny plan-reference wire contract against current state."""
    if not isinstance(raw_refs, list):
        raise ValueError("plan_refs must be an array")
    by_id = {action.id: action for action in current_plan}
    seen_handles: set[str] = set()
    resolved: list[dict] = []
    for raw in raw_refs:
        if not isinstance(raw, dict):
            raise ValueError("plan_refs entries must be objects")
        if raw.get("kind") != "plan_task":
            raise ValueError("plan_refs entries must have kind 'plan_task'")
        handle = raw.get("handle")
        action_id = raw.get("action_id")
        if not isinstance(handle, str) or not handle.strip():
            raise ValueError("plan_refs entries require a non-empty handle")
        if handle in seen_handles:
            raise ValueError(f"duplicate plan reference handle {handle!r}")
        if not isinstance(action_id, str) or not action_id.strip():
            raise ValueError("plan_refs entries require a non-empty action_id")
        if action_id not in by_id:
            raise ValueError(f"referenced plan task no longer exists: {action_id!r}")
        seen_handles.add(handle)
        # Do not echo client-supplied task snapshots into the prompt.  The
        # matching item in current_plan is the one authoritative source.
        resolved.append({"handle": handle, "action_id": action_id})
    return resolved


def _merge_augmented_actions(
    existing: list[AugmentedAction],
    additions: list[AugmentedAction],
    manifest: dict | None = None,
) -> list[AugmentedAction]:
    """Insert an augmented move into an existing open..close envelope.

    `augment` intentionally returns only new actions for an existing session.
    Destination moves are inserted before their existing close. Source moves
    keep the existing closer and extend its dependencies so cross-turn
    retrieval never creates or races a second close.
    """
    result = list(existing)
    for action in additions:
        if action.op == "move" and action.dest:
            source = None
            if manifest is not None and action.object:
                source = (
                    (manifest.get("objects") or {}).get(action.object, {})
                    .get("home_facility")
                )
            if source and source != action.dest:
                source_close_index = next(
                    (
                        index
                        for index, item in enumerate(result)
                        if item.op == "close" and item.facility == source
                    ),
                    None,
                )
                if source_close_index is not None:
                    closer = result[source_close_index]
                    dependencies = list(closer.after or [])
                    if action.id not in dependencies:
                        dependencies.append(action.id)
                        result[source_close_index] = replace(
                            closer, after=dependencies
                        )
            close_index = next(
                (index for index, item in enumerate(result)
                 if item.op == "close" and item.facility == action.dest),
                None,
            )
            if close_index is not None:
                result.insert(close_index, action)
                continue
        result.append(action)
    return result


def _action_by_id(actions: list[AugmentedAction], action_id: object) -> AugmentedAction:
    if not isinstance(action_id, str) or not action_id:
        raise ValueError("ordering action ids must be non-empty strings")
    action = next((item for item in actions if item.id == action_id), None)
    if action is None:
        raise ValueError(f"unknown ordering action id {action_id!r}")
    return action


def _move_by_id(actions: list[AugmentedAction], action_id: object) -> AugmentedAction:
    action = _action_by_id(actions, action_id)
    if action.op != "move":
        raise ValueError(f"ordering only accepts move actions; {action_id!r} is {action.op!r}")
    return action


def _container_session_constraints(
    actions: list[AugmentedAction],
) -> list[tuple[str, str, tuple[str, ...]]]:
    """Snapshot existing open..close membership before an ordering edit.

    The user may move either endpoint relative to unrelated work, but every
    move already protected by that endpoint pair must remain between it. This
    lets open/close be first-class ordering anchors without allowing a door to
    close before its placements finish.
    """
    constraints: list[tuple[str, str, tuple[str, ...]]] = []
    for start, opener in enumerate(actions):
        if opener.op != "open" or not opener.facility:
            continue
        for end in range(start + 1, len(actions)):
            candidate = actions[end]
            if candidate.op == "open" and candidate.facility == opener.facility:
                break
            if candidate.op != "close" or candidate.facility != opener.facility:
                continue
            members = tuple(
                action.id for action in actions[start + 1:end]
                if action.op == "move" and action.dest == opener.facility
            )
            constraints.append((opener.id, candidate.id, members))
            break
    return constraints


def _validate_container_session_constraints(
    actions: list[AugmentedAction],
    constraints: list[tuple[str, str, tuple[str, ...]]],
) -> None:
    positions = {action.id: index for index, action in enumerate(actions)}
    for open_id, close_id, move_ids in constraints:
        open_index = positions[open_id]
        close_index = positions[close_id]
        if open_index >= close_index:
            raise ValueError(
                f"ordering would place {close_id!r} before its opener {open_id!r}"
            )
        escaped = [
            action_id for action_id in move_ids
            if not open_index < positions[action_id] < close_index
        ]
        if escaped:
            raise ValueError(
                "ordering would move container work outside its open/close "
                f"session: {escaped}"
            )


def _session_for(actions: list[AugmentedAction], action_id: str) -> tuple[int, int, list[str]] | None:
    """Return a conservative open..close envelope containing this move.

    We intentionally only recognise a contiguous explicit facility session.
    That makes manual close deletion safe: without both envelope endpoints no
    session is inferred and the move is treated as ordinary work.
    """
    move_index = next(i for i, action in enumerate(actions) if action.id == action_id)
    move = actions[move_index]
    if not move.dest:
        return None
    for start in range(move_index - 1, -1, -1):
        opener = actions[start]
        if opener.op == "close" and opener.facility == move.dest:
            break
        if opener.op != "open" or opener.facility != move.dest:
            continue
        for end in range(move_index + 1, len(actions)):
            closer = actions[end]
            if closer.op == "open" and closer.facility == move.dest:
                break
            if closer.op == "close" and closer.facility == move.dest:
                moves = [a.id for a in actions[start + 1:end] if a.op == "move" and a.dest == move.dest]
                return start, end, moves
    return None


def _compatible_move_reorder(actions: list[AugmentedAction], ids: list[str]) -> None:
    sessions = [_session_for(actions, action_id) for action_id in ids]
    known = [session for session in sessions if session is not None]
    if not known:
        return
    if any(session != known[0] for session in sessions):
        raise ValueError(
            "cannot move a task across a shared open/close session boundary; "
            "keep referenced moves inside the same container session"
        )


def _is_single_task_envelope(
    actions: list[AugmentedAction], action_id: str, session: tuple[int, int, list[str]] | None
) -> bool:
    """A move may cross a session boundary only with its whole simple envelope."""
    return bool(
        session
        and session[2] == [action_id]
        and session[1] == session[0] + 2
        and actions[session[0] + 1].id == action_id
    )


def _block_for_move(
    actions: list[AugmentedAction], action_id: str
) -> tuple[int, int, tuple[int, int, list[str]] | None]:
    """Return the atomic block that must move with ``action_id``.

    A move inside an explicit open..close session cannot leave that envelope.
    For a shared session the whole envelope is therefore the ordering block;
    for an ordinary move the block is just that move.  Callers that reorder
    moves *within the same session* handle that case before reaching here.
    """
    session = _session_for(actions, action_id)
    if session is None:
        index = next(i for i, action in enumerate(actions) if action.id == action_id)
        return index, index, None
    return session[0], session[1], session


def _effective_block_bounds(
    actions: list[AugmentedAction], action_id: str
) -> tuple[int, int]:
    """Bounds used to compare execution order without splitting sessions."""
    session = _session_for(actions, action_id)
    if session is not None:
        return session[0], session[1]
    index = next(i for i, action in enumerate(actions) if action.id == action_id)
    return index, index


def _relative_order_already_satisfied(
    actions: list[AugmentedAction], task_id: str, anchor_id: str, relation: str
) -> bool:
    """Whether two distinct atomic blocks already satisfy before/after.

    Ordinary before/after is block-relative.  Immediate relations deliberately
    keep flowing through the mutation path because they additionally request
    adjacency.  Moves in one shared session are compared by their own indices,
    since that session's internal order remains authorable.
    """
    if relation not in {"before", "after"}:
        return False
    task_session = _session_for(actions, task_id)
    anchor_session = _session_for(actions, anchor_id)
    if task_session is not None and task_session == anchor_session:
        task_index = next(i for i, action in enumerate(actions) if action.id == task_id)
        anchor_index = next(i for i, action in enumerate(actions) if action.id == anchor_id)
        return task_index < anchor_index if relation == "before" else task_index > anchor_index
    task_start, task_end = _effective_block_bounds(actions, task_id)
    anchor_start, anchor_end = _effective_block_bounds(actions, anchor_id)
    return task_end < anchor_start if relation == "before" else task_start > anchor_end


def _move_block_relative(
    actions: list[AugmentedAction], task_id: str, anchor_id: str, relation: str
) -> list[AugmentedAction]:
    """Move one ordinary task or a whole single-task session as an atomic block."""
    task_start, task_end, _ = _block_for_move(actions, task_id)
    anchor_session = _session_for(actions, anchor_id)
    # An anchor may be shared: relative language refers to its full envelope.
    # It may also be a singleton envelope, in which case its envelope is still
    # the stable insertion boundary.  It is never removed in this operation.
    if anchor_session is None:
        anchor_start = anchor_end = next(
            i for i, action in enumerate(actions) if action.id == anchor_id
        )
    else:
        anchor_start, anchor_end, _ = anchor_session
    block = actions[task_start:task_end + 1]
    result = actions[:task_start] + actions[task_end + 1:]
    removed_before_anchor = task_start < anchor_start
    if removed_before_anchor:
        shift = task_end - task_start + 1
        anchor_start -= shift
        anchor_end -= shift
    insert_at = anchor_start if relation in {"before", "immediately_before"} else anchor_end + 1
    return result[:insert_at] + block + result[insert_at:]


def _set_independent_blocks_sequence(
    actions: list[AugmentedAction], task_ids: list[str]
) -> list[AugmentedAction]:
    """Reorder ordinary tasks/singleton envelopes while preserving all others."""
    blocks: dict[str, tuple[int, int]] = {}
    for action_id in task_ids:
        start, end, _ = _block_for_move(actions, action_id)
        blocks[action_id] = (start, end)
    ordered_ranges = sorted(blocks.values())
    if any(left[1] >= right[0] for left, right in zip(ordered_ranges, ordered_ranges[1:])):
        raise ValueError("set_sequence cannot select overlapping task envelopes")
    insertion_index = ordered_ranges[0][0]
    selected_indices = {
        index for start, end in ordered_ranges for index in range(start, end + 1)
    }
    remaining = [action for index, action in enumerate(actions) if index not in selected_indices]
    before_count = sum(1 for index in range(insertion_index) if index not in selected_indices)
    ordered_blocks = [actions[start:end + 1] for start, end in (blocks[action_id] for action_id in task_ids)]
    flattened = [action for block in ordered_blocks for action in block]
    return remaining[:before_count] + flattened + remaining[before_count:]


def _replace_at_indices(
    actions: list[AugmentedAction], indices: list[int], ordered: list[AugmentedAction]
) -> list[AugmentedAction]:
    result = list(actions)
    for index, action in zip(sorted(indices), ordered):
        result[index] = action
    return result


def _action_order_already_satisfied(
    actions: list[AugmentedAction], task_id: str, anchor_id: str, relation: str
) -> bool:
    if relation not in {"before", "after"}:
        return False
    task_index = next(i for i, action in enumerate(actions) if action.id == task_id)
    anchor_index = next(i for i, action in enumerate(actions) if action.id == anchor_id)
    return task_index < anchor_index if relation == "before" else task_index > anchor_index


def _move_action_relative(
    actions: list[AugmentedAction], task_id: str, anchor_id: str, relation: str
) -> list[AugmentedAction]:
    """Move one semantic action without implicitly moving its container peers."""
    task_index = next(i for i, action in enumerate(actions) if action.id == task_id)
    task = actions[task_index]
    result = actions[:task_index] + actions[task_index + 1:]
    anchor_index = next(i for i, action in enumerate(result) if action.id == anchor_id)
    insert_at = anchor_index if relation in {"before", "immediately_before"} else anchor_index + 1
    return result[:insert_at] + [task] + result[insert_at:]


def _endpoint_session_bounds(
    actions: list[AugmentedAction], action_id: str
) -> tuple[int, int] | None:
    """Return the explicit container session owned by an open/close endpoint."""
    for start, end, _move_ids in _container_session_constraints(actions):
        if action_id in {start, end}:
            positions = {action.id: index for index, action in enumerate(actions)}
            return positions[start], positions[end]
    return None


def _move_range_relative(
    actions: list[AugmentedAction], start: int, end: int,
    anchor_id: str, relation: str,
) -> list[AugmentedAction]:
    """Move one contiguous semantic workflow as a block around an external anchor."""
    anchor_index = next(i for i, action in enumerate(actions) if action.id == anchor_id)
    if start <= anchor_index <= end:
        raise ValueError("cannot move a container session relative to one of its own actions")
    block = actions[start:end + 1]
    result = actions[:start] + actions[end + 1:]
    if start < anchor_index:
        anchor_index -= end - start + 1
    insert_at = (
        anchor_index
        if relation in {"before", "immediately_before"}
        else anchor_index + 1
    )
    return result[:insert_at] + block + result[insert_at:]


def _place_relative(actions: list[AugmentedAction], raw: dict) -> list[AugmentedAction]:
    task = _action_by_id(actions, raw.get("task_id"))
    anchor = _action_by_id(actions, raw.get("anchor_id"))
    relation = raw.get("relation")
    if task.id == anchor.id:
        raise ValueError("ordering task_id and anchor_id must differ")
    if relation not in {"before", "after", "immediately_before", "immediately_after"}:
        raise ValueError(f"invalid ordering relation {relation!r}")
    if task.robot != anchor.robot:
        if relation.startswith("immediately_"):
            raise ValueError("immediately_before/after requires both tasks on the same robot")
        if relation == "after":
            return _add_semantic_after(actions, task.id, anchor.id)
        return _add_semantic_after(actions, anchor.id, task.id)
    if task.op != "move" or anchor.op != "move":
        if _action_order_already_satisfied(actions, task.id, anchor.id, relation):
            return list(actions)
        session = _endpoint_session_bounds(actions, task.id)
        anchor_index = next(
            i for i, action in enumerate(actions) if action.id == anchor.id
        )
        # Moving a closer earlier or an opener later across external work must
        # carry the complete workflow. Moving only the endpoint would strand
        # its protected moves outside open..close, so the session validator
        # would reject an otherwise valid request such as "close the cabinet
        # before the milk task" after that cabinet workflow was just appended.
        carry_session = bool(
            session
            and not session[0] <= anchor_index <= session[1]
            and (
                (task.op == "close" and relation in {"before", "immediately_before"})
                or (task.op == "open" and relation in {"after", "immediately_after"})
            )
        )
        if carry_session:
            result = _move_range_relative(
                actions, session[0], session[1], anchor.id, relation)
        else:
            result = _move_action_relative(actions, task.id, anchor.id, relation)
        if relation in {"before", "immediately_before"}:
            return _remove_semantic_after(result, task.id, anchor.id)
        return _remove_semantic_after(result, anchor.id, task.id)
    if _relative_order_already_satisfied(
            actions, task.id, anchor.id, relation):
        return list(actions)
    task_session = _session_for(actions, task.id)
    anchor_session = _session_for(actions, anchor.id)
    if task_session is not None and task_session == anchor_session:
        _compatible_move_reorder(actions, [task.id, anchor.id])
        task_index = next(i for i, action in enumerate(actions) if action.id == task.id)
        anchor_index = next(i for i, action in enumerate(actions) if action.id == anchor.id)
        result = list(actions)
        result.pop(task_index)
        if task_index < anchor_index:
            anchor_index -= 1
        insert_at = anchor_index if relation in {"before", "immediately_before"} else anchor_index + 1
        result.insert(insert_at, task)
    else:
        result = _move_block_relative(actions, task.id, anchor.id, relation)
    # Within one robot, the task order itself is the precedence.  Treat both
    # immediate and ordinary relative language as a deterministic insertion;
    # untouched tasks retain their relative order.
    # A direct ordering edit supersedes only the pairwise semantic edge that
    # would now contradict it.  Other dependencies stay intact and are checked
    # below, so an unrelated user constraint cannot silently disappear.
    if relation in {"before", "immediately_before"}:
        return _remove_semantic_after(result, task.id, anchor.id)
    return _remove_semantic_after(result, anchor.id, task.id)


def _set_sequence(actions: list[AugmentedAction], raw: dict) -> list[AugmentedAction]:
    raw_ids = raw.get("task_ids")
    if not isinstance(raw_ids, list) or len(raw_ids) < 2:
        raise ValueError("set_sequence requires at least two task_ids")
    if any(not isinstance(action_id, str) or not action_id for action_id in raw_ids):
        raise ValueError("set_sequence task_ids must be non-empty strings")
    if len(set(raw_ids)) != len(raw_ids):
        raise ValueError("set_sequence task_ids must not contain duplicates")
    selected = [_action_by_id(actions, action_id) for action_id in raw_ids]
    if len({action.robot for action in selected}) != 1:
        raise ValueError("set_sequence requires all tasks to belong to one robot")
    if any(action.op != "move" for action in selected):
        indices = [
            next(i for i, action in enumerate(actions) if action.id == action_id)
            for action_id in raw_ids
        ]
        result = _replace_at_indices(actions, indices, selected)
        for earlier_index, earlier in enumerate(selected):
            for later in selected[earlier_index + 1:]:
                result = _remove_semantic_after(result, earlier.id, later.id)
        return result
    sessions = [_session_for(actions, action_id) for action_id in raw_ids]
    shared = [
        session for action_id, session in zip(raw_ids, sessions)
        if session is not None and not _is_single_task_envelope(actions, action_id, session)
    ]
    if shared:
        if not all(session == shared[0] for session in sessions):
            raise ValueError("set_sequence cannot mix a shared session with external task envelopes")
        _compatible_move_reorder(actions, list(raw_ids))
        indices = [next(i for i, action in enumerate(actions) if action.id == action_id) for action_id in raw_ids]
        result = _replace_at_indices(actions, indices, selected)
    else:
        result = _set_independent_blocks_sequence(actions, list(raw_ids))
    # The requested sequence supersedes inverse direct edges among precisely
    # the named tasks.  It does not weaken edges touching unmentioned tasks.
    for earlier_index, earlier in enumerate(selected):
        for later in selected[earlier_index + 1:]:
            result = _remove_semantic_after(result, earlier.id, later.id)
    return result


def _add_semantic_after(
    actions: list[AugmentedAction], dependent_id: str, predecessor_id: str
) -> list[AugmentedAction]:
    result = []
    for action in actions:
        if action.id != dependent_id:
            result.append(action)
            continue
        after = list(action.after or [])
        if predecessor_id not in after:
            after.append(predecessor_id)
        result.append(replace(action, after=after or None))
    return result


def _remove_semantic_after(
    actions: list[AugmentedAction], dependent_id: str, predecessor_id: str
) -> list[AugmentedAction]:
    return [
        replace(action, after=[item for item in (action.after or []) if item != predecessor_id] or None)
        if action.id == dependent_id else action
        for action in actions
    ]


def _validate_semantic_dependencies(actions: list[AugmentedAction]) -> None:
    ids = {action.id for action in actions}
    graph = {action.id: list(action.after or []) for action in actions}
    for action_id, predecessors in graph.items():
        if action_id in predecessors:
            raise ValueError(f"semantic dependency cannot reference itself: {action_id!r}")
        unknown = [predecessor for predecessor in predecessors if predecessor not in ids]
        if unknown:
            raise ValueError(f"unknown semantic dependency ids on {action_id!r}: {unknown}")
        if len(set(predecessors)) != len(predecessors):
            raise ValueError(f"duplicate semantic dependency ids on {action_id!r}")
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(action_id: str) -> None:
        if action_id in visiting:
            raise ValueError("semantic ordering would create a dependency cycle")
        if action_id in visited:
            return
        visiting.add(action_id)
        for predecessor in graph[action_id]:
            visit(predecessor)
        visiting.remove(action_id)
        visited.add(action_id)
    for action_id in graph:
        visit(action_id)


def _validate_same_robot_dependency_order(actions: list[AugmentedAction]) -> None:
    """Reject semantic edges that would cycle with a robot's list order."""
    index = {action.id: position for position, action in enumerate(actions)}
    by_id = {action.id: action for action in actions}
    for action in actions:
        for predecessor in action.after or []:
            previous = by_id[predecessor]
            if previous.robot == action.robot and index[predecessor] > index[action.id]:
                raise ValueError(
                    f"same-robot ordering puts {action.id!r} before required predecessor "
                    f"{predecessor!r}"
                )


def _validate_reassign_topology(
    actions: list[AugmentedAction], manifest: dict
) -> None:
    """Validate dependencies introduced by a candidate robot/order assignment.

    This is the semantic-task analogue of the compiler's container dependency
    graph. It intentionally uses whole move tasks as units: the current author
    and task Gantt can insert an open only between tasks, not between a move's
    pick and destination-navigation steps.
    """
    _validate_semantic_dependencies(actions)
    _validate_same_robot_dependency_order(actions)

    by_id = {action.id: action for action in actions}
    dependencies = {
        action.id: set(action.after or []) for action in actions
    }
    previous_by_robot: dict[str, str] = {}
    for action in actions:
        previous = previous_by_robot.get(action.robot)
        if previous is not None:
            dependencies[action.id].add(previous)
        previous_by_robot[action.robot] = action.id

    for facility_name, facility in manifest.get("facilities", {}).items():
        place = facility.get("place") or {}
        if place.get("kind") != "container":
            continue
        moves = [
            action for action in actions
            if action.op == "move" and action.dest == facility_name
        ]
        if not moves:
            continue
        openers = [
            action for action in actions
            if action.op == "open" and action.facility == facility_name
        ]
        if place.get("requires_open") and len(openers) != 1:
            raise ValueError(
                f"container {facility_name!r} requires exactly one opener"
            )
        if len(openers) > 1:
            raise ValueError(f"container {facility_name!r} has multiple openers")
        if openers:
            opener = openers[0]
            for move in moves:
                dependencies[move.id].add(opener.id)

        closers = [
            action for action in actions
            if action.op == "close" and action.facility == facility_name
        ]
        if len(closers) > 1:
            raise ValueError(f"container {facility_name!r} has multiple closers")
        if closers:
            for move in moves:
                dependencies[closers[0].id].add(move.id)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(action_id: str) -> None:
        if action_id in visiting:
            raise ValueError(
                "reassign position creates an authored/shared-world dependency cycle"
            )
        if action_id in visited:
            return
        visiting.add(action_id)
        for predecessor in dependencies[action_id]:
            if predecessor not in by_id:
                raise ValueError(
                    f"unknown semantic dependency {predecessor!r} on {action_id!r}"
                )
            visit(predecessor)
        visiting.remove(action_id)
        visited.add(action_id)

    for action_id in dependencies:
        visit(action_id)


def parse_actions(raw_actions: object, manifest: dict) -> list[AugmentedAction]:
    """Public wrapper: validate a raw AugmentedAction list against the manifest.

    Used by the HTTP service to deserialize a prior turn's ``current_actions``
    (echoed back from a previous /author response) into typed, validated actions.
    """
    return _parse_plan_actions(raw_actions, manifest)


def _parse_move_intents(raw_actions: object, manifest: dict) -> list[SemanticTask]:
    if not isinstance(raw_actions, list):
        raise ValueError("augment requires an actions array")
    intents = []
    for raw in raw_actions:
        if not isinstance(raw, dict):
            raise ValueError("augment actions must be objects")
        intents.append(
            SemanticTask(
                id=str(raw.get("id") or ""),
                action=str(raw.get("action") or ""),
                object=str(raw.get("object") or ""),
                dest=str(raw.get("dest") or ""),
            )
        )
    return intents


def _parse_plan_actions(raw_actions: object, manifest: dict) -> list[AugmentedAction]:
    if not isinstance(raw_actions, list):
        raise ValueError("propose_plan requires an actions array")
    objects = manifest.get("objects", {})
    facilities = manifest.get("facilities", {})
    actions: list[AugmentedAction] = []
    seen_ids: set[str] = set()
    for raw in raw_actions:
        if not isinstance(raw, dict):
            raise ValueError("propose_plan actions must be objects")
        raw_after = raw.get("after")
        if raw_after is not None and (
            not isinstance(raw_after, list)
            or any(not isinstance(item, str) or not item for item in raw_after)
        ):
            raise ValueError("action after must be null or an array of non-empty action ids")
        action = AugmentedAction(
            id=str(raw.get("id") or ""),
            robot=str(raw.get("robot") or ""),
            op=str(raw.get("op") or ""),
            object=raw.get("object"),
            dest=raw.get("dest"),
            facility=raw.get("facility"),
            target=raw.get("target"),
            via_points=raw.get("via_points"),
            serves=raw.get("serves"),
            robot_locked=bool(raw.get("robot_locked", False)),
            # Carried through, not validated here: this function also
            # deserializes a prior turn's `current_actions`, whose pin handles
            # may be stale (scene_refs are per-turn). Validation happens only
            # for THIS turn's propose_plan call, in _AuthoringExecutor.execute.
            place_at_pin=raw.get("place_at_pin"),
            after=(list(raw_after) if raw_after is not None else None),
        )
        if not action.id or action.id in seen_ids:
            raise ValueError(f"duplicate or empty action id {action.id!r}")
        if action.robot not in {"robot0", "robot1"}:
            raise ValueError(f"invalid robot {action.robot!r}")
        if action.op == "move":
            if action.object not in objects:
                raise ValueError(f"unknown object {action.object!r}")
            facility = facilities.get(action.dest)
            if not facility or facility.get("place") is None:
                raise ValueError(f"destination {action.dest!r} is not placeable")
        elif action.op in {"open", "close"}:
            facility = facilities.get(action.facility)
            skill = (
                ((facility or {}).get("articulation") or {})
                .get("skills", {})
                .get(action.op)
            )
            if not skill:
                raise ValueError(
                    f"facility {action.facility!r} has no {action.op} skill"
                )
        elif action.op == "go_to":
            if not action.target:
                raise ValueError(f"go_to {action.id!r} requires target")
        else:
            raise ValueError(f"unsupported semantic op {action.op!r}")
        seen_ids.add(action.id)
        actions.append(action)
    _validate_semantic_dependencies(actions)
    return actions


def _action_key(action: AugmentedAction) -> str:
    """A structural identity for an action, ignoring `place_at_pin`.

    `place_at_pin` is mutated independently via `set_place_pin` (Phase B),
    so two actions that differ only by that field represent the same
    underlying semantic action. Retained for callers (tests) comparing
    action identity irrespective of pin annotation.
    """
    fields = asdict(action)
    fields.pop("place_at_pin", None)
    return json.dumps(fields, sort_keys=True, separators=(",", ":"))
