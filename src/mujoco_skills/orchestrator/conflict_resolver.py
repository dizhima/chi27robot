"""LLM adapter for one conflict-resolution round.

The user-facing chat transcript is deliberately not part of this adapter.
Every call is stateless and receives the complete, compiler-produced round
payload; the model's only job is to select from the bounded repair tools.
"""

from __future__ import annotations

import json

from mujoco_skills.orchestrator.conflict_payload import REPLAN_PATH_ENABLED
from mujoco_skills.orchestrator.schema import Message, ToolSpec


SYSTEM_PROMPT = """You are a bounded multi-robot scheduling repair strategist.
You do not author tasks and you do not invent geometry. Read the complete JSON
payload for this round and emit only tool calls that repair its listed
conflicts.

Rules:
- Use only a conflict's allowed_tools and only ids/robots present in the payload.
- Prefer a repair with the smallest likely makespan increase.
- Facility conflicts are resource clashes: use add_after or the advertised
  departure/handoff tool, never replan_path.
- A handoff_terminal_close changes robot program topology. If one is available,
  select it by itself; the engine will defer other calls and recompile before
  asking for temporal repairs in the next round.
- For add_after, copy an exact entry from add_after_candidates whenever that
  array is non-empty. Never reverse it. Otherwise make the blocked party wait
  for the OTHER party's explicit departure_step. Do not anchor on a dwell/place
  step. If that departure_step
  is null, first use insert_go_to for that party and use
  '<robot>#go_to_rest' as the departure anchor.
- For path conflicts, when replan_path is allowed, choose replan_path before
  add_after. For one_moving conflicts, replan only the moving party. Use
  add_after only when replan_path is unavailable, or when a prior reroute
  attempt failed or did not remove the conflict. On round 2 or later, prefer
  add_after for a conflict that is still present instead of repeating
  replan_path on the same mover.
- When insert_yield is allowed, the engine has already proven that neither
  priority ordering can move while the other robot waits. Copy one exact
  yield_candidate; do not select replan_path or add_after instead.
- insert_go_to is only for a dwelling party whose is_last_step is true. It may
  be paired in the same response with add_after using '<robot>#go_to_rest'.
- handoff_terminal_close may be used only by copying an exact entry from the
  conflict's handoff_candidates array.
- Do not address object or placement conflicts; they are reserved for humans.
- Do not explain your answer. If no safe repair exists, return no tool calls.
"""


RESOLVER_TOOLS = [
    ToolSpec(
        name="handoff_terminal_close",
        description=(
            "Move an eligible terminal close group to the robot that completed "
            "the last placement. Arguments must exactly match a payload "
            "handoff_candidate."
        ),
        parameters={
            "type": "object",
            "properties": {
                "conflict_id": {"type": "string"},
                "close_step": {"type": "string"},
                "to_robot": {"type": "string", "enum": ["robot0", "robot1"]},
            },
            "required": ["conflict_id", "close_step", "to_robot"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="add_after",
        description=(
            "Serialize one step after another. Copy a topology-safe payload "
            "add_after_candidate whenever one is provided."
        ),
        parameters={
            "type": "object",
            "properties": {
                "conflict_id": {"type": "string"},
                "step": {"type": "string"},
                "after_step": {"type": "string"},
            },
            "required": ["conflict_id", "step", "after_step"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="insert_yield",
        description=(
            "Insert a deterministic temporary parking move for one robot, "
            "let the winner pass, then resume the yielding move. Arguments "
            "must exactly match a server-generated yield candidate."
        ),
        parameters={
            "type": "object",
            "properties": {
                "conflict_id": {"type": "string"},
                "yielding_step": {"type": "string"},
                "winner_step": {"type": "string"},
            },
            "required": ["conflict_id", "yielding_step", "winner_step"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="replan_path",
        description=(
            "Request a deterministic priority reroute for one moving step. "
            "The engine, not the model, computes the route from the scene "
            "navgrid and the avoided robot's conflict-window occupancy."
        ),
        parameters={
            "type": "object",
            "properties": {
                "conflict_id": {"type": "string"},
                "mover": {"type": "string"},
                "avoid": {"type": "string"},
            },
            "required": ["conflict_id", "mover", "avoid"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name="insert_go_to",
        description=(
            "Retire a finished dwelling robot to its known rest point, creating "
            "the deterministic departure id '<robot>#go_to_rest'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "conflict_id": {"type": "string"},
                "robot": {"type": "string", "enum": ["robot0", "robot1"]},
            },
            "required": ["conflict_id", "robot"],
            "additionalProperties": False,
        },
    ),
]

if not REPLAN_PATH_ENABLED:
    RESOLVER_TOOLS = [
        tool for tool in RESOLVER_TOOLS if tool.name != "replan_path"
    ]


def propose_repairs(payload: dict, provider) -> list[dict]:
    """Make one stateless LLM decision for a compiler-produced round."""
    reply = provider.chat(
        [
            Message("system", SYSTEM_PROMPT),
            Message(
                "user",
                "Repair the delegable conflicts in this round:\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        ],
        RESOLVER_TOOLS,
    )
    calls = []
    for call in reply.tool_calls or []:
        arguments = dict(call.arguments or {})
        conflict_id = arguments.pop("conflict_id", None)
        calls.append({
            "tool": call.name,
            "conflict_id": conflict_id,
            "args": arguments,
        })
    return calls
