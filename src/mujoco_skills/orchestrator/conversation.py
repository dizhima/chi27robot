"""NDJSON event envelope + Method-B progress mapping for the Author/Resolver
turns.

Keeps HTTP plumbing (sockets, headers, flushing) entirely out of this module so
the event sequence is unit-testable without a live connection. `service.py`
supplies a `write_event` callable that stamps `seq`/`turn_id` and writes the
line to the wire; this module only decides *what* events to emit and *when*,
driven by `authoring.author`'s existing `on_event` callback (Author) and by
`resolver_v2.run_resolution_v2`'s `on_event` sink (Resolver, Phase 3).

Resolver events are pure observation: `stream_resolve_turn` maps them to
display text via `resolver_progress_text` and never alters
`resolve_v2_core`/`run_resolution_v2` behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from mujoco_skills.orchestrator import authoring, decompose, loop
from mujoco_skills.orchestrator.schema import Message, ToolSpec

WriteEvent = Callable[[dict], None]

# ---------------------------------------------------------------------------
# Part A -- intent classifier (phase4 spec section 3)
# ---------------------------------------------------------------------------

INTENTS = ("author", "resolve", "explain")

# D3 (compound_turn_integration_spec §2): once the tail auto-runs
# Compile -> Resolve after every author turn, "resolve" stops being a
# natural-language intent -- an explicit re-run is the sync button
# (`intent_hint: "sync"`). This variant is selected ONLY on the compound path;
# the legacy set above is what the rollback path (COMPOUND_TURN unset) keeps
# using, so turning the flag off restores the old routing exactly.
COMPOUND_INTENTS = ("author", "explain")


def _classify_tool(intents: tuple[str, ...]) -> ToolSpec:
    return ToolSpec(
        name="classify",
        description="Classify the user's latest message into exactly one workflow.",
        parameters={
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": list(intents)},
                "confidence": {"type": "number"},
                "reason_code": {"type": "string"},
            },
            "required": ["intent", "confidence", "reason_code"],
            "additionalProperties": False,
        },
    )


CLASSIFY_TOOL = _classify_tool(INTENTS)
COMPOUND_CLASSIFY_TOOL = _classify_tool(COMPOUND_INTENTS)

CLASSIFY_PROMPT = (
    "You are an intent classifier for a two-robot task-plan editor. Classify "
    "the user's latest message by their GOAL, not by surface wording, into "
    "exactly one of three workflows:\n"
    "- 'author': the user wants to CHANGE plan tasks -- add/remove/move an "
    "object, change a destination, or change a robot assignment.\n"
    "- 'resolve': the user wants the system to FIX scheduling conflicts, avoid "
    "a collision, or improve execution ordering on an already-compiled plan.\n"
    "- 'explain': the user wants INFORMATION or an explanation about the "
    "current plan, schedule, ordering, conflicts, status, or about HOW "
    "something would be done -- they are not asking the system to change "
    "anything now.\n"
    "Key rule: a polite request wrapper (can you / could you / would you / "
    "please / I'd like you to) does NOT by itself mean 'explain'. Look at the "
    "ACTION it wraps:\n"
    "- wrapping a change/fix verb (resolve, fix, avoid, move, add, remove, "
    "reassign, change) -> that action ('resolve' or 'author').\n"
    "- wrapping an information verb (explain, describe, tell, show, list) -> "
    "'explain'.\n"
    "A bare wh-question about state or method (what / why / which / when, or "
    "'how to' / 'how would you' resolve) is 'explain'. A bare imperative to "
    "change the plan is 'author' or 'resolve'. An explicit change/fix request "
    "wins over the mere presence of conflicts.\n"
    "Examples:\n"
    "- 'can you resolve the first one?' -> resolve\n"
    "- 'could you fix the collision' -> resolve\n"
    "- 'please move the mug to the sink' -> author\n"
    "- 'resolve the conflicts' -> resolve\n"
    "- 'how to resolve these conflicts' -> explain\n"
    "- 'how would you fix this?' -> explain\n"
    "- 'what are the current conflicts?' -> explain\n"
    "- 'why does robot1 wait?' -> explain\n"
    "- 'can you explain the conflicts?' -> explain\n"
    "Always finish by calling the classify tool with your answer."
)

COMPOUND_CLASSIFY_PROMPT = (
    "You are an intent classifier for a two-robot task-plan editor. Classify "
    "the user's latest message by their GOAL, not by surface wording, into "
    "exactly one of two workflows:\n"
    "- 'author': the user wants to CHANGE something about the plan -- "
    "add/remove/move an object, change a destination, change a robot "
    "assignment, fix a scheduling conflict, avoid a collision, or otherwise "
    "make the plan do something different. Compiling and resolving conflicts "
    "are not separate requests: the system runs them automatically after "
    "every plan change, so a request to fix/resolve/avoid a conflict is "
    "'author' too.\n"
    "- 'explain': the user wants INFORMATION or an explanation about the "
    "current plan, schedule, ordering, conflicts, status, or about HOW "
    "something would be done -- they are not asking the system to change "
    "anything now.\n"
    "Key rule: a polite request wrapper (can you / could you / would you / "
    "please / I'd like you to) does NOT by itself mean 'explain'. Look at the "
    "ACTION it wraps:\n"
    "- wrapping a change/fix verb (resolve, fix, avoid, move, add, remove, "
    "reassign, change) -> 'author'.\n"
    "- wrapping an information verb (explain, describe, tell, show, list) -> "
    "'explain'.\n"
    "A bare wh-question about state or method (what / why / which / when, or "
    "'how to' / 'how would you' resolve) is 'explain'. A bare imperative to "
    "change the plan is 'author'. An explicit change/fix request wins over "
    "the mere presence of conflicts.\n"
    "Examples:\n"
    "- 'can you resolve the first one?' -> author\n"
    "- 'could you fix the collision' -> author\n"
    "- 'please move the mug to the sink' -> author\n"
    "- 'resolve the conflicts' -> author\n"
    "- 'how to resolve these conflicts' -> explain\n"
    "- 'how would you fix this?' -> explain\n"
    "- 'what are the current conflicts?' -> explain\n"
    "- 'why does robot1 wait?' -> explain\n"
    "- 'can you explain the conflicts?' -> explain\n"
    "Always finish by calling the classify tool with your answer."
)


def classify_intent(
    latest_user_text: str,
    state_hint: dict,
    provider: Any,
    *,
    compound: bool = False,
) -> dict:
    """One forced structured call -> {intent, confidence, reason_code}.

    ``state_hint`` is a compact dict such as ``{"plan_exists": bool,
    "delegable_conflict_count": int}`` so the model can separate "resolve the
    conflicts" from "why is there a conflict". If the model's ``intent`` is
    not one of the active intent set, this falls back to ``author`` (the safe,
    non-destructive default -- it only stages a draft for review) with
    ``reason_code="classifier_fallback"``. ``reason_code`` is telemetry/test
    only and is never shown to the user.

    ``compound=True`` selects the two-intent variant (D3). It is passed only by
    the ``COMPOUND_TURN`` routing branch, so the classifier the rollback path
    sees is bit-identical to the pre-compound-turn one.
    """
    intents = COMPOUND_INTENTS if compound else INTENTS
    prompt = COMPOUND_CLASSIFY_PROMPT if compound else CLASSIFY_PROMPT
    tool = COMPOUND_CLASSIFY_TOOL if compound else CLASSIFY_TOOL
    messages = [
        Message("system", prompt),
        Message(
            "user",
            json.dumps(
                {"latest_user_text": latest_user_text, "state_hint": state_hint},
                ensure_ascii=False,
            ),
        ),
    ]
    reply = provider.force_tool(messages, [tool], "classify")
    for call in reply.tool_calls or []:
        if call.name != "classify":
            continue
        args = dict(call.arguments or {})
        if args.get("intent") in intents:
            return args
        break
    return {
        "intent": "author",
        "confidence": 0.0,
        "reason_code": "classifier_fallback",
    }

# Method-B mapping (phase0 §8.1 / phase2 §5): tool_call name -> fixed English
# progress text. Only these three author tools drive default progress.
AUTHOR_PROGRESS = {
    "augment": "Filling in shared-facility open/close/dependency steps.",
    "reassign": "Adjusting robot assignments.",
    "revise_order": "Applying the requested task order.",
    "propose_plan": "Plan structure fixed; generating execution steps.",
}


def _authoring_tool_detail_lines(name: str, result: object) -> list[str]:
    """Return a safe, user-facing audit of deterministic author mutations.

    The author loop already exposes exact tool results through ``on_event``;
    previously the compound path discarded them as low-level detail.  Keep
    only semantic action ids, operations, targets, and robot assignments so
    See details can prove what augment returned and what reassign actually
    changed without exposing model messages or chain-of-thought.
    """
    if not isinstance(result, dict):
        return []

    def action_text(action: object) -> str | None:
        if not isinstance(action, dict):
            return None
        action_id = action.get("id")
        op = action.get("op")
        robot = action.get("robot")
        if not all(isinstance(value, str) and value for value in (action_id, op, robot)):
            return None
        if op == "move":
            subject = action.get("object")
            destination = action.get("dest")
            description = f"move {subject} to {destination}"
        elif op in ("open", "close"):
            description = f"{op} {action.get('facility')}"
        elif op == "go_to":
            description = f"go to {action.get('target')}"
        else:
            description = op
        return f"{action_id} ({description}) -> {robot}"

    if name in ("augment", "reassign", "revise_order"):
        actions = result.get("actions")
        if not isinstance(actions, list):
            return []
        label = {
            "augment": "augment returned",
            "reassign": "reassign executed",
            "revise_order": "revise_order updated",
        }[name]
        return [
            f"{label}: {text}."
            for text in (action_text(action) for action in actions)
            if text is not None
        ]
    if name == "remove_task":
        removed = result.get("removed")
        ids = [
            action.get("id") for action in removed or []
            if isinstance(action, dict) and isinstance(action.get("id"), str)
        ]
        return [f"remove_task removed: {', '.join(ids)}."] if ids else []
    if name in ("update_move", "set_place_pin"):
        text = action_text(result.get("action"))
        return [f"{name} updated: {text}."] if text is not None else []
    if name == "propose_plan":
        return ["propose_plan committed the resulting authoring state."]
    return []


def _authoring_summary(lines: list[str]) -> str:
    if not lines:
        return ""
    return "\n".join([
        "Authoring operations:",
        *(f"{index}. {line}" for index, line in enumerate(lines, start=1)),
    ])


def _run_author_stage(
    body: dict,
    write_event: WriteEvent,
    *,
    provider: Any | None = None,
    manifest: dict | None = None,
    decomposition_text: str = "Draft ready for your review and compile.",
) -> dict | None:
    """The Author workflow through its ``decomposition`` progress event.

    Everything ``stream_author_turn`` used to do end to end, minus the final
    ``result``/``message_completed`` pair: validation, ``message_started``/
    ``intent_selected``, the authoring loop's own progress, and the
    decomposition line. This is a *stage*, not a turn -- both a plain author
    turn (``stream_author_turn``, below) and a compound turn
    (``stream_compound_turn``) embed it and decide for themselves how to
    close out the envelope -- including the decomposition line itself, which
    must promise what actually happens next: a review + Compile press on the
    legacy path, an immediate compile on the compound one.
    Never raises: any failure (validation or
    authoring) is converted into a single ``error`` event and this returns
    ``None``; otherwise returns
    ``{"actions", "plan", "message", "reason", "authoring_summary"}``.
    """
    turn_id = body.get("turn_id")
    messages = body.get("messages")
    current = body.get("current_actions", [])
    scene_refs = body.get("scene_refs") or []
    plan_refs = body.get("plan_refs") or []
    if not isinstance(turn_id, str) or not turn_id:
        write_event({"type": "error", "text": "turn_id must be a non-empty string"})
        return None
    if not isinstance(messages, list) or not isinstance(current, list):
        write_event({"type": "error", "text": "messages and current_actions must be arrays"})
        return None

    write_event({"type": "message_started"})
    write_event(
        {
            "type": "intent_selected",
            "intent": "author",
            "text": "Understanding your plan change.",
        }
    )

    try:
        manifest = manifest or _load_manifest()
        provider = provider or _default_provider()
        current_plan = authoring.parse_actions(current, manifest)
        authoring_detail_lines: list[str] = []

        def on_event(kind: str, payload: object) -> None:
            if kind == "tool_call":
                name = getattr(payload, "name", None)
                text = AUTHOR_PROGRESS.get(name)
                if text is not None:
                    write_event({"type": "progress", "stage": name, "text": text})
                return
            if kind == "tool_result":
                if (isinstance(payload, tuple) and len(payload) == 2
                        and isinstance(payload[0], str)):
                    authoring_detail_lines.extend(
                        _authoring_tool_detail_lines(payload[0], payload[1])
                    )
                return
            if kind == "max_iters":
                write_event(
                    {
                        "type": "warning",
                        "text": "Authoring hit its step limit before finishing cleanly.",
                    }
                )
                return
            # "assistant_text" and "tool_result" are model chain-of-thought /
            # low-level detail; they never become a default progress event.

        result = authoring.author(
            messages=messages,
            current_plan=current_plan,
            provider=provider,
            manifest=manifest,
            on_event=on_event,
            scene_refs=scene_refs,
            plan_refs=plan_refs,
        )
        actions = [asdict(action) for action in result.actions]
        plan = (
            decompose.decompose(result.actions, manifest, scene_refs)
            if result.actions
            else None
        )
        write_event(
            {
                "type": "progress",
                "stage": "decomposition",
                "text": decomposition_text,
            }
        )
        return {
            "actions": actions,
            "plan": plan,
            "message": result.message,
            "reason": result.reason,
            "authoring_summary": _authoring_summary(authoring_detail_lines),
        }
    except Exception as exc:  # noqa: BLE001 - convert any authoring failure to one error event
        write_event({"type": "error", "text": str(exc)})
        return None


def stream_author_turn(
    body: dict,
    write_event: WriteEvent,
    *,
    provider: Any | None = None,
    manifest: dict | None = None,
) -> None:
    """Run the Author workflow, emitting envelope events via ``write_event``.

    ``write_event`` receives a fully-formed event dict (without ``seq``/
    ``turn_id`` — the caller in ``service.py`` stamps those). This function
    never raises: any exception during authoring/decompose is converted into a
    single ``error`` event, and the function returns without emitting
    ``result``/``message_completed``. A thin wrapper around
    ``_run_author_stage`` so this keeps its exact historical signature and
    event sequence (rollback path for the compound turn behind
    ``COMPOUND_TURN``).
    """
    stage = _run_author_stage(body, write_event, provider=provider, manifest=manifest)
    if stage is None:
        return
    write_event(
        {
            "type": "result",
            "artifact": {
                "kind": "author_result",
                "turn_id": body.get("turn_id"),
                "actions": stage["actions"],
                "plan": stage["plan"],
                "message": stage["message"],
                "reason": stage["reason"],
            },
        }
    )
    write_event({"type": "message_completed", "text": stage["message"]})


def resolver_progress_text(kind: str, data: dict) -> str | None:
    """Method-B resolver -> English progress mapping (phase0 §8.2).

    Interpolates facility/robots when present. Never leaks step ids,
    fingerprints, raw tool JSON, or session keys into display text -- those
    stay in ``details`` only (this function never returns them). Returns
    ``None`` for event kinds with no default progress line (the caller decides
    what, if anything, to do with an unmapped kind).
    """
    if kind == "run_started":
        return "Checking conflicts in the compiled plan."
    if kind == "focus_started":
        facility = data.get("facility")
        robots = data.get("robots")
        if facility and robots:
            robots_text = " and ".join(str(r) for r in robots)
            return f"Working on the overlap near {facility} between {robots_text}."
        return "Working on the overlap between the robots."
    if kind == "strategy_selected":
        # Effect language, not mechanism language: name what the plan will do,
        # never the internal tool. Robot/facility names are fine in display
        # text (focus_started already interpolates them); step ids are not.
        tool = data.get("tool")
        args = data.get("args") or {}
        facility = data.get("facility")
        if tool == "insert_yield":
            yielding = args.get("yielding_step")
            robot = _step_owner(yielding, data.get("plan"))
            if robot:
                return f"Sending {robot} aside so the other robot can pass."
            return "Sending one robot aside so the other can pass."
        if tool == "insert_go_to" and args.get("robot"):
            where = f" to clear the {facility}" if facility else ""
            return f"Sending {args['robot']} to its parking spot{where}."
        if tool == "replan_path":
            return "Planning a detour so the robots keep clear of each other."
        if tool == "handoff_terminal_close" and args.get("to_robot"):
            return f"Reassigning the final close to {args['to_robot']}."
        return "Trying to adjust execution order so the occupant leaves earlier."
    if kind == "verification_started":
        return "Change generated; running a full compile to verify."
    if kind == "round_completed":
        if data.get("accepted"):
            return "That adjustment worked; checking remaining conflicts."
        return "That adjustment did not help; trying another option."
    if kind == "session_deferred":
        return "This conflict group hit its attempt limit; kept for the sync button to pick up."
    if kind == "pin_deferred":
        # Item 13: never silently unpin. Named here exactly as it will be
        # named again in resolve_summary_text's deferred_pins clause, so the
        # live activity line and the final chat message agree.
        return (
            f"Your pinned {data.get('pin')} conflicts with "
            f"{data.get('conflicting_task')}; kept your setting."
        )
    if kind == "run_completed":
        return "Final verification done; producing the summary."
    return None


def _step_owner(step_id: Any, plan: dict | None) -> str | None:
    """Robot owning ``step_id`` in the nested resolved plan. Generated
    departure ids (``robot0#go_to_rest``) encode their robot as the '#'
    prefix, so they resolve even before the plan lookup."""
    if not isinstance(step_id, str) or not step_id:
        return None
    if "#" in step_id:
        return step_id.split("#", 1)[0]
    for task in (plan or {}).get("tasks", []):
        for step in task.get("steps", []):
            if step.get("id") == step_id:
                return task.get("robot")
    return None


def _applied_action_text(record: dict, plan: dict | None) -> str:
    """One accepted repair -> one user-facing sentence.

    Same effect-not-mechanism rule as resolver_progress_text, but past tense
    and with ``[[ref:...]]`` facility chips (the final chat message renders
    them; the plain-text progress trail must not use them).
    """
    tool = record.get("tool")
    args = record.get("args") or {}
    facility = ((record.get("focus_before") or {}).get("detail") or {}).get(
        "facility")
    chip = f"[[ref:{facility}]]" if facility else None
    if tool == "insert_yield":
        yielding = _step_owner(args.get("yielding_step"), plan)
        winner = _step_owner(args.get("winner_step"), plan)
        if yielding and winner:
            return (
                f"Sent {yielding} aside temporarily so {winner} could pass, "
                f"then resumed {yielding}'s route."
            )
        return "Sent one robot aside temporarily, then resumed its route."
    if tool == "insert_go_to" and args.get("robot"):
        at = f" at the {chip}" if chip else ""
        return f"Sent {args['robot']} to its parking spot once it finished{at}."
    if tool == "add_after":
        waiter = _step_owner(args.get("step"), plan)
        leader = _step_owner(args.get("after_step"), plan)
        if waiter and leader:
            if str(args.get("after_step", "")).endswith("#go_to_rest"):
                return (f"Made {waiter} wait until {leader} had left for "
                        "its parking spot.")
            where = f" clear the {chip}" if chip else " finish"
            return f"Made {waiter} wait for {leader} to{where} first."
        return "Adjusted the execution order so one robot waits for the other."
    if tool == "replan_path":
        mover = _step_owner(args.get("mover"), plan)
        avoid = args.get("avoid")
        if mover and avoid:
            return f"Re-routed {mover} to keep clear of {avoid}."
        return "Re-routed one robot around the other."
    if tool == "handoff_terminal_close" and args.get("to_robot"):
        what = f"closing the {chip}" if chip else "the final close"
        return f"Handed {what} to {args['to_robot']}, the last robot to use it."
    return "Applied a scheduling adjustment."


_NO_CONFLICTS_LINE = "No conflicts needed fixing; the plan is already clean."


def _resolution_progress_signature(plan: dict, report: dict) -> str:
    """Stable cross-pass state used to detect an actually stalled tail.

    Conflict counts are too coarse: an accepted repair can replace one
    warning with another while still changing the verified plan or advancing
    to a different conflict session.  Compare the committed plan together
    with normalized unresolved/session state instead.  Sorting makes detector
    ordering and report ordering irrelevant.
    """
    unresolved_state = []
    reason_counts: dict[str, int] = {}
    deferred_sessions = set()
    for item in report.get("unresolved") or []:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        session_id = item.get("session_id")
        if reason == "session_cap_reached" and session_id is not None:
            deferred_sessions.add(str(session_id))
        identity = item.get("fingerprint")
        if identity is None:
            identity = {
                "kind": item.get("kind"),
                "steps": sorted(str(value) for value in item.get("steps") or []),
                "robots": sorted(str(value) for value in item.get("robots") or []),
                "session_id": session_id,
            }
        unresolved_state.append({
            "identity": identity,
            "reason": reason,
            "session_id": session_id,
        })

    for item in report.get("deferred") or []:
        if not isinstance(item, dict) or item.get("reason") != "session_cap_reached":
            continue
        if item.get("session_id") is not None:
            deferred_sessions.add(str(item["session_id"]))

    sessions = report.get("sessions") or {}
    processed_sessions = (
        sorted(str(key) for key in sessions)
        if isinstance(sessions, dict)
        else []
    )
    material = {
        "plan": plan,
        "unresolved": sorted(
            unresolved_state,
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"), default=str
            ),
        ),
        "reason_counts": sorted(reason_counts.items()),
        "processed_sessions": processed_sessions,
        "deferred_sessions": sorted(deferred_sessions),
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolution_diagnostic_lines(report: dict) -> list[str]:
    """Compact machine-readable diagnostics shown only under See details."""
    lines = []
    stop_reason = report.get("stop_reason")
    if stop_reason:
        lines.append(f"Stop reason: {stop_reason}.")

    passes = report.get("passes")
    pass_cap = report.get("pass_cap")
    if isinstance(passes, int):
        suffix = f"/{pass_cap}" if isinstance(pass_cap, int) else ""
        lines.append(f"Resolve passes: {passes}{suffix}.")

    reason_counts: dict[str, int] = {}
    for item in report.get("unresolved") or []:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    if reason_counts:
        summary = ", ".join(
            f"{reason}={count}" for reason, count in sorted(reason_counts.items())
        )
        lines.append(f"Remaining: {summary}.")
    return lines


def _resolve_summary_sections(report: dict, plan: dict | None = None) -> tuple[str, str]:
    """Split the resolve report into (details, warning) -- the two halves D8
    puts in different places in the UI.

    ``details`` is the numbered adjustment list plus compound-turn time: the
    accountability record, useless while the turn is still running (none of
    it exists yet) and the whole reason it stays templated rather than
    LLM-written (D5's docstring on ``resolve_summary_text`` below). ``warning``
    is the non-converged / pin-deferral sentences: these must ride in the
    chat bubble itself (D8), never behind "See details" -- "N conflicts could
    not be fixed automatically" is precisely the sentence a user must not
    have to go looking for.

    Both are empty strings when there is nothing to say in that half (in
    particular, both are empty for the converged-with-nothing-to-fix case --
    callers that need a sentence for THAT case use ``_NO_CONFLICTS_LINE``
    directly, since it is a "nothing happened" line, not a detail or a
    warning).
    """
    applied = report.get("applied") or []
    unresolved = report.get("unresolved") or []
    deferred_pins = report.get("deferred_pins") or []
    converged = bool(report.get("converged"))
    actions = [_applied_action_text(record, plan) for record in applied]

    facilities = {
        ((record.get("focus_before") or {}).get("detail") or {}).get("facility")
        for record in applied
    }
    facilities.discard(None)
    near = (
        f" near the [[ref:{next(iter(facilities))}]]"
        if len(facilities) == 1 else ""
    )

    detail_lines: list[str] = []
    warning_lines: list[str] = []

    if converged and not actions and not deferred_pins:
        return "", ""

    if converged:
        plural = "s" if len(actions) != 1 else ""
        detail_lines.append(
            f"Resolved all conflicts with {len(actions)} adjustment{plural}{near}:")
    elif actions:
        detail_lines.append(f"Fixed some conflicts with {len(actions)} adjustment(s){near}:")
    for index, action in enumerate(actions, start=1):
        detail_lines.append(f"{index}. {action}")

    compound_turn_seconds = report.get("compound_turn_seconds")
    if isinstance(compound_turn_seconds, (int, float)):
        detail_lines.append(f"Turn time used: {compound_turn_seconds:.1f}s.")

    if not converged:
        detail_lines.extend(_resolution_diagnostic_lines(report))

    if not converged:
        count = len(unresolved)
        noun = "conflict" if count == 1 else "conflicts"
        warning_lines.append(
            f"{count} {noun} could not be fixed automatically; kept the last "
            "verified plan. Use sync to try again, or reorder/reassign "
            "manually."
            if count else
            "Stopped before full convergence; kept the last verified plan. "
            "Use sync to try again.")

    # Pin deferral (§4 items 11-13, S3): resolve never silently unpins -- a
    # focus left with no legal move SOLELY because of a pin (see
    # resolver_v2._apply_protected_pins) surfaces here by name instead of
    # blending into "unresolved". Empty whenever the turn carried no
    # `protected` set (every pre-S3 caller, and any S3 turn with no active
    # pins). Grouped with the non-converged sentence (not the adjustment
    # list): both are "something still needs your attention", D8's bubble
    # half, not the accountability record.
    for pin in deferred_pins:
        warning_lines.append(
            f"Your pinned {pin.get('pin')} conflicts with "
            f"{pin.get('conflicting_task')}; kept your setting -- use sync "
            "after you resolve it yourself."
        )
    return "\n".join(detail_lines), "\n".join(warning_lines)


def resolve_summary_text(report: dict, plan: dict | None = None) -> str:
    """Deterministic user-facing resolve summary (chat ``message_completed``).

    Templated straight from the structured report — no LLM call — so the
    accountability text cannot drift from what actually happened (same rule
    as render_v2_report, which stays tool-jargon for details/debugging). Used
    standalone by the plain resolve turn (``stream_resolve_turn``), which has
    no bubble/details split to make -- it just wants the whole thing as one
    string, verbatim as before D8. ``stream_compound_turn`` instead calls
    ``_resolve_summary_sections`` directly so it can route the two halves to
    different places (D8): this function is exactly
    ``"\\n".join(details, warning)`` with the same nothing-to-fix special case.
    """
    details, warning = _resolve_summary_sections(report, plan)
    if not details and not warning:
        return _NO_CONFLICTS_LINE
    return "\n".join(part for part in (details, warning) if part)


def _compound_bubble_text(author_message: str | None, details: str, warning: str) -> str:
    """``message_completed``'s text for a compound turn (D8).

    ``message_completed`` predates the ``turn_result`` artifact's separate
    ``author_message``/``resolve_summary``/``resolve_warning`` fields, which
    is what the frontend bubble actually renders from now. This function
    exists so ``message_completed`` stays a sensible STANDALONE fallback --
    for a caller that only reads the streamed text and never parses the
    artifact -- rather than being deleted outright: it mirrors exactly what
    the bubble ends up showing.

    - Author stage ran (the normal chat-turn case): the bubble is the
      author's own message plus the warning, if any -- never the itemized
      adjustment list, which is D8's whole point (that list is accountability
      detail, not headline news).
    - No author stage (``sync``/the classifier's structured "resolve" hint):
      there is no other headline to show, so the full old-style composition
      (details then warning) is used -- the resolve summary IS the content
      here, not a detail to hide. In practice these turns never render as a
      chat bubble at all (ScenePage's sync tail reports through its own
      `editActivity` line, not a message bubble), so this branch only feeds
      the raw stream / tests.
    """
    if author_message:
        return f"{author_message}\n\n{warning}" if warning else author_message
    if details or warning:
        return "\n\n".join(part for part in (details, warning) if part)
    return _NO_CONFLICTS_LINE


def _compiler_v2_compat_report(metadata: dict, elapsed_seconds: float) -> dict:
    """Adapt Compiler V2's repair ledger to the existing details/report UI."""
    applied = []
    for entry in metadata.get("repairs") or []:
        if not entry.get("accepted"):
            continue
        tool = entry.get("tool")
        if tool == "go_to_rest":
            applied.append({
                "tool": "insert_go_to",
                "args": {"robot": entry.get("robot")},
            })
        elif tool == "wait":
            applied.append({
                "tool": "add_after",
                "args": {
                    "step": entry.get("step"),
                    "after_step": entry.get("after"),
                },
            })
        elif tool == "yield":
            applied.append({
                "tool": "insert_yield",
                "args": {
                    "yielding_step": entry.get("yielding_step"),
                    "winner_step": entry.get("winner_step"),
                },
            })
        elif tool in ("detour", "go_away"):
            applied.append({
                "tool": "insert_yield",
                "args": {
                    "yielding_step": entry.get("step"),
                    "winner_step": entry.get("winner_step"),
                },
            })
        elif tool == "reroute":
            applied.append({
                "tool": "replan_path",
                "args": {
                    "mover": entry.get("step"),
                    "avoid": entry.get("avoid"),
                },
            })
    return {
        "converged": True,
        "applied": applied,
        "unresolved": [],
        "deferred_pins": [],
        "compile_count": 1,
        "compiler_v2": metadata,
        "compound_turn_seconds": max(0.0, float(elapsed_seconds)),
        "stop_reason": "compiler_v2_converged",
    }


def stream_resolve_turn(
    body: dict,
    write_event: WriteEvent,
    *,
    provider: Any | None = None,
    compile_fn: Any | None = None,
    topology_check_fn: Any | None = None,
    snapshot_lookup_fn: Any | None = None,
) -> None:
    """Run the v2 resolve core with an event adapter, emitting the envelope and
    ending with a ``resolve_result`` artifact.

    Never raises: any failure (validation, compile, resolver) is converted
    into a single ``error`` event and the function returns without emitting
    ``result``/``message_completed``.
    """
    turn_id = body.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        write_event({"type": "error", "text": "turn_id must be a non-empty string"})
        return

    write_event({"type": "message_started"})
    write_event(
        {
            "type": "intent_selected",
            "intent": "resolve",
            "text": "Checking the compiled plan for conflicts.",
        }
    )

    try:
        # Local import to avoid a hard import-time dependency loop with
        # service.py, mirroring _load_manifest/_default_provider below.
        from mujoco_skills.orchestrator.service import resolve_v2_core

        def on_event(kind: str, data: dict) -> None:
            if kind in ("session_deferred", "pin_deferred"):
                text = resolver_progress_text(kind, data)
                if text is not None:
                    write_event({"type": "warning", "text": text})
                return
            text = resolver_progress_text(kind, data)
            if text is not None:
                write_event({"type": "progress", "stage": kind, "text": text})

        result = resolve_v2_core(
            body,
            provider=provider or _default_provider(),
            compile_fn=compile_fn,
            topology_check_fn=topology_check_fn,
            snapshot_lookup_fn=snapshot_lookup_fn,
            on_event=on_event,
        )
        report = result.get("report") or {}
        summary = resolve_summary_text(report, result.get("plan"))
        write_event(
            {
                "type": "result",
                "artifact": {
                    "kind": "resolve_result",
                    "turn_id": turn_id,
                    "plan": result.get("plan"),
                    "compile": result.get("compile"),
                    "report": report,
                    "initial_snapshot_reused": result.get("initial_snapshot_reused"),
                },
            }
        )
        write_event({"type": "message_completed", "text": summary})
    except Exception as exc:  # noqa: BLE001 - convert any resolve failure to one error event
        write_event({"type": "error", "text": str(exc)})


def _merge_semantic_ordering_protected(protected: object, actions: object) -> dict:
    """Add authored semantic precedence to the resolver's existing pin shape."""
    base = protected if isinstance(protected, dict) else {}
    semantic_edges = [
        (predecessor, action.get("id"))
        for action in (actions if isinstance(actions, list) else [])
        if isinstance(action, dict) and isinstance(action.get("id"), str)
        for predecessor in (action.get("after") or [])
        if isinstance(predecessor, str) and predecessor and predecessor != action.get("id")
    ]
    # Keep the legacy wire object byte-for-byte shape when there is no authored
    # semantic ordering to add.  Older callers intentionally send only the
    # populated protected categories.
    if not semantic_edges:
        return dict(base)
    result = {
        "allocations": list(base.get("allocations") or []),
        "orderings": list(base.get("orderings") or []),
        # Exact edges are only created from manual edit deltas by the
        # frontend. Semantic author ordering remains task-level protection.
        "exact_after_edges": list(base.get("exact_after_edges") or []),
        "exact_after_fields": list(base.get("exact_after_fields") or []),
        "destinations": list(base.get("destinations") or []),
        "waypoints": list(base.get("waypoints") or []),
    }
    seen = {(item.get("before"), item.get("after")) for item in result["orderings"] if isinstance(item, dict)}
    for predecessor, dependent in semantic_edges:
        edge = (predecessor, dependent)
        if edge not in seen:
            result["orderings"].append({"before": edge[0], "after": edge[1]})
            seen.add(edge)
    return result


def _filter_protected_for_plan(protected: object, actions: object, plan: object) -> dict:
    """Drop manual/semantic pins whose targets vanished during authoring.

    A structural author turn (notably remove_task) cleanly re-decomposes the
    semantic action list. The protected set was built from the pre-author plan,
    so carrying deleted action/group/step ids into the resolver would create
    dangling constraints. Stable ids on surviving actions and steps make this
    a precise filter rather than a best-effort rewrite.
    """
    base = protected if isinstance(protected, dict) else {}
    action_ids = {
        action.get("id")
        for action in (actions if isinstance(actions, list) else [])
        if isinstance(action, dict) and isinstance(action.get("id"), str)
    }
    tasks = plan.get("tasks") if isinstance(plan, dict) else []
    step_ids = {
        step.get("id")
        for task in (tasks if isinstance(tasks, list) else [])
        if isinstance(task, dict)
        for step in (task.get("steps") or [])
        if isinstance(step, dict) and isinstance(step.get("id"), str)
    }

    def objects(name: str) -> list[dict]:
        values = base.get(name) or []
        return [value for value in values if isinstance(value, dict)]

    return {
        "allocations": [
            item for item in objects("allocations") if item.get("group") in action_ids
        ],
        "orderings": [
            item for item in objects("orderings")
            if item.get("before") in action_ids and item.get("after") in action_ids
        ],
        "exact_after_edges": [
            item for item in objects("exact_after_edges")
            if item.get("step") in step_ids and item.get("after_step") in step_ids
        ],
        "exact_after_fields": [
            item for item in objects("exact_after_fields")
            if item.get("step") in step_ids
            and all(value in step_ids for value in (item.get("after") or []))
        ],
        "destinations": [
            item for item in objects("destinations") if item.get("step") in step_ids
        ],
        "waypoints": [
            item for item in objects("waypoints") if item.get("step") in step_ids
        ],
    }


def stream_compound_turn(
    body: dict,
    write_event: WriteEvent,
    *,
    provider: Any | None = None,
    manifest: dict | None = None,
    compile_fn: Any | None = None,
    topology_check_fn: Any | None = None,
    snapshot_lookup_fn: Any | None = None,
    time_fn: Any | None = None,
    elapsed_time_fn: Any | None = None,
) -> None:
    """Author (optional) -> Compile -> conditional bounded Resolve -> one
    ``turn_result``. The compound_turn_integration_spec's tail: one user turn,
    one live assistant message, at most one committed plan.

    ``intent_hint in ("sync", "edit")`` skips the author stage entirely (D1/D3:
    manual edits and the sync button are already structured, not natural
    language). ``"sync"`` takes the plan straight from ``body["plan"]``
    (the caller's last committed, already-resolved plan). ``"edit"`` (S3, the
    manual-edit tail) takes its plan from ``body["previous_plan"]`` -- the
    SAME already-resolved plan ``"sync"`` uses -- and then applies this
    batch's deltas onto it (D2b). Otherwise the author stage runs exactly as
    ``stream_author_turn`` does through its ``decomposition`` line.

    D2b (revises D2/item 16): the user edits the RESOLVED plan -- it is the
    only plan D1 ever shows them, so it is also the only plan they could have
    edited -- and a resolved plan contains steps the authored layer cannot
    express: ``go_to_rest`` departure legs and cross-robot ``after`` edges the
    resolver inserted. D2's original design replayed edits onto a FRESH
    ``decompose(current_actions)`` for the edit path; that plan has no
    ``go_to_rest`` step at all, so a delta targeting one had no target
    (silently dropped) while every prior repair was discarded in the same
    stroke -- one edit, two symptoms, one cause. Editing an artifact was not
    an edge case to special-case later; it is the ordinary act of adjusting
    the plan the user is looking at. Per path:
      - ``"edit"``: previous resolved plan + this batch's deltas.
      - ``"sync"`` with nothing pending: previous resolved plan, unchanged --
        replaying the same deltas onto it again would double-apply them.
      - author turn, pure append (D1b): the graft target IS the previous
        resolved plan, so replaying onto it is exactly as safe as the edit
        path.
      - author turn, not a pure append: a clean recompute has no resolved-plan
        continuity at all -- see the branch below, which drops the whole
        batch rather than best-effort id-matching against a plan the
        restructuring never touched.
    A delta whose target genuinely does not exist in the plan it was applied
    to is dropped with a visible ``warning`` activity line, never silently
    (D2's stated risk, still real for a batch containing its own stale/
    contradictory edits).

    Resolve is skipped (D4) when the compiled plan has zero
    ``conflict_payload.delegable_conflicts`` -- the resolver's own queue
    predicate, so "we skipped resolve" can never disagree with "the resolver
    had nothing to do". When resolve does run, it auto-continues across
    multiple bounded passes (D3b) because ``run_resolution_v2`` admits at most
    ``MAX_SESSIONS_PER_RUN`` conflict groups per call; a second pass re-admits
    whatever the first deferred. A pass in flight always finishes (the
    deadline is only checked between passes), so the plan handed to the
    terminal artifact is always a verified compile.

    Never raises: author/compile/resolve failures each degrade per
    compound_turn_integration_spec §7 rather than aborting the turn -- a
    resolve failure in particular still commits the author's compiled plan.
    ``time_fn`` (default ``time.monotonic``) exists so the resolve deadline is
    testable without sleeping. ``elapsed_time_fn`` independently measures the
    whole compound response for user-facing details, so deadline-clock tests
    do not have to account for those additional reads.
    """
    elapsed_time_fn = elapsed_time_fn or time.monotonic
    turn_started_at = elapsed_time_fn()
    # Local imports: same import-time cycle avoidance as
    # _load_manifest/_default_provider (service.py imports this module).
    from mujoco_skills.orchestrator.conflict_payload import delegable_conflicts
    from mujoco_skills.orchestrator import plan_edits
    from mujoco_skills.orchestrator.service import (
        _compile_via_skill_service,
        _nest_resolved_plan,
        _public_compile_result,
        _validate_via_skill_service,
        resolve_v2_core,
    )
    from mujoco_skills.orchestrator.compiler_v2_plan import (
        prepare_compiler_v2_input,
    )

    turn_id = body.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        write_event({"type": "error", "text": "turn_id must be a non-empty string"})
        return

    time_fn = time_fn or time.monotonic
    max_passes = int(os.environ.get("COMPOUND_RESOLVE_MAX_PASSES", "4"))
    deadline_seconds = float(os.environ.get("COMPOUND_TAIL_DEADLINE_SECONDS", "240"))
    uses_default_compile = compile_fn is None
    compile_fn = compile_fn or _compile_via_skill_service
    if topology_check_fn is None:
        # Unit callers commonly inject a synthetic compiler; do not make those
        # tests (or alternate embedders) depend on the HTTP skill service.
        topology_check_fn = (
            _validate_via_skill_service if uses_default_compile else lambda _plan: None
        )
    intent_hint = body.get("intent_hint")
    # S3 items 11-13: threaded straight into every resolve_v2_core call this
    # turn makes (round 0 and every auto-continue pass) -- see D2, "one
    # structure, two uses": the frontend derives this from the same `edits`
    # list that `plan_edits.replay_edits` below replays.
    protected = _merge_semantic_ordering_protected(
        body.get("protected"), body.get("current_actions")
    )

    stages: list[str] = []
    actions: list[dict] | None = None
    author_message: str | None = None
    authoring_summary = ""
    # D1b diagnosis; stays None on sync/edit turns, which have no action list
    # to promote from (they carry an already-resolved plan straight from
    # body["plan"]/body["previous_plan"], per D2b).
    promotion_reason: str | None = None
    dropped_edits: list[dict] = []
    applied_edits: list[dict] = []
    edit_base_plan: dict | None = None

    if intent_hint in ("sync", "edit"):
        # No natural-language input to translate -- the plan is already a
        # structured delta (manual edit) or a re-request of the last one
        # (sync button). stream_author_turn/​_run_author_stage own their own
        # message_started; here the compound turn owns it directly.
        write_event({"type": "message_started"})
        # D2b: both hints take their plan from the same place -- the previous
        # turn's already-resolved plan. "sync" reads it from body["plan"]
        # (dispatch rewrites the button press to hand over
        # plan_state.completed_plan there); "edit" reads it from
        # body["previous_plan"] (the frontend's livePlan, sent with every
        # turn). Neither decomposes from current_actions: that would rebuild
        # the AUTHORED plan, which cannot express the resolver artifacts
        # (go_to_rest legs, cross-robot after edges) the user is actually
        # looking at and may be editing.
        plan = body.get("plan") if intent_hint == "sync" else body.get("previous_plan")
        if not isinstance(plan, dict):
            field = "plan" if intent_hint == "sync" else "previous_plan"
            write_event({"type": "error", "text": f"{field} must be an object for a {intent_hint} turn"})
            return
        if intent_hint == "edit":
            edit_base_plan = plan
    else:
        stage = _run_author_stage(
            body,
            write_event,
            provider=provider,
            manifest=manifest,
            # No review/Compile ceremony exists downstream of Author on this
            # path (integration spec §4.3): the tail compiles immediately.
            decomposition_text="Plan drafted; compiling.",
        )
        if stage is None:
            return  # _run_author_stage already emitted the error event
        stages.append("authoring")
        actions, author_message, plan = stage["actions"], stage["message"], stage["plan"]
        authoring_summary = stage["authoring_summary"]
        if plan is None:
            previous_actions = body.get("current_actions") or []
            cleared = bool(previous_actions) and actions == []
            # A successful removal of the last semantic action is a real empty
            # plan replacement, not the legacy no-plan/preserve signal. Return
            # a valid empty plan+compile so the existing frontend commit path
            # clears its timeline and still snapshots the old plan for Revert.
            terminal_plan = {"tasks": []} if cleared else None
            terminal_compile = (
                {
                    "schedule": [],
                    "warnings": [],
                    "conflicts": [],
                    "completed": {},
                    "compile_id": None,
                }
                if cleared else None
            )
            write_event(
                {
                    "type": "result",
                    "artifact": {
                        "kind": "turn_result",
                        "turn_id": turn_id,
                        "plan": terminal_plan,
                        "compile": terminal_compile,
                        "actions": actions,
                        "stages": stages,
                        # D8: no resolve stage ran (nothing to compile), so
                        # there is no summary/warning half -- the bubble is
                        # just the author message, same as before D8.
                        "author_message": author_message,
                        "authoring_summary": authoring_summary,
                        "resolve_summary": "",
                        "resolve_warning": "",
                    },
                }
            )
            write_event({"type": "message_completed", "text": author_message})
            return

        # D1b: reuse the previous turn's resolved plan instead of re-deriving
        # (and re-resolving, from scratch) it -- see decompose.merge_appended_
        # tasks. Only reachable here (the author stage ran): a sync/edit turn
        # has no "new_actions" to compare against a previous action list, and
        # already carries its own already-resolved `plan` from body["plan"].
        previous_plan = body.get("previous_plan")
        try:
            merge_manifest = manifest or _load_manifest()
            previous_actions = authoring.parse_actions(
                body.get("current_actions", []), merge_manifest
            )
            new_actions = authoring.parse_actions(actions, merge_manifest)
            promotion_reason = decompose.append_promotion_reason(
                previous_actions, previous_plan, new_actions
            )
            merged_plan = (
                decompose.merge_appended_tasks(
                    previous_actions,
                    previous_plan,
                    new_actions,
                    merge_manifest,
                    body.get("scene_refs") or [],
                )
                if promotion_reason is None
                else None
            )
        except Exception as exc:  # noqa: BLE001 - malformed input just skips promotion
            merged_plan, promotion_reason = None, f"error: {exc}"
        # Printed, not just carried in the artifact: a promotion that silently
        # stops applying looks exactly like a slow turn from the outside.
        # Diagnostics must never be able to terminate the user stream. On
        # Windows a supervised/background process can retain a Python stdout
        # object whose underlying pipe has already closed; bare print(...,
        # flush=True) then raises OSError at exactly this point, after Author's
        # decomposition event but before compile/result.
        try:
            print(f"[compound] append_promotion: {promotion_reason or 'applied'}",
                  flush=True)
        except (BrokenPipeError, OSError, ValueError):
            pass
        if merged_plan is not None:
            plan = merged_plan
            write_event(
                {
                    "type": "progress",
                    "stage": "reusing_resolution",
                    "text": "Keeping the conflict fixes from your earlier turns.",
                }
            )

        # Filter only after pure-append promotion has chosen the actual plan
        # carried forward. Filtering against the fresh decomposed draft above
        # would incorrectly discard pins on resolver artifacts that promotion
        # preserves verbatim (for example a manually edited go_to_rest step).
        protected = _filter_protected_for_plan(protected, actions, plan)
        protected = _merge_semantic_ordering_protected(protected, actions)

    edits = body.get("edits") or []
    removal_ids = plan_edits.semantic_removal_ids(edits)
    replayable_edits = plan_edits.without_semantic_removals(edits)

    # A Gantt remove is a semantic mutation, not a plan-step deletion. Apply
    # it to current_actions through Author's deterministic remove_task
    # implementation, then decompose again before replaying the remaining
    # manual plan edits. This keeps future turns from resurrecting a task and
    # gives container-envelope/dependency cleanup exactly one implementation.
    if removal_ids and intent_hint != "sync":
        try:
            removal_manifest = manifest or _load_manifest()
            previous_semantic = authoring.parse_actions(
                body.get("current_actions", []), removal_manifest
            )
            previous_ids = {action.id for action in previous_semantic}
            unknown_ids = [action_id for action_id in removal_ids if action_id not in previous_ids]
            if unknown_ids:
                raise ValueError(f"unknown action id {unknown_ids[0]!r}")

            # An authoring turn may already have removed one of the staged
            # targets. Treat that target as satisfied; apply the atomic batch
            # to whichever targets remain in the authored result.
            semantic_source = (
                authoring.parse_actions(actions, removal_manifest)
                if actions is not None
                else previous_semantic
            )
            present_ids = {action.id for action in semantic_source}
            pending_ids = [action_id for action_id in removal_ids if action_id in present_ids]
            updated_semantic = (
                authoring.remove_tasks(semantic_source, pending_ids)
                if pending_ids
                else semantic_source
            )
            actions = [asdict(action) for action in updated_semantic]
            plan = (
                decompose.decompose(
                    updated_semantic, removal_manifest, body.get("scene_refs") or []
                )
                if updated_semantic
                else {"tasks": []}
            )
            protected = _filter_protected_for_plan(protected, actions, plan)
            protected = _merge_semantic_ordering_protected(protected, actions)
            applied_edits.extend(
                edit for edit in edits if (edit or {}).get("op") == "remove_task"
            )
            write_event(
                {
                    "type": "progress",
                    "stage": "restaging",
                    "text": "Removing the selected tasks and rechecking coordination.",
                }
            )
        except Exception as exc:  # noqa: BLE001 - reject the whole removal batch
            write_event({"type": "error", "text": str(exc)})
            return

    if intent_hint == "sync":
        # Skipped: "sync"'s plan is the last turn's ALREADY-resolved
        # completed_plan, so replaying the same deltas again would
        # double-apply them (e.g. an add_after dependency inserted twice is
        # harmless, but a set_task_robot lock is not idempotent against a
        # plan that has since been reshaped by the resolver under a
        # different task grouping). "sync" is only ever sent with nothing
        # pending anyway (D2b table, row 2).
        pass
    elif intent_hint == "edit" or promotion_reason is None:
        # "edit" and the D1b pure-append graft both land on a plan with real
        # continuity back to the previous resolved plan (identically, for
        # "edit"; via the graft, for pure-append), so a delta's target id has
        # a real chance of still being there. What doesn't survive is
        # reported per-delta, never silently (D2's stated risk).
        plan, dropped_edits = plan_edits.replay_edits(plan, replayable_edits)
        for delta in dropped_edits:
            write_event({"type": "warning", "text": plan_edits.dropped_edit_text(delta)})
        # A structural delta that actually LANDED invalidates the departure
        # anchors the resolver derived from the old allocation/ordering; they
        # are conclusions, and the premises just changed. Withdraw them so
        # resolve decides afresh instead of inheriting them (this is what
        # makes an obsolete "go to rest" leg disappear again after a task
        # reassignment -- it used to vanish only as a side effect of the
        # rebuild-from-scratch D2b removed). Keyed on the APPLIED subset: a
        # delta that was dropped above changed nothing, so it justifies
        # nothing. Identity comparison, not equality -- two deltas targeting
        # the same step can be legitimately equal by value.
        applied = [
            delta for delta in replayable_edits
            if not any(delta is dropped for dropped in dropped_edits)
        ]
        applied_edits.extend(applied)
        if plan_edits.has_structural_edit(applied):
            plan = decompose.strip_stale_departures(plan)
            write_event(
                {
                    "type": "progress",
                    "stage": "restaging",
                    "text": "Rechecking coordination after your change.",
                }
            )
    else:
        # D2b: a non-pure-append author turn recomputes the authored plan
        # from scratch -- the user just restructured the very tasks these
        # edits were tweaking. Best-effort id matching against that fresh
        # plan would sometimes succeed by COINCIDENCE (decompose's step ids
        # are `f"{action_id}:s{index}"`, so an untouched action keeps its old
        # ids even when the plan around it changed), silently reapplying an
        # edit into a context the user never approved. D2b's decision is
        # blunter and safer: the whole pending batch is lost here, and said
        # so out loud, every time -- never a coin flip.
        dropped_edits = list(replayable_edits)
        for delta in dropped_edits:
            write_event({"type": "warning", "text": plan_edits.dropped_edit_text(delta)})

    edit_message = (
        plan_edits.applied_edit_message(
            applied_edits,
            edit_base_plan,
            plan,
            body.get("current_actions") or actions or [],
            dropped_count=len(dropped_edits),
        )
        if intent_hint == "edit"
        else ""
    )

    if removal_ids and actions == []:
        empty_compile = {
            "schedule": [],
            "warnings": [],
            "conflicts": [],
            "completed": {},
            "compile_id": None,
        }
        write_event({"type": "progress", "stage": "verified", "text": "Verified."})
        write_event(
            {
                "type": "result",
                "artifact": {
                    "kind": "turn_result",
                    "turn_id": turn_id,
                    "plan": {"tasks": []},
                    "compile": empty_compile,
                    "actions": [],
                    "stages": stages + ["verified"],
                    "dropped_edits": dropped_edits,
                    "edit_message": edit_message,
                    "author_message": author_message,
                    "authoring_summary": authoring_summary,
                    "resolve_summary": "",
                    "resolve_warning": "",
                },
            }
        )
        message = author_message or "Removed the selected tasks."
        write_event({"type": "message_completed", "text": message})
        return

    compiler_v2_preparation = None
    if os.environ.get("COMPILER_VERSION", "v1").strip().lower() == "v2":
        plan, compiler_v2_preparation = prepare_compiler_v2_input(plan)

    try:
        topology_check_fn(plan)
    except Exception as exc:  # candidate is never committed on a topology rejection
        details = getattr(exc, "error", None)
        write_event({
            "type": "error",
            "text": str(exc),
            "details": details if isinstance(details, dict) else {
                "code": getattr(exc, "code", "invalid_plan_topology"),
                "message": str(exc),
            },
        })
        return

    write_event({"type": "progress", "stage": "compiling", "text": "Compiling the plan."})
    try:
        # This is the user-visible compile (retain_snapshot=True): its
        # compile_id is what a later sync press can reuse without recompiling.
        compile_result = compile_fn(plan, retain_snapshot=True)
    except Exception as exc:  # noqa: BLE001 - plan stays untouched on a compile failure
        event = {"type": "error", "text": str(exc)}
        detail = getattr(exc, "detail", None)
        if isinstance(detail, dict) and detail:
            event["details"] = detail
        write_event(event)
        return
    stages.append("compiling")

    compiler_v2_metadata = compile_result.get("compiler_v2")
    if isinstance(compiler_v2_metadata, dict):
        final_flat = compile_result.get("completed") or {}
        final_plan = _nest_resolved_plan(final_flat)
        public_compile = _public_compile_result(compile_result, final_flat)
        report = _compiler_v2_compat_report(
            compiler_v2_metadata,
            elapsed_time_fn() - turn_started_at,
        )
        report["preparation"] = (
            compiler_v2_metadata.get("preparation")
            or compiler_v2_preparation
        )
        stages.append("verified")
        write_event({"type": "progress", "stage": "verified", "text": "Verified."})
        resolve_summary, resolve_warning = _resolve_summary_sections(
            report, final_plan)
        write_event({
            "type": "result",
            "artifact": {
                "kind": "turn_result",
                "turn_id": turn_id,
                "plan": final_plan,
                "compile": public_compile,
                "report": report,
                "actions": actions,
                "stages": stages,
                "append_promotion": promotion_reason or "applied",
                "dropped_edits": dropped_edits,
                "edit_message": edit_message,
                "author_message": author_message,
                "authoring_summary": authoring_summary,
                "resolve_summary": resolve_summary,
                "resolve_warning": resolve_warning,
            },
        })
        write_event({
            "type": "message_completed",
            "text": _compound_bubble_text(
                author_message, resolve_summary, resolve_warning),
        })
        return

    delegable_before = delegable_conflicts(compile_result)
    if not delegable_before:
        # Fast path (D4): the resolver's own queue would be empty, so there is
        # nothing for it to do.
        public_compile = _public_compile_result(
            compile_result, compile_result.get("completed", {}))
        stages.append("verified")
        write_event({"type": "progress", "stage": "verified", "text": "Verified."})
        write_event(
            {
                "type": "result",
                "artifact": {
                    "kind": "turn_result",
                    "turn_id": turn_id,
                    "plan": plan,
                    "compile": public_compile,
                    "actions": actions,
                    "stages": stages,
                    "append_promotion": promotion_reason or "applied",
                    "dropped_edits": dropped_edits,
                    "edit_message": edit_message,
                    # D8: resolve never ran (D4 fast path), so there is
                    # nothing to itemize and nothing to warn about -- both
                    # halves are empty and "See details" must not appear.
                    "author_message": author_message,
                    "authoring_summary": authoring_summary,
                    "resolve_summary": "",
                    "resolve_warning": "",
                },
            }
        )
        write_event(
            {"type": "message_completed", "text": _compound_bubble_text(author_message, "", "")}
        )
        return

    def on_event(kind: str, data: dict) -> None:
        if kind in ("session_deferred", "pin_deferred"):
            text = resolver_progress_text(kind, data)
            if text is not None:
                write_event({"type": "warning", "text": text})
            return
        text = resolver_progress_text(kind, data)
        if text is not None:
            write_event({"type": "progress", "stage": kind, "text": text})

    stages.append("resolving")
    passes = 0
    stop_reason = "converged"
    aggregated_applied: list[dict] = []
    aggregated_rounds: list[dict] = []
    aggregated_deferred_pins: list[dict] = []
    compile_count_total = 0
    seen_resolution_states: set[str] = set()
    last_plan, last_compile_public = plan, None
    last_raw_compile = compile_result
    final_report: dict | None = None
    deadline_at = time_fn() + deadline_seconds
    # Round 0's compile is already in hand -- hand it straight to the first
    # pass so resolve_v2_core does not recompile something it was just given
    # (service.py item 9). Later passes let resolve_v2_core do its own
    # fallback compile from the previous pass's committed plan.
    initial_compile_result = compile_result

    while True:
        passes += 1
        try:
            result = resolve_v2_core(
                {"plan": last_plan, "protected": protected},
                provider=provider,
                compile_fn=compile_fn,
                topology_check_fn=topology_check_fn,
                snapshot_lookup_fn=snapshot_lookup_fn,
                on_event=on_event,
                initial_compile_result=initial_compile_result,
            )
        except Exception as exc:  # noqa: BLE001 - a resolve failure must never fail the turn
            write_event(
                {
                    "type": "warning",
                    "text": f"Could not check for scheduling conflicts ({exc}); "
                    "kept the compiled plan.",
                }
            )
            stop_reason = "resolve_error"
            break

        report = result["report"]
        final_report = report
        last_plan = result["plan"]
        last_compile_public = result["compile"]
        aggregated_applied.extend(report.get("applied", []))
        aggregated_rounds.extend(report.get("rounds", []))
        aggregated_deferred_pins.extend(report.get("deferred_pins", []))
        compile_count_total += report.get("compile_count", 0)
        unresolved = report.get("unresolved") or []

        if report.get("converged"):
            stop_reason = "converged"
            break
        if passes >= max_passes:
            stop_reason = "pass_cap"
            break
        if time_fn() >= deadline_at:
            stop_reason = "deadline"
            break
        if unresolved and all(
            item.get("reason") == "no_remaining_legal_strategy" for item in unresolved
        ):
            stop_reason = "stuck"
            break
        state_signature = _resolution_progress_signature(last_plan, report)
        if state_signature in seen_resolution_states:
            stop_reason = "no_progress"
            break
        seen_resolution_states.add(state_signature)

        # A later pass recompiles from the plan the previous pass just
        # verified -- it holds no ready-made compile_result of its own.
        initial_compile_result = None
        write_event(
            {
                "type": "progress",
                "stage": "resolving_again",
                "text": "More conflicts than one pass covers; continuing.",
            }
        )

    if final_report is None:
        # Resolve never completed a single pass (it raised immediately):
        # commit the compiled-but-unresolved plan per §7's failure table.
        final_plan, public_compile = plan, _public_compile_result(
            last_raw_compile, last_raw_compile.get("completed", {}))
        merged_report = {"converged": False, "applied": [], "unresolved": [],
                         "deferred_pins": aggregated_deferred_pins,
                         "passes": passes, "pass_cap": max_passes,
                         "stop_reason": stop_reason}
    else:
        final_plan, public_compile = last_plan, last_compile_public
        merged_report = {
            **final_report,
            "applied": aggregated_applied,
            "rounds": aggregated_rounds,
            "deferred_pins": aggregated_deferred_pins,
            "compile_count": compile_count_total,
            "passes": passes,
            "pass_cap": max_passes,
            "stop_reason": stop_reason,
        }

    merged_report["compound_turn_seconds"] = max(
        0.0, elapsed_time_fn() - turn_started_at
    )

    stages.append("verified")
    write_event({"type": "progress", "stage": "verified", "text": "Verified."})
    # D8: split at the source rather than handing the frontend one composed
    # string to regex apart. `resolve_summary` (the numbered adjustments +
    # compound-turn elapsed time) is the accountability record -- it lands behind
    # "See details". `resolve_warning` (non-converged count / pin deferral)
    # must ride in the chat bubble itself, same standing as `author_message`
    # -- "N conflicts could not be fixed automatically" is precisely the
    # sentence a user must not have to go looking for.
    resolve_summary, resolve_warning = _resolve_summary_sections(merged_report, final_plan)
    write_event(
        {
            "type": "result",
            "artifact": {
                "kind": "turn_result",
                "turn_id": turn_id,
                "plan": final_plan,
                "compile": public_compile,
                "report": merged_report,
                "actions": actions,
                "stages": stages,
                "append_promotion": promotion_reason or "applied",
                "dropped_edits": dropped_edits,
                "edit_message": edit_message,
                "author_message": author_message,
                "authoring_summary": authoring_summary,
                "resolve_summary": resolve_summary,
                "resolve_warning": resolve_warning,
            },
        }
    )
    write_event(
        {
            "type": "message_completed",
            "text": _compound_bubble_text(author_message, resolve_summary, resolve_warning),
        }
    )


# ---------------------------------------------------------------------------
# Part B -- read-only Explain agent (phase4 spec section 4)
# ---------------------------------------------------------------------------

_NO_ARGS_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

READ_AUTHOR_TRANSCRIPT_TOOL = ToolSpec(
    name="read_author_transcript",
    description=(
        "Read the author-tagged conversation transcript (user + assistant "
        "turns of the authoring workflow only)."
    ),
    parameters=_NO_ARGS_SCHEMA,
)

READ_RESOLVER_REPORT_TOOL = ToolSpec(
    name="read_resolver_report",
    description="Read the last resolver run's report, if one exists.",
    parameters=_NO_ARGS_SCHEMA,
)

READ_PLAN_TOOL = ToolSpec(
    name="read_plan",
    description="Read the current draft and completed plan.",
    parameters=_NO_ARGS_SCHEMA,
)

READ_CONFLICTS_TOOL = ToolSpec(
    name="read_conflicts",
    description="Read the current conflicts, warnings, and plan status.",
    parameters=_NO_ARGS_SCHEMA,
)

RESPOND_TOOL = ToolSpec(
    name="respond",
    description="Deliver the final read-only answer. Never proposes plan changes.",
    parameters={
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "follow_up": {"type": "boolean"},
        },
        "required": ["answer"],
        "additionalProperties": False,
    },
)

# Intentionally reviewable: exactly the four read-only tools plus the
# terminal `respond` -- zero mutation tools of any kind.
EXPLAIN_TOOLS = (
    READ_AUTHOR_TRANSCRIPT_TOOL,
    READ_RESOLVER_REPORT_TOOL,
    READ_PLAN_TOOL,
    READ_CONFLICTS_TOOL,
    RESPOND_TOOL,
)

EXPLAIN_PROMPT = (
    "You are a read-only assistant answering questions about the current "
    "two-robot task plan. You MUST ground every factual claim by calling "
    "read_author_transcript, read_resolver_report, read_plan, and/or "
    "read_conflicts -- never guess or invent plan state. You MUST NOT "
    "propose, suggest, or imply any plan edit, reassignment, or resolve "
    "action; you have no tool that can change anything. If the user is "
    "actually asking for a change (not just an explanation), say plainly "
    "that you can't change the plan here and that they should use Author or "
    "Resolve instead -- you may set follow_up to true when you are asking "
    "the user something back. Keep answers concise and never mention "
    "internal step ids, fingerprints, raw tool JSON, or session keys. "
    "Always finish the turn by calling respond with your final answer."
)

# Method-B (phase4 spec section 4.2): tool_call name -> fixed English progress
# text. assistant_text/tool_result/max_iters never produce default progress.
EXPLAIN_PROGRESS = {
    "read_author_transcript": "Reviewing the authoring history.",
    "read_resolver_report": "Reviewing the last resolve result.",
    "read_plan": "Looking at the current plan.",
    "read_conflicts": "Checking the current conflicts.",
}


class _ExplainExecutor:
    """Read-only executor: every tool reads from ``body``; nothing mutates."""

    def __init__(self, body: dict):
        self.body = body
        self.answer: str | None = None
        self.follow_up = False

    def execute(self, name: str, arguments: dict) -> dict:
        plan_state = self.body.get("plan_state") or {}
        if name == "read_author_transcript":
            messages = self.body.get("messages") or []
            transcript = [
                {"role": item.get("role"), "content": item.get("content")}
                for item in messages
                if isinstance(item, dict)
                and item.get("role") in ("user", "assistant")
                and item.get("source") in (None, "authoring")
            ]
            return {"transcript": transcript}
        if name == "read_resolver_report":
            report = plan_state.get("last_resolver_report")
            return report if isinstance(report, dict) else {"available": False}
        if name == "read_plan":
            return {
                "draft": plan_state.get("draft_plan"),
                "completed": plan_state.get("completed_plan"),
            }
        if name == "read_conflicts":
            return {
                "conflicts": plan_state.get("conflicts"),
                "warnings": plan_state.get("warnings"),
                "status": plan_state.get("status"),
                "in_sync": plan_state.get("in_sync"),
                "delegable_conflict_count": plan_state.get("delegable_conflict_count"),
            }
        if name == "respond":
            self.answer = str(arguments.get("answer") or "")
            self.follow_up = bool(arguments.get("follow_up", False))
            return {"ok": True}
        return {"error": f"unknown explain tool {name!r}"}


def stream_explain_turn(
    body: dict,
    write_event: WriteEvent,
    *,
    provider: Any | None = None,
) -> None:
    """Run the read-only Explain agent, emitting envelope events via
    ``write_event`` and ending in an ``answer`` artifact.

    Never raises: any exception, or a loop that ends without a usable answer,
    is converted into a single ``error`` event and the function returns
    without emitting ``result``/``message_completed``.
    """
    turn_id = body.get("turn_id")

    write_event({"type": "message_started"})
    write_event(
        {
            "type": "intent_selected",
            "intent": "explain",
            "text": "Looking into your question.",
        }
    )

    try:
        messages = body.get("messages")
        if not isinstance(messages, list):
            raise ValueError("messages must be an array")
        latest_index = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if isinstance(messages[index], dict)
                and messages[index].get("role") == "user"
            ),
            None,
        )
        if latest_index is None:
            raise ValueError("explain requires at least one user message")
        latest_user_text = str(messages[latest_index].get("content", ""))

        provider = provider or _default_provider()
        executor = _ExplainExecutor(body)

        def on_event(kind: str, payload: object) -> None:
            if kind != "tool_call":
                # "assistant_text", "tool_result", "max_iters" are model
                # chain-of-thought / low-level detail; never default progress.
                return
            name = getattr(payload, "name", None)
            text = EXPLAIN_PROGRESS.get(name)
            if text is not None:
                write_event({"type": "progress", "stage": name, "text": text})

        history = loop.run(
            latest_user_text,
            provider,
            list(EXPLAIN_TOOLS),
            executor.execute,
            history=[Message("system", EXPLAIN_PROMPT)],
            terminal_tools={"respond"},
            on_event=on_event,
        )

        if executor.answer is not None:
            answer = executor.answer
            follow_up = executor.follow_up
        else:
            # No respond call: fall back to the last assistant prose.
            answer = None
            for message in reversed(history):
                if message.role == "assistant" and message.content:
                    answer = message.content
                    break
            if answer is None:
                write_event(
                    {
                        "type": "error",
                        "text": "explain turn ended without an answer",
                    }
                )
                return
            follow_up = False

        write_event(
            {
                "type": "result",
                "artifact": {
                    "kind": "answer",
                    "turn_id": turn_id,
                    "content": answer,
                    "follow_up": follow_up,
                },
            }
        )
        write_event({"type": "message_completed", "text": answer})
    except Exception as exc:  # noqa: BLE001 - convert any explain failure to one error event
        write_event({"type": "error", "text": str(exc)})


def _load_manifest() -> dict:
    # Local import to avoid a hard dependency loop at module import time and to
    # mirror do_author's `manifest or load_manifest()` fallback.
    from mujoco_skills.orchestrator.service import load_manifest

    return load_manifest()


def _default_provider() -> Any:
    # Local import: only touched when the caller (production service.py path)
    # omits a provider; tests always inject a fake provider and never hit this.
    from mujoco_skills.orchestrator.providers.openai_provider import OpenAIProvider

    return OpenAIProvider()
