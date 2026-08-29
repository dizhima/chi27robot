"""Stateful, globally chronological Conflict Resolver V2."""

from __future__ import annotations

import copy
import json

from mujoco_skills.orchestrator.conflict_payload import build_round_payload
from mujoco_skills.orchestrator.conflict_resolver import resolver_tools
from mujoco_skills.orchestrator.conflict_tools import (
    ToolError,
    analyze_spatial_deadlock,
    apply_add_after,
    apply_handoff_terminal_close,
    apply_insert_go_to,
    apply_insert_yield,
    apply_replan_path,
)
from mujoco_skills.orchestrator.resolver_sessions import terminal_context
from mujoco_skills.orchestrator.protected_pins import (
    completed_plan_or_candidate, exact_after_edges, exact_after_fields,
    mismatched_exact_after_fields, missing_exact_after_edges,
    require_exact_after_edges,
)
from mujoco_skills.orchestrator.resolver_state import (
    GLOBAL_ATTEMPT_CAP,
    MAX_SESSIONS_PER_RUN,
    ResolutionState,
    SESSION_ATTEMPT_CAP,
    conflict_metric,
    find_conflict,
    frozen_step_ids,
    makespan,
    stable_fingerprint,
    step_index,
    step_structure,
)
from mujoco_skills.orchestrator.schema import DEFAULT_ROBOT_IDS
from mujoco_skills.orchestrator.schema import Message, ToolCall


SYSTEM_PROMPT_V2 = """You are a bounded multi-robot scheduling repair strategist.
The deterministic compiler owns geometry. Maintain continuity with every prior
tool result, but act only on the single focus conflict in the newest payload.
Return at most one low-level tool call, copy the exact tool arguments from a
server-generated candidate,
never invent coordinates, and never address object/placement conflicts. The
engine rejects repeated, non-focus, unsafe, or non-improving actions. When an
insert_go_to candidate is marked departure_prerequisite, it is the verified
structural prerequisite for the current facility focus even if that focus is
not yet a final-dwell conflict. If legal candidates are present, choose one
rather than returning an empty response. For a path conflict with a legal
replan_path candidate, choose replan_path before add_after. Use add_after only
when replan_path is unavailable, or when an earlier reroute attempt failed or
did not improve that conflict. For add_after, the engine exposes departure_start
candidates first; departure_complete appears only after all aggressive
candidates for that focus have been attempted. When insert_yield is allowed,
the engine has already proven a spatial priority deadlock; copy one exact
yield_candidate and do not choose replan_path or add_after."""


class ScriptedProposer:
    """Deterministic provider used by golden/loop tests."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.requests = []
        self.tool_requests = []
        self.index = 0

    def chat(self, messages, tools):
        self.requests.append(copy.deepcopy(messages))
        self.tool_requests.append(copy.deepcopy(tools))
        calls = self.rounds[self.index] if self.index < len(self.rounds) else []
        self.index += 1
        return Message(
            "assistant",
            tool_calls=[
                ToolCall(
                    str(call.get("id", f"script-{self.index}-{offset}")),
                    call["tool"],
                    {"conflict_id": call.get("conflict_id"), **call.get("args", {})},
                )
                for offset, call in enumerate(calls)
            ],
        )


def _payload_entry(focus, compile_result):
    shaped = build_round_payload(compile_result, 1, 6)
    wanted_steps = set(focus.conflict.get("steps", []))
    entry = next((
        value for value in shaped["conflicts"]
        if value.get("kind") == focus.conflict.get("kind")
        and {party.get("step") for party in value.get("parties", [])} == wanted_steps
    ), None)
    if entry is None:
        raise ValueError("focus conflict was not present in shaped payload")
    return shaped, {**entry, "id": focus.id, "fingerprint": focus.fingerprint}


def _action_key(session_id, fingerprint, tool, args):
    if tool == "insert_yield":
        return (
            tool, session_id, fingerprint,
            args.get("yielding_step"), args.get("winner_step"),
        )
    if tool == "replan_path":
        return (tool, session_id, fingerprint, args.get("mover"))
    if tool == "add_after":
        return (tool, args.get("step"), args.get("after_step"))
    if tool == "insert_go_to":
        return (tool, session_id, args.get("robot"))
    if tool == "handoff_terminal_close":
        return (tool, session_id, args.get("close_step"), args.get("to_robot"))
    return (tool,)


def _terminal_candidates(entry, policy, ledger):
    if not policy.get("terminal"):
        entry["handoff_candidates"] = []
        entry["allowed_tools"] = [
            tool for tool in entry.get("allowed_tools", [])
            if tool not in ("insert_go_to", "handoff_terminal_close")
        ]
        return
    if not policy.get("eligible"):
        entry["allowed_tools"] = []
        entry["handoff_candidates"] = []
        entry["terminal_policy"] = policy
        return

    earlier = policy["earlier_robot"]
    later = policy["later_robot"]
    locked = set(policy.get("locked_robots", []))
    if ledger.departure_robot is None:
        # robot_locked pins a task's OWNER; insert_go_to appends a
        # retire-to-rest leg to the same robot and reassigns nothing, so a
        # locked placement must not veto the departure (a user-swapped
        # shared-container plan would otherwise have no legal repair at all).
        qualifies = any(
            party.get("robot") == earlier
            and party.get("motion") == "dwelling"
            and party.get("is_last_step")
            for party in entry.get("parties", [])
        )
        entry["allowed_tools"] = ["insert_go_to"] if qualifies else []
        entry["insert_go_to_candidates"] = (
            [{"robot": earlier}] if entry["allowed_tools"] else [])
        entry["handoff_candidates"] = []
    else:
        handoffs = [
            candidate for candidate in entry.get("handoff_candidates", [])
            if candidate.get("to_robot") == later
            and candidate.get("from_robot") == ledger.departure_robot
            and later not in locked
        ]
        if handoffs and ledger.handoff_direction is None:
            entry["allowed_tools"] = ["handoff_terminal_close"]
            entry["handoff_candidates"] = handoffs
        else:
            entry["allowed_tools"] = [
                tool for tool in entry.get("allowed_tools", [])
                if tool not in ("insert_go_to", "handoff_terminal_close")
            ]
            entry["handoff_candidates"] = []
    entry["terminal_policy"] = policy


def _has_cycle(edges):
    visiting = set()
    complete = set()

    def visit(node):
        if node in visiting:
            return True
        if node in complete:
            return False
        visiting.add(node)
        if any(visit(dependency) for dependency in edges.get(node, ())):
            return True
        visiting.remove(node)
        complete.add(node)
        return False

    return any(visit(node) for node in edges if node not in complete)


def _schedule_facility(item, schedule):
    if item.get("facility"):
        return item["facility"]
    group = item.get("group") or item.get("id")
    facilities = {
        candidate.get("facility") for candidate in schedule
        if (candidate.get("group") or candidate.get("id")) == group
        and candidate.get("facility")
    }
    return next(iter(facilities)) if len(facilities) == 1 else None


def _departure_prerequisite_candidates(state, focus, entry, shaped, ledger):
    """Create a future departure anchor when a facility visit has none.

    This is narrow structural lookahead, not speculative terminal ownership:
    the candidate is exposed only when it makes an otherwise impossible exact
    add_after edge topology-safe.
    """
    entry["insert_go_to_candidates"] = entry.get("insert_go_to_candidates", [])
    if (focus.conflict.get("kind") != "facility"
            or entry.get("add_after_candidates")
            or ledger.departure_robot is not None):
        return

    schedule = state.compile_result.get("schedule", [])
    by_id = {item.get("id"): item for item in schedule}
    facility = (focus.conflict.get("detail") or {}).get("facility")
    edges = {
        step_id: set(dependencies)
        for step_id, dependencies in shaped.get("dep_graph", {}).items()
    }
    for steps in state.plan.values():
        for previous, current in zip(steps, steps[1:]):
            edges.setdefault(current["id"], set()).add(previous["id"])

    candidates = []
    for occupant in entry.get("parties", []):
        robot = occupant.get("robot")
        if occupant.get("departure_step") is not None or robot not in state.plan:
            continue
        robot_steps = state.plan[robot]
        if not robot_steps:
            continue
        final_id = robot_steps[-1]["id"]
        final_item = by_id.get(final_id)
        if final_item is None or _schedule_facility(final_item, schedule) != facility:
            continue
        departure_id = f"{robot}#go_to_rest"
        if departure_id in by_id:
            continue
        # A robot_locked item at this facility does NOT disqualify the robot:
        # the lock pins task ownership, and a departure anchor moves no task.
        waiter = next((
            party for party in entry.get("parties", [])
            if party.get("robot") != robot
        ), None)
        if waiter is None:
            continue
        trial = {step_id: set(values) for step_id, values in edges.items()}
        trial.setdefault(departure_id, set()).add(final_id)
        trial.setdefault(waiter["step"], set()).add(departure_id)
        if _has_cycle(trial):
            continue
        completion = (
            float(final_item.get("start", 0.0))
            + float(final_item.get("duration", 0.0))
        )
        candidates.append({
            "robot": robot,
            "waiter_step": waiter["step"],
            "after_step": departure_id,
            "final_step": final_id,
            "causal_floor": completion,
            "reason": "departure_prerequisite",
        })

    if candidates:
        # Preserve the terminal policy's one-departure commitment: offer only
        # the earliest completing safe occupant, with robot id as stable tie-break.
        selected = min(candidates, key=lambda value: (
            value["causal_floor"], value["robot"]))
        entry["insert_go_to_candidates"] = [selected]
        if "insert_go_to" not in entry.get("allowed_tools", []):
            entry.setdefault("allowed_tools", []).append("insert_go_to")


def _step_group(step_id, schedule_by_id):
    item = schedule_by_id.get(step_id)
    return item.get("group") if item else None


def _apply_protected_pins(entry: dict, protected: dict, schedule: list[dict]) -> dict | None:
    """Non-negotiable pin-veto matrix (integration spec §4 item 12).

    A pin may veto a candidate ONLY when that candidate would actually
    CHANGE the pinned attribute -- never "this neighborhood is pinned,
    therefore nothing here may move". That blanket-veto mistake is exactly
    what the `robot_locked` comment above (`_terminal_candidates`) is
    documenting: it made a user-swapped shared-container plan have NO legal
    repair at all, because a lock on task ownership also (wrongly) vetoed
    `insert_go_to`, which reassigns no task and reorders no task. The matrix
    here is the general form of that fix, one entry per tool:

      insert_go_to            -> vetoed by NOTHING. It appends a retire-leg
                                  to the SAME robot; no task is reassigned,
                                  no task is reordered, so no pin applies.
      add_after                -> vetoed only by an ordering pin whose
                                  intended order the new edge would invert.
      replan_path               -> vetoed only by a waypoint pin on the
                                  mover step (the resolver would otherwise
                                  overwrite the user's authored route).
      handoff_terminal_close    -> vetoed only by an allocation pin on that
                                  close's task group (robot_locked's finer,
                                  user-driven sibling).

    Mutates ``entry`` in place (pruning candidate lists the same way the
    existing untried/topology filters above do) and returns a
    ``{"pin": ..., "conflicting_task": ...}`` dict when pins removed EVERY
    tool that would otherwise have been legal (item 13's "keep the last
    verified plan, defer honestly" case); ``None`` otherwise (including when
    there was nothing legal to begin with -- that is an ordinary
    no-candidate case, not a pin deferral).
    """
    if not protected:
        return None
    allocations = protected.get("allocations") or []
    orderings = protected.get("orderings") or []
    exact_edges = exact_after_edges(protected)
    locked_after_steps = {step for step, _after in exact_after_fields(protected)}
    waypoints = {str(item.get("step")) for item in (protected.get("waypoints") or [])}
    schedule_by_id = {item.get("id"): item for item in schedule}

    had_legal = bool(entry.get("allowed_tools"))
    deferred = None

    if "handoff_terminal_close" in entry.get("allowed_tools", []):
        kept = []
        for candidate in entry.get("handoff_candidates", []):
            group = candidate.get("close_group")
            pin = next((a for a in allocations if a.get("group") == group), None)
            if pin is not None and pin.get("robot") != candidate.get("to_robot"):
                deferred = deferred or {
                    "pin": f"allocation of {group} to {pin.get('robot')}",
                    "conflicting_task": group,
                }
                continue
            kept.append(candidate)
        entry["handoff_candidates"] = kept
        if not kept:
            entry["allowed_tools"] = [
                tool for tool in entry["allowed_tools"] if tool != "handoff_terminal_close"
            ]

    if "add_after" in entry.get("allowed_tools", []):
        kept = []
        for candidate in entry.get("add_after_candidates", []):
            step_group = _step_group(candidate.get("step"), schedule_by_id)
            # A dwelling conflict is repaired by delaying its entry
            # navigation.  Ordering pins still describe the conflicted task,
            # rather than the implementation-level entry step, so use the
            # source conflict step for the ordering comparison when present.
            ordering_step = candidate.get("conflict_step") or candidate.get("step")
            ordering_step_group = _step_group(ordering_step, schedule_by_id)
            after_group = _step_group(candidate.get("after_step"), schedule_by_id)
            if candidate.get("step") in locked_after_steps:
                deferred = deferred or {
                    "pin": f"exact after field of {candidate.get('step')}",
                    "conflicting_task": step_group or candidate.get("step"),
                }
                continue
            # The candidate makes `after_group` finish before `step_group`
            # starts. That inverts an ordering pin {before, after} exactly
            # when the pin already commits to that same pair in the OPPOSITE
            # direction: before == step_group, after == after_group.
            inverted = next((
                pin for pin in orderings
                if (pin.get("before") == ordering_step_group
                    and pin.get("after") == after_group)
            ), None)
            if inverted is not None:
                deferred = deferred or {
                    "pin": f"order {inverted.get('before')} before {inverted.get('after')}",
                    "conflicting_task": ordering_step_group,
                }
                continue
            # A reverse exact edge is a direct attempt to undo a manual
            # `set_step_after`. It would become a cycle anyway, but reject it
            # before spending a candidate/compile attempt.
            exact_inverted = next((
                edge for edge in exact_edges
                if edge == (candidate.get("after_step"), candidate.get("step"))
            ), None)
            if exact_inverted is not None:
                deferred = deferred or {
                    "pin": f"exact after {exact_inverted[0]} after {exact_inverted[1]}",
                    "conflicting_task": step_group,
                }
                continue
            kept.append(candidate)
        entry["add_after_candidates"] = kept
        if not kept:
            entry["allowed_tools"] = [
                tool for tool in entry["allowed_tools"] if tool != "add_after"
            ]

    if "replan_path" in entry.get("allowed_tools", []):
        candidates = entry.get("replan_path_candidates", [])
        kept = [c for c in candidates if str(c.get("mover")) not in waypoints]
        removed = [c for c in candidates if c not in kept]
        entry["replan_path_candidates"] = kept
        if removed and not kept:
            mover = removed[0].get("mover")
            deferred = deferred or {
                "pin": f"waypoint on {mover}",
                "conflicting_task": _step_group(mover, schedule_by_id) or mover,
            }
            entry["allowed_tools"] = [
                tool for tool in entry["allowed_tools"] if tool != "replan_path"
            ]

    if "insert_yield" in entry.get("allowed_tools", []):
        kept = []
        for candidate in entry.get("yield_candidates", []):
            yielding_step = str(candidate.get("yielding_step"))
            winner_step = str(candidate.get("winner_step"))
            yielding_group = _step_group(yielding_step, schedule_by_id) or yielding_step
            winner_group = _step_group(winner_step, schedule_by_id) or winner_step
            if yielding_step in waypoints or winner_step in waypoints:
                pinned = yielding_step if yielding_step in waypoints else winner_step
                deferred = deferred or {
                    "pin": f"waypoint on {pinned}",
                    "conflicting_task": _step_group(
                        pinned, schedule_by_id) or pinned,
                }
                continue
            if yielding_step in locked_after_steps or winner_step in locked_after_steps:
                pinned = (
                    yielding_step if yielding_step in locked_after_steps
                    else winner_step
                )
                deferred = deferred or {
                    "pin": f"exact after field of {pinned}",
                    "conflicting_task": _step_group(
                        pinned, schedule_by_id) or pinned,
                }
                continue
            inverted = next((
                pin for pin in orderings
                if pin.get("before") == yielding_group
                and pin.get("after") == winner_group
            ), None)
            if inverted is not None:
                deferred = deferred or {
                    "pin": (
                        f"order {inverted.get('before')} before "
                        f"{inverted.get('after')}"
                    ),
                    "conflicting_task": yielding_group,
                }
                continue
            kept.append(candidate)
        entry["yield_candidates"] = kept
        if not kept:
            entry["allowed_tools"] = [
                tool for tool in entry["allowed_tools"] if tool != "insert_yield"
            ]

    if had_legal and not entry.get("allowed_tools"):
        return deferred
    return None


def build_focus_payload(state: ResolutionState, focus) -> dict:
    shaped, entry = _payload_entry(focus, state.compile_result)
    ledger = state.sessions[focus.session_id]
    policy = terminal_context(focus, state.compile_result, ledger)
    _terminal_candidates(entry, policy, ledger)
    _departure_prerequisite_candidates(
        state, focus, entry, shaped, ledger)

    entry["yield_candidates"] = []
    try:
        deadlock = analyze_spatial_deadlock(
            state.plan, focus.conflict, state.compile_result)
    except ToolError as exc:
        deadlock = {
            "detected": False,
            "can_go_first": {},
            "yield_candidates": [],
            "analysis_error": str(exc),
        }
    entry["spatial_deadlock"] = bool(deadlock.get("detected"))
    entry["deadlock_can_go_first"] = deadlock.get("can_go_first", {})
    if deadlock.get("analysis_error"):
        entry["deadlock_analysis_error"] = deadlock["analysis_error"]
    if entry["spatial_deadlock"]:
        entry["yield_candidates"] = [
            {
                "yielding_step": candidate["yielding_step"],
                "winner_step": candidate["winner_step"],
                "yielding_robot": candidate["yielding_robot"],
                "winner_robot": candidate["winner_robot"],
                "estimated_distance": round(
                    float(candidate["estimated_distance"]), 3),
            }
            for candidate in deadlock.get("yield_candidates", [])
        ]
        # A proven priority deadlock makes one-sided rerouting and pure
        # serialization dominated strategies.  Do not spend LLM/compile
        # attempts on tools that cannot release either occupied endpoint.
        entry["allowed_tools"] = (
            ["insert_yield"] if entry["yield_candidates"] else []
        )
        if not entry["yield_candidates"]:
            entry["terminal_blocked_reason"] = (
                "spatial_deadlock_no_yield_point")

    pruning = []
    if entry["spatial_deadlock"]:
        pruning.extend([
            "replan_path:dominated_by_spatial_deadlock",
            "add_after:both_priority_orders_infeasible",
        ])
    if ("replan_path" in focus.conflict.get("allowed_tools", [])
            and "replan_path" not in entry.get("allowed_tools", [])):
        pruning.append("replan_path:facility_or_terminal_policy")
    if not entry.get("add_after_candidates"):
        pruning.append("add_after:no_topology_safe_candidate")
    if (focus.conflict.get("kind") == "facility"
            and not entry.get("add_after_candidates")
            and not entry.get("insert_go_to_candidates")):
        pruning.append("insert_go_to:no_safe_departure_prerequisite")

    legal = []
    for tool in entry.get("allowed_tools", []):
        if tool == "insert_yield":
            candidates = [
                candidate for candidate in entry.get("yield_candidates", [])
                if _action_key(
                    focus.session_id, focus.fingerprint, tool, candidate)
                not in ledger.attempted_action_keys
            ]
            # Geometry already sorts by estimated total travel and robot id.
            # Expose one deterministic choice at a time so the LLM cannot
            # override the priority decision; a rejected choice reveals the
            # next untried candidate on the following round.
            entry["yield_candidates"] = candidates[:1]
            if candidates:
                legal.append(tool)
            else:
                pruning.append("insert_yield:all_action_keys_exhausted")
        elif tool == "replan_path":
            candidates = [
                {"mover": party.get("step"), "avoid": next(
                    (other.get("robot") for other in entry.get("parties", [])
                     if other.get("robot") != party.get("robot")), None)}
                for party in entry.get("parties", [])
                if party.get("motion") == "moving"
            ]
            candidates = [candidate for candidate in candidates if
                _action_key(focus.session_id, focus.fingerprint, tool, candidate)
                not in ledger.attempted_action_keys
            ]
            entry["replan_path_candidates"] = candidates
            if candidates:
                legal.append(tool)
            else:
                pruning.append("replan_path:all_action_keys_exhausted")
        elif tool == "add_after":
            candidates = [
                candidate for candidate in entry.get("add_after_candidates", [])
                if _action_key(
                    focus.session_id, focus.fingerprint, tool, candidate)
                not in ledger.attempted_action_keys
            ]
            aggressive = [
                candidate for candidate in candidates
                if candidate.get("strategy") == "departure_start"
            ]
            if aggressive:
                candidates = aggressive
            entry["add_after_candidates"] = candidates
            if candidates:
                legal.append(tool)
            else:
                pruning.append("add_after:no_untried_candidate")
        elif tool == "insert_go_to":
            candidates = [
                candidate for candidate in entry.get("insert_go_to_candidates", [])
                if _action_key(
                    focus.session_id, focus.fingerprint, tool, candidate)
                not in ledger.attempted_action_keys
            ]
            entry["insert_go_to_candidates"] = candidates
            if candidates:
                legal.append(tool)
            else:
                pruning.append("insert_go_to:no_untried_candidate")
        elif tool == "handoff_terminal_close":
            candidates = [
                candidate for candidate in entry.get("handoff_candidates", [])
                if _action_key(
                    focus.session_id, focus.fingerprint, tool, candidate)
                not in ledger.attempted_action_keys
            ]
            entry["handoff_candidates"] = candidates
            if candidates:
                legal.append(tool)
            else:
                pruning.append("handoff_terminal_close:no_untried_candidate")
        else:
            legal.append(tool)
    entry["allowed_tools"] = legal
    if (entry.get("spatial_deadlock")
            and not entry.get("allowed_tools")
            and not entry.get("terminal_blocked_reason")):
        entry["terminal_blocked_reason"] = (
            "spatial_deadlock_yield_candidates_exhausted")
    entry["candidate_pruning"] = pruning
    pin_deferred = _apply_protected_pins(entry, state.protected, shaped["schedule"])
    if pin_deferred:
        entry["pin_deferred"] = pin_deferred
    return {
        "round": len(state.rounds) + 1,
        "global_attempts": state.attempts_used,
        "global_attempt_cap": GLOBAL_ATTEMPT_CAP,
        "session_count": len(state.sessions),
        "session_cap": state.session_cap,
        "makespan": makespan(state.compile_result),
        "focus": entry,
        "global_conflicts": [
            {
                "fingerprint": item.fingerprint,
                "session_id": item.session_id,
                "kind": item.conflict.get("kind"),
                "class": item.conflict.get("class"),
                "steps": item.conflict.get("steps"),
                "window": item.conflict.get("window"),
            }
            for item in state.queue
        ],
        "session": {
            "session_id": ledger.session_id,
            "attempts_used": ledger.attempts_used,
            "failed_action_keys": [list(key) for key in ledger.failed_action_keys],
            "departure_robot": ledger.departure_robot,
            "close_owner": ledger.close_owner,
            "handoff_direction": ledger.handoff_direction,
        },
        "sessions": {
            key: {
                "attempts_used": value.attempts_used,
                "status": value.status,
                "departure_robot": value.departure_robot,
                "close_owner": value.close_owner,
            }
            for key, value in state.sessions.items()
        },
        "schedule": shaped["schedule"],
        "dep_graph": shaped["dep_graph"],
        "decision_feedback": (
            "The previous response returned no tool call despite legal "
            "candidates; select one exact candidate."
            if state.decision_failures.get(focus.fingerprint, 0) else None
        ),
    }


def _normalize_call(call):
    arguments = dict(call.arguments or {})
    return {
        "call_id": call.id,
        "tool": call.name,
        "conflict_id": arguments.pop("conflict_id", None),
        "args": arguments,
    }


def _focus_diagnostics(payload, provider_tool_call_count):
    focus = payload["focus"]
    return {
        "focus_kind": focus.get("kind"),
        "focus_class": focus.get("class"),
        "allowed_tools": list(focus.get("allowed_tools", [])),
        "spatial_deadlock": bool(focus.get("spatial_deadlock")),
        "deadlock_can_go_first": copy.deepcopy(
            focus.get("deadlock_can_go_first", {})),
        "yield_candidates": copy.deepcopy(
            focus.get("yield_candidates", [])),
        "replan_path_candidates": copy.deepcopy(
            focus.get("replan_path_candidates", [])),
        "add_after_candidates": copy.deepcopy(
            focus.get("add_after_candidates", [])),
        "insert_go_to_candidates": copy.deepcopy(
            focus.get("insert_go_to_candidates", [])),
        "handoff_candidates": copy.deepcopy(
            focus.get("handoff_candidates", [])),
        "candidate_pruning": list(focus.get("candidate_pruning", [])),
        "provider_tool_call_count": provider_tool_call_count,
    }


def _legal_candidate_count(payload):
    focus = payload["focus"]
    candidate_key = {
        "insert_yield": "yield_candidates",
        "replan_path": "replan_path_candidates",
        "add_after": "add_after_candidates",
        "insert_go_to": "insert_go_to_candidates",
        "handoff_terminal_close": "handoff_candidates",
    }
    return sum(
        len(focus.get(candidate_key[tool], []))
        for tool in focus.get("allowed_tools", [])
        if tool in candidate_key
    )


def _validate_call(record, focus, payload, ledger):
    tool, args = record["tool"], record["args"]
    entry = payload["focus"]
    if record["conflict_id"] != focus.id:
        return "non_focus_conflict"
    if tool not in entry.get("allowed_tools", []):
        return "tool_not_legal_for_focus"
    key = _action_key(focus.session_id, focus.fingerprint, tool, args)
    if key in ledger.attempted_action_keys:
        return "action_already_attempted"
    if tool == "insert_yield" and not any(
        candidate.get("yielding_step") == args.get("yielding_step")
        and candidate.get("winner_step") == args.get("winner_step")
        for candidate in entry.get("yield_candidates", [])
    ):
        return "yield_candidate_mismatch"
    if tool == "add_after" and not any(
        candidate.get("step") == args.get("step")
        and candidate.get("after_step") == args.get("after_step")
        for candidate in entry.get("add_after_candidates", [])
    ):
        return "add_after_candidate_mismatch"
    if tool == "replan_path":
        if not any(
            party.get("step") == args.get("mover")
            and party.get("motion") == "moving"
            for party in entry.get("parties", [])
        ):
            return "reroute_mover_mismatch"
        if not any(
            party.get("robot") == args.get("avoid")
            and party.get("step") != args.get("mover")
            for party in entry.get("parties", [])
        ):
            return "reroute_avoid_mismatch"
    if tool == "insert_go_to" and not any(
        candidate.get("robot") == args.get("robot")
        for candidate in entry.get("insert_go_to_candidates", [])
    ):
        return "departure_candidate_mismatch"
    if tool == "handoff_terminal_close" and not any(
        candidate.get("close_step") == args.get("close_step")
        and candidate.get("to_robot") == args.get("to_robot")
        for candidate in entry.get("handoff_candidates", [])
    ):
        return "handoff_candidate_mismatch"
    return None


def _causal_floor(tool, args, focus, compile_result, payload=None):
    by_id = {item.get("id"): item for item in compile_result.get("schedule", [])}
    if tool == "insert_yield":
        starts = [
            float(by_id.get(step_id, {}).get("start", focus.start))
            for step_id in (args.get("yielding_step"), args.get("winner_step"))
        ]
        return min(starts, default=focus.start)
    if tool == "replan_path":
        item = by_id.get(args.get("mover"), {})
        return float(item.get("start", focus.start))
    if tool == "add_after":
        item = by_id.get(args.get("step"), {})
        return float(item.get("start", focus.start))
    if tool == "handoff_terminal_close":
        close = by_id.get(args.get("close_step"), {})
        group = close.get("group") or args.get("close_step")
        starts = [
            float(item.get("start", focus.start))
            for item in compile_result.get("schedule", [])
            if (item.get("group") or item.get("id")) == group
        ]
        return min(starts, default=focus.start)
    if tool == "insert_go_to":
        advertised = next((
            candidate for candidate in (payload or {}).get(
                "focus", {}).get("insert_go_to_candidates", [])
            if candidate.get("robot") == args.get("robot")
        ), None)
        if advertised and advertised.get("causal_floor") is not None:
            return float(advertised["causal_floor"])
        starts = [
            (
                float(by_id.get(party.get("step"), {}).get("start", focus.start))
                + float(by_id.get(party.get("step"), {}).get("duration", 0.0))
            )
            for party in focus.conflict.get("parties", [])
            if party.get("robot") == args.get("robot")
            and party.get("motion") == "dwelling"
            and party.get("is_last_step")
        ]
        return min(starts, default=focus.start)
    return focus.start


def _apply(candidate, tool, args, focus, compile_result, payload):
    dep_graph = {
        key: list(value) for key, value in payload.get("dep_graph", {}).items()
    }
    if tool == "insert_yield":
        apply_insert_yield(
            candidate,
            args["yielding_step"],
            args["winner_step"],
            focus.conflict,
            compile_result,
        )
    elif tool == "add_after":
        apply_add_after(candidate, dep_graph, args["step"], args["after_step"])
    elif tool == "replan_path":
        apply_replan_path(
            candidate, args["mover"], args["avoid"], focus.conflict, compile_result)
    elif tool == "insert_go_to":
        apply_insert_go_to(
            candidate, args["robot"], compile_result.get("rest_points", {}).get(args["robot"]))
    elif tool == "handoff_terminal_close":
        apply_handoff_terminal_close(candidate, args["close_step"], args["to_robot"])
    else:
        raise ToolError(f"unknown tool {tool!r}")


def _verify_candidate(state, focus, tool, args, candidate, post, causal_floor,
                      frozen_ids, frozen_before):
    post_plan = completed_plan_or_candidate(post, candidate)
    missing_pins = missing_exact_after_edges(post_plan, state.protected)
    if missing_pins:
        rendered = ", ".join(f"{step} after {after}" for step, after in missing_pins)
        return False, f"manual_exact_after_pin_regressed: {rendered}", post_plan, []
    changed_fields = mismatched_exact_after_fields(post_plan, state.protected)
    if changed_fields:
        rendered = ", ".join(step for step, _expected, _actual in changed_fields)
        return False, f"manual_exact_after_field_regressed: {rendered}", post_plan, []
    frozen_preserved = step_structure(post_plan, frozen_ids) == frozen_before
    pre_fps = {
        stable_fingerprint(c) for c in state.compile_result.get("conflicts", [])
    }
    new_conflicts = [
        c for c in post.get("conflicts", []) if stable_fingerprint(c) not in pre_fps
    ]
    if not frozen_preserved:
        return False, "frozen_prefix_regressed", post_plan, new_conflicts
    if any(c.get("kind") in ("object", "placement") for c in new_conflicts):
        return False, "new_human_conflict", post_plan, new_conflicts
    pre_by_fp = {
        stable_fingerprint(c): c for c in state.compile_result.get("conflicts", [])
    }
    if any(
        c.get("window")
        and float(c["window"][0]) < causal_floor - 1e-9
        and (
            stable_fingerprint(c) not in pre_by_fp
            or not pre_by_fp[stable_fingerprint(c)].get("window")
            or float(pre_by_fp[stable_fingerprint(c)]["window"][0])
            >= causal_floor - 1e-9
        )
        for c in post.get("conflicts", [])
    ):
        return False, "new_conflict_before_causal_floor", post_plan, new_conflicts

    # Protect already-accepted earlier decisions in unrelated sessions.
    for old in state.compile_result.get("conflicts", []):
        if stable_fingerprint(old) == focus.fingerprint:
            continue
        window = old.get("window")
        if not window or float(window[0]) >= focus.start:
            continue
        after = find_conflict(post, stable_fingerprint(old))
        if after is not None and conflict_metric(after) > conflict_metric(old):
            return False, "earlier_session_made_more_severe", post_plan, new_conflicts

    after_focus = find_conflict(post, focus.fingerprint)
    ordinary_improved = (
        after_focus is None
        or conflict_metric(after_focus) < conflict_metric(focus.conflict)
    )
    if tool in ("replan_path", "add_after", "insert_yield"):
        if tool == "insert_yield":
            yield_id = f"{args['yielding_step']}#yield"
            created = (
                yield_id not in step_index(state.plan)
                and yield_id in step_index(post_plan)
            )
            ordinary_improved = ordinary_improved and created
        return (
            ordinary_improved,
            "focus_improved" if ordinary_improved else "focus_not_improved",
            post_plan,
            new_conflicts,
        )
    if tool == "insert_go_to":
        departure_id = f"{args['robot']}#go_to_rest"
        created = departure_id not in step_index(state.plan) and departure_id in step_index(post_plan)
        return (
            created,
            "departure_anchor_created" if created else "departure_anchor_not_created",
            post_plan,
            new_conflicts,
        )
    if tool == "handoff_terminal_close":
        pre = step_index(state.plan)
        after = step_index(post_plan)
        close_id = args["close_step"]
        changed = (
            close_id in pre and close_id in after
            and pre[close_id][0] != after[close_id][0]
            and after[close_id][0] == args["to_robot"]
        )
        return changed, "close_owner_locked" if changed else "handoff_not_applied", post_plan, new_conflicts
    return False, "unknown_tool", post_plan, new_conflicts


def _round_record(state, focus, tool, args, *, accepted, reason,
                  causal_floor, before, after, new_conflicts):
    return {
        "round": len(state.rounds) + 1,
        "focus_time": focus.start,
        "focus_fingerprint": focus.fingerprint,
        "session_id": focus.session_id,
        "attempt": state.sessions[focus.session_id].attempts_used,
        "tool": tool,
        "args": args,
        "accepted": accepted,
        "compile_succeeded": after is not None,
        "focus_before": focus.conflict,
        "focus_after": find_conflict(after, focus.fingerprint) if after else focus.conflict,
        "new_conflicts": [stable_fingerprint(c) for c in new_conflicts],
        "new_earliest_conflict": None,
        "makespan_before": makespan(before),
        "makespan_after": makespan(after) if after else makespan(before),
        "causal_floor": causal_floor,
        "frozen_prefix_preserved": reason != "frozen_prefix_regressed",
        "reason": reason,
    }


def run_resolution_v2(plan, compile_fn, provider, *, initial_compile_result=None,
                      initial_snapshot_reused=False,
                      global_attempt_cap=GLOBAL_ATTEMPT_CAP,
                      session_cap=MAX_SESSIONS_PER_RUN,
                      return_compile_result=False,
                      on_event=None,
                      protected=None,
                      topology_check_fn=None,
                      robot_ids=DEFAULT_ROBOT_IDS):
    """Resolve from the last verified plan; every executed candidate compiles.

    ``on_event`` is an optional ``(kind: str, data: dict) -> None`` sink for
    PURE OBSERVATION of the run (phase3 spec). It is called at fixed points in
    this loop only (never inside helpers) with data already computed for the
    loop's own use, and it never influences a scheduling decision, budget
    count, compile, verification result, or the returned plan/report. A
    throwing sink is swallowed so it can never break resolution.
    """
    def emit(_event_kind, **data):
        # ``data`` may legitimately contain a "kind" key (e.g. the conflict's
        # own kind field for focus_started), so the event kind is passed
        # positionally/by a non-colliding name rather than as **data["kind"].
        if on_event is None:
            return
        try:
            on_event(_event_kind, data)
        except Exception:  # noqa: BLE001 - a bad sink must never break resolution
            pass

    if initial_compile_result is None:
        initial_compile_result = compile_fn(plan)
        initial_compile_count = 1
    else:
        initial_compile_count = 0
    accepted_plan = completed_plan_or_candidate(
        initial_compile_result, copy.deepcopy(plan))
    require_exact_after_edges(accepted_plan, protected)
    state = ResolutionState(
        plan=copy.deepcopy(accepted_plan),
        compile_result=initial_compile_result,
        initial_snapshot_reused=initial_snapshot_reused,
        compile_count=initial_compile_count,
        session_cap=session_cap,
        messages=[Message("system", SYSTEM_PROMPT_V2)],
        protected=protected or {},
    )
    state.rebuild_queue()
    emit("run_started", queued_conflicts=len(state.queue))
    tools = resolver_tools(robot_ids)

    llm_round_cap = max(global_attempt_cap * 3, 18)
    while state.queue and state.attempts_used < global_attempt_cap \
            and len(state.rounds) < llm_round_cap:
        focus = state.queue[0]
        emit("focus_started", **{
            "facility": (focus.conflict.get("detail") or {}).get("facility"),
            "robots": focus.conflict.get("robots"),
            "kind": focus.conflict.get("kind"),
            "class": focus.conflict.get("class"),
            "window": focus.conflict.get("window"),
        })
        ledger = state.sessions[focus.session_id]
        payload = build_focus_payload(state, focus)
        pin_deferred = payload["focus"].pop("pin_deferred", None)
        if pin_deferred is not None:
            # Item 13: a focus with allowed_tools == [] SOLELY because of a
            # pin never reaches the LLM at all -- there is no legal call to
            # make, and the reason must be attributable to the pin, not
            # blended into "no_remaining_legal_strategy" (that would make it
            # look genuinely unfixable rather than a decision handed back to
            # the user). block immediately (no 3-attempt grace period like
            # the generic no-candidate path): a pin will not stop applying on
            # retry, so retrying only burns budget.
            state.deferred_pins.append(pin_deferred)
            state.blocked_fingerprints[focus.fingerprint] = "pin_blocked"
            state.rounds.append({
                "round": len(state.rounds) + 1,
                "focus_time": focus.start,
                "focus_fingerprint": focus.fingerprint,
                "session_id": focus.session_id,
                "tool": None,
                "args": {},
                "accepted": False,
                "compile_succeeded": False,
                "reason": "pin_blocked",
                "rejected_calls": [],
                **_focus_diagnostics(payload, 0),
            })
            emit("pin_deferred", **pin_deferred)
            emit(
                "round_completed",
                accepted=False,
                reason="pin_blocked",
                tool=None,
                facility=(focus.conflict.get("detail") or {}).get("facility"),
            )
            state.rebuild_queue()
            continue
        terminal_blocked_reason = payload["focus"].pop(
            "terminal_blocked_reason", None)
        if terminal_blocked_reason is not None:
            state.blocked_fingerprints[focus.fingerprint] = (
                terminal_blocked_reason)
            state.rounds.append({
                "round": len(state.rounds) + 1,
                "focus_time": focus.start,
                "focus_fingerprint": focus.fingerprint,
                "session_id": focus.session_id,
                "tool": None,
                "args": {},
                "accepted": False,
                "compile_succeeded": False,
                "reason": terminal_blocked_reason,
                "rejected_calls": [],
                **_focus_diagnostics(payload, 0),
            })
            emit(
                "round_completed",
                accepted=False,
                reason=terminal_blocked_reason,
                tool=None,
                facility=(focus.conflict.get("detail") or {}).get("facility"),
            )
            state.rebuild_queue()
            continue
        ledger.conflict_history.append(focus.fingerprint)
        state.messages.append(Message(
            "user",
            "Resolve only this chronological focus:\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ))
        reply = provider.chat(state.messages, tools)
        state.messages.append(reply)
        calls = [_normalize_call(call) for call in reply.tool_calls or []]

        selected = None
        results = []
        for record in calls:
            error = _validate_call(record, focus, payload, ledger)
            if error is None and selected is None:
                selected = record
            else:
                results.append({
                    **record,
                    "accepted": False,
                    "executed": False,
                    "reason": error or "one_tool_per_round",
                })
        if selected is None:
            legal_candidate_count = _legal_candidate_count(payload)
            state.decision_failures[focus.fingerprint] = (
                state.decision_failures.get(focus.fingerprint, 0) + 1
            )
            failures = state.decision_failures[focus.fingerprint]
            if not calls and legal_candidate_count == 0:
                reason = "no_legal_candidate"
                state.blocked_fingerprints[focus.fingerprint] = reason
            elif not calls:
                reason = (
                    "model_declined_repeatedly" if failures >= 3
                    else "model_declined_tool"
                )
                if failures >= 3:
                    state.blocked_fingerprints[focus.fingerprint] = reason
            else:
                reason = (
                    "repeated_invalid_tool_call" if failures >= 3
                    else "no_valid_focus_tool"
                )
                if failures >= 3:
                    state.blocked_fingerprints[focus.fingerprint] = reason
            state.rounds.append({
                "round": len(state.rounds) + 1,
                "focus_time": focus.start,
                "focus_fingerprint": focus.fingerprint,
                "session_id": focus.session_id,
                "tool": None,
                "args": {},
                "accepted": False,
                "compile_succeeded": False,
                "reason": reason,
                "rejected_calls": results,
                **_focus_diagnostics(payload, len(calls)),
            })
            emit(
                "round_completed",
                accepted=False,
                reason=reason,
                tool=None,
                facility=(focus.conflict.get("detail") or {}).get("facility"),
            )
            for record in results:
                state.messages.append(Message(
                    "tool", json.dumps(record), tool_call_id=record["call_id"]))
            state.rebuild_queue()
            continue

        # Every returned call receives a result in provider order. The selected
        # call's result is filled after its one verification compile.
        tool, args = selected["tool"], selected["args"]
        emit(
            "strategy_selected",
            tool=tool,
            args=args,
            robots=focus.conflict.get("robots"),
            facility=(focus.conflict.get("detail") or {}).get("facility"),
        )
        action_key = _action_key(
            focus.session_id, focus.fingerprint, tool, args)
        ledger.attempted_action_keys.add(action_key)
        state.decision_failures.pop(focus.fingerprint, None)
        if tool == "replan_path":
            ledger.replan_keys.add(action_key)
        causal_floor = _causal_floor(
            tool, args, focus, state.compile_result, payload)
        target = (
            args.get("yielding_step") or args.get("mover")
            or args.get("step") or args.get("close_step")
        )
        frozen_ids = frozen_step_ids(state.compile_result, causal_floor)
        frozen_before = step_structure(state.plan, frozen_ids)
        candidate = copy.deepcopy(state.plan)
        before = state.compile_result
        post = None
        new_conflicts = []
        accepted = False
        try:
            if target in frozen_ids:
                raise ToolError(f"tool targets frozen step {target!r}")
            _apply(candidate, tool, args, focus, before, payload)
            require_exact_after_edges(candidate, state.protected)
            if topology_check_fn is not None:
                topology_check_fn(candidate)
            emit("verification_started", tool=tool)
            post = compile_fn(candidate)
            state.compile_count += 1
            # The attempt budget measures candidates that reached hard
            # compiler verification. Tool/application and compiler failures
            # remain recorded and cannot be retried, but consume no attempt.
            ledger.attempts_used += 1
            state.attempts_used += 1
            accepted, reason, post_plan, new_conflicts = _verify_candidate(
                state, focus, tool, args, candidate, post, causal_floor,
                frozen_ids, frozen_before)
        except Exception as exc:  # noqa: BLE001 - compiler failures must roll back
            prefix = (
                "topology_rejected"
                if getattr(exc, "code", None) is not None
                else "compiler_or_tool_failure"
            )
            reason = f"{prefix}: {exc}"
            post_plan = state.plan

        if accepted:
            accepted_pre_plan = state.plan
            for fingerprint in list(state.blocked_fingerprints):
                if fingerprint in ledger.conflict_history:
                    state.blocked_fingerprints.pop(fingerprint, None)
                    state.decision_failures.pop(fingerprint, None)
            state.plan = copy.deepcopy(post_plan)
            state.compile_result = post
            if tool == "insert_go_to":
                ledger.departure_robot = args["robot"]
            elif tool == "handoff_terminal_close":
                source = step_index(accepted_pre_plan).get(
                    args["close_step"], (ledger.departure_robot,))[0]
                ledger.handoff_direction = (source, args["to_robot"])
                ledger.close_owner = args["to_robot"]
            ledger.resolved_until = max(ledger.resolved_until, focus.start)
        else:
            ledger.failed_action_keys.add(action_key)

        record = _round_record(
            state, focus, tool, args, accepted=accepted, reason=reason,
            causal_floor=causal_floor, before=before, after=post,
            new_conflicts=new_conflicts)
        record["rejected_calls"] = results
        record.update(_focus_diagnostics(payload, len(calls)))
        ledger.tool_history.append(record)
        state.rounds.append(record)
        emit(
            "round_completed",
            accepted=accepted,
            reason=reason,
            tool=tool,
            facility=(focus.conflict.get("detail") or {}).get("facility"),
        )
        selected_result = {**record, "executed": True}
        result_by_id = {selected["call_id"]: selected_result}
        result_by_id.update({record["call_id"]: record for record in results})
        for call in calls:
            state.messages.append(Message(
                "tool",
                json.dumps(result_by_id[call["call_id"]], ensure_ascii=False),
                tool_call_id=call["call_id"],
            ))

        state.rebuild_queue()
        if state.queue:
            state.rounds[-1]["new_earliest_conflict"] = {
                "fingerprint": state.queue[0].fingerprint,
                "session_id": state.queue[0].session_id,
                "window": state.queue[0].conflict.get("window"),
            }

    if state.attempts_used >= global_attempt_cap:
        for ledger in state.sessions.values():
            if ledger.status == "active":
                ledger.residual_reason = "global_attempt_cap_reached"
    for ledger in state.sessions.values():
        if ledger.attempts_used >= SESSION_ATTEMPT_CAP and ledger.status == "active":
            ledger.status = "exhausted"
            ledger.residual_reason = "session_attempt_cap_reached"

    # End-of-run observation pass only (no hot-loop branching): report every
    # session that ends non-active and every conflict deferred by the session
    # cap during the final queue rebuild.
    for session_id, ledger in state.sessions.items():
        if ledger.status != "active":
            emit("session_deferred", session_id=session_id, reason=ledger.residual_reason)
    for deferred in state.deferred_conflicts.values():
        emit(
            "session_deferred",
            session_id=deferred.get("session_id"),
            reason=deferred.get("reason"),
        )

    report = _build_report(state, global_attempt_cap)
    # Covers a no-conflict run as well as every accepted candidate path.
    require_exact_after_edges(state.plan, state.protected)
    emit("run_completed", converged=report["converged"], rounds_used=report["rounds_used"])
    if return_compile_result:
        return state.plan, report, state.compile_result
    return state.plan, report


def _build_report(state, global_attempt_cap):
    active_fps = {
        stable_fingerprint(conflict): conflict
        for conflict in state.compile_result.get("conflicts", [])
        if conflict.get("kind") in ("path", "facility")
    }
    unresolved = []
    for fingerprint, conflict in active_fps.items():
        session_id = state.conflict_sessions.get(fingerprint)
        session = state.sessions.get(session_id)
        deferred = state.deferred_conflicts.get(fingerprint)
        unresolved.append({
            "fingerprint": fingerprint,
            "kind": conflict.get("kind"),
            "steps": conflict.get("steps"),
            "robots": conflict.get("robots"),
            "session_id": session_id,
            "reason": (
                deferred.get("reason") if deferred else
                state.blocked_fingerprints.get(fingerprint)
                or (
                    session.residual_reason
                    if session and session.residual_reason
                    else "global_attempt_cap_reached"
                    if state.attempts_used >= global_attempt_cap
                    else "no_remaining_legal_strategy"
                )
            ),
        })
    return {
        "version": "v2",
        "converged": not active_fps,
        "rounds_used": len(state.rounds),
        "round_cap": global_attempt_cap,
        "session_cap": state.session_cap,
        "session_attempt_cap": SESSION_ATTEMPT_CAP,
        "sessions_admitted": len(state.sessions),
        "executed_attempts": state.attempts_used,
        "compile_count": state.compile_count,
        "initial_snapshot_reused": state.initial_snapshot_reused,
        "rounds": state.rounds,
        "applied": [record for record in state.rounds if record.get("accepted")],
        "rejected": [
            record for record in state.rounds
            if record.get("tool") is not None and not record.get("accepted")
        ],
        "deferred": [
            call for record in state.rounds for call in record.get("rejected_calls", [])
        ] + list(state.deferred_conflicts.values()),
        "unresolved": unresolved,
        "deferred_pins": list(state.deferred_pins),
        "escalated_to_human": [
            {
                "kind": conflict.get("kind"),
                "steps": conflict.get("steps"),
                "robots": conflict.get("robots"),
                "message": conflict.get("message"),
            }
            for conflict in state.human_conflicts
        ],
        "sessions": {
            key: {
                "attempts_used": value.attempts_used,
                "departure_robot": value.departure_robot,
                "close_owner": value.close_owner,
                "handoff_direction": value.handoff_direction,
                "resolved_until": value.resolved_until,
                "status": value.status,
                "residual_reason": value.residual_reason,
            }
            for key, value in state.sessions.items()
        },
    }


def render_v2_report(report):
    lines = []
    by_session = {}
    for record in report.get("rounds", []):
        by_session.setdefault(record["session_id"], []).append(record)
    for session_id, records in by_session.items():
        lines.append(session_id)
        for record in records:
            tool = record.get("tool") or "no_tool"
            status = "accepted" if record.get("accepted") else "rejected"
            lines.append(
                f"- t={record.get('focus_time', 0):.3f} {tool} "
                f"{record.get('args', {})}: {status} ({record.get('reason')})"
            )
    deferred_sessions = sorted({
        item.get("session_id")
        for item in report.get("deferred", [])
        if item.get("reason") == "session_cap_reached"
        and item.get("session_id")
    })
    for session_id in deferred_sessions:
        lines.append(session_id)
        lines.append("- deferred (session_cap_reached)")
    if report.get("escalated_to_human"):
        lines.append(
            f"{len(report['escalated_to_human'])} object/placement conflict(s) need a human decision."
        )
    lines.append("Converged." if report.get("converged") else "Returned the last verified partial plan.")
    return "\n".join(lines)
