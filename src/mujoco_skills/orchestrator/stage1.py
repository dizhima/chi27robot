"""Stage 1: multi-turn natural language -> complete grounded semantic tasks."""

from __future__ import annotations

import json
from collections.abc import Callable

from mujoco_skills.orchestrator.prompt import STAGE1_PROMPT
from mujoco_skills.orchestrator.providers.base import LLMProvider
from mujoco_skills.orchestrator.schema import (
    GroundResult,
    Message,
    SemanticTask,
    SEMANTIC_TASKS_SCHEMA,
    ToolSpec,
)

PROPOSE_TASKS_TOOL = ToolSpec(
    name="propose_semantic_tasks",
    description=(
        "Report the grounded semantic tasks for the user's request, or an "
        "empty tasks list plus a `reason` if the request cannot be grounded "
        "in the given objects/facilities (no matching object, or the "
        "destination has no place capability)."
    ),
    parameters=SEMANTIC_TASKS_SCHEMA,
)


def _manifest_context(manifest: dict) -> str:
    """A compact objects/facilities view for the stage-1 prompt. Small enough
    (a handful of entries for this scene) to embed directly rather than
    round-trip through a retrieval tool — that round-trip is exactly what
    design option B would add inside this function later."""
    objects = {
        name: {"label": o.get("label"), "home_facility": o.get("home_facility")}
        for name, o in manifest.get("objects", {}).items()
    }
    facilities = {
        name: {
            "label": f.get("label"),
            "can_navigate": f.get("standoff") is not None,
            "can_place": f.get("place") is not None,
            "can_articulate": f.get("articulation") is not None,
        }
        for name, f in manifest.get("facilities", {}).items()
    }
    return json.dumps({"objects": objects, "facilities": facilities}, ensure_ascii=False)


def ground(
    messages: list[dict],
    current_tasks: list[SemanticTask],
    provider: LLMProvider,
    manifest: dict,
    *,
    on_event: Callable[[str, object], None] | None = None,
) -> GroundResult:
    """Apply one conversation turn to the UI's authoritative task list."""
    context = _manifest_context(manifest)
    tasks_context = json.dumps(
        [
            {"id": t.id, "action": t.action, "object": t.object, "dest": t.dest}
            for t in current_tasks
        ],
        ensure_ascii=False,
    )
    msgs = [
        Message("system", STAGE1_PROMPT),
        Message(
            "user",
            "Structured context (not a user request):\n"
            f"scene_vocabulary={context}\n"
            f"current_tasks={tasks_context}",
        ),
        *[
            Message(m["role"], str(m.get("content", "")))
            for m in messages
            if m.get("role") in {"user", "assistant"}
        ],
        Message(
            "system",
            "FINAL STATE SYNC: the UI's authoritative current_tasks is "
            f"{tasks_context}. Treat any task absent from this list as manually "
            "deleted by the user. Do not restore an absent task from conversation "
            "history unless the latest user message explicitly asks for it again. "
            "Apply only the latest user message to this exact state, then return "
            "the complete resulting list.",
        ),
    ]
    # Only one tool is offered, so a model that calls any tool at all calls
    # this one; a model that ignores the "always call it" instruction and
    # answers in prose is handled below rather than crashing.
    force_tool = getattr(provider, "force_tool", None)
    if callable(force_tool):
        reply = force_tool(msgs, [PROPOSE_TASKS_TOOL], "propose_semantic_tasks")
    else:
        reply = provider.chat(msgs, [PROPOSE_TASKS_TOOL])

    call = None
    if reply.tool_calls:
        call = next((c for c in reply.tool_calls if c.name == "propose_semantic_tasks"), None)

    if call is None:
        reason = reply.content or "(model did not propose any tasks)"
        if on_event:
            on_event("no_tasks_reason", reason)
        return GroundResult(tasks=[], message=reason, reason=reason)

    raw_tasks = call.arguments.get("tasks") or []
    message = str(call.arguments.get("message") or "").strip()
    if not raw_tasks:
        reason = str(call.arguments.get("reason") or "(no reason given)")
        if on_event:
            on_event("no_tasks_reason", reason)
        return GroundResult(tasks=[], message=message or reason, reason=reason)

    tasks = _with_stable_ids(raw_tasks, current_tasks)
    return GroundResult(
        tasks=tasks,
        message=message or f"Updated the semantic task list ({len(tasks)} tasks).",
        reason=None,
    )


def _with_stable_ids(raw_tasks: list[dict], current_tasks: list[SemanticTask]) -> list[SemanticTask]:
    """Keep IDs across full-list turns without asking the model to manage them."""
    unused = list(current_tasks)
    used_ids: set[str] = set()
    next_id = 0
    result: list[SemanticTask] = []

    def allocate() -> str:
        nonlocal next_id
        while f"t{next_id}" in used_ids or any(t.id == f"t{next_id}" for t in current_tasks):
            next_id += 1
        task_id = f"t{next_id}"
        next_id += 1
        return task_id

    for raw in raw_tasks:
        exact = next(
            (
                t for t in unused
                if (t.action, t.object, t.dest)
                == (raw["action"], raw["object"], raw["dest"])
            ),
            None,
        )
        same_object = next(
            (
                t for t in unused
                if (t.action, t.object) == (raw["action"], raw["object"])
            ),
            None,
        )
        matched = exact or same_object
        if matched:
            unused.remove(matched)
            task_id = matched.id
        else:
            task_id = allocate()
        used_ids.add(task_id)
        result.append(
            SemanticTask(
                id=task_id,
                action=raw["action"],
                object=raw["object"],
                dest=raw["dest"],
            )
        )
    return result
