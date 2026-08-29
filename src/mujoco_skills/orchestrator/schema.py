"""Provider-neutral tool schema for the co-authoring orchestrator.

A `ToolSpec` holds the parts every LLM provider needs — name, description, and a
plain JSON Schema for the arguments — with no provider-specific wrapping. Each
`to_<provider>` helper reshapes that into the wire format a given API expects;
the inner JSON Schema is reused verbatim (the providers only differ in the outer
keys). Tool definitions are authored once in service/llm_tools.json (currently in
OpenAI's format) and loaded via `load_tools`.

Only OpenAI is implemented today (that's the key we have). Claude/Gemini helpers
are sketched below for when a second provider is wired up — the real cost of
multi-provider support is the message/tool-result round-trip, not this file.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

# service/llm_tools.json lives next to skill_service.py; from this file that is
# ../service/llm_tools.json.
DEFAULT_TOOLS_PATH = Path(__file__).resolve().parents[1] / "service" / "llm_tools.json"


DEFAULT_ROBOT_IDS = ("robot0", "robot1")


def robot_ids_from_manifest(manifest: dict | None) -> tuple[str, ...]:
    """Return the manifest robot ids in stable scene order.

    Older unit fixtures and callers that predate the robot registry are kept
    compatible with the original two-robot scene.  Real scene manifests carry
    a non-empty ``robots`` mapping; its ``index`` field is the primary order
    and insertion order is the deterministic tie breaker.
    """
    robots = (manifest or {}).get("robots")
    if not isinstance(robots, dict) or not robots:
        return DEFAULT_ROBOT_IDS
    entries = []
    for position, (robot_id, descriptor) in enumerate(robots.items()):
        if not isinstance(robot_id, str) or not robot_id:
            raise ValueError("manifest robot ids must be non-empty strings")
        index = descriptor.get("index") if isinstance(descriptor, dict) else None
        entries.append((index if isinstance(index, int) else position, position, robot_id))
    entries.sort()
    return tuple(robot_id for _, _, robot_id in entries)


def schema_for_robot_ids(schema: dict, robot_ids: tuple[str, ...] | list[str]) -> dict:
    """Copy a JSON schema and bind every robot selector to this scene."""
    ids = list(robot_ids)
    if not ids:
        raise ValueError("at least one robot id is required")
    result = copy.deepcopy(schema)

    def bind(node):
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            for name in ("robot", "to_robot"):
                field = properties.get(name)
                if isinstance(field, dict) and field.get("type") == "string":
                    field["enum"] = ids
        for value in node.values():
            if isinstance(value, dict):
                bind(value)
            elif isinstance(value, list):
                for item in value:
                    bind(item)

    bind(result)
    return result


@dataclass(frozen=True)
class ToolSpec:
    """One tool the model may call, in provider-neutral form."""

    name: str
    description: str
    parameters: dict  # a plain JSON Schema object describing the arguments


@dataclass
class ToolCall:
    """A single tool invocation the model requested."""

    id: str
    name: str
    arguments: dict


@dataclass
class Message:
    """A provider-neutral conversation message.

    - role "assistant" with `tool_calls` set: the model wants tools run (continue)
    - role "assistant" with only `content`: the model is done (terminate)
    - role "tool": a tool result fed back, keyed by `tool_call_id`
    """

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None  # set when role == "tool"


@dataclass
class SemanticTask:
    """One grounded semantic task — stage 1's output, stage 2's input.

    A stable `id` (unique within one grounding call) lets the CLI confirmation
    step and stage 2's cross-robot `after` dependencies refer back to a task.
    """

    id: str
    action: str  # only "move" today; articulation actions may be added later
    object: str  # a name from manifest["objects"]
    dest: str    # a name from manifest["facilities"] whose `place` is non-null
    # Optional authoring-time ownership requested explicitly by the user.
    # Grounding-stage callers leave this unset.
    robot: str | None = None


@dataclass
class GroundResult:
    """The complete stage-1 state returned after one conversation turn."""

    tasks: list[SemanticTask]
    message: str
    reason: str | None


@dataclass
class AugmentedAction:
    """One ordered semantic action after task-level augmentation."""

    id: str
    robot: str
    op: str  # "move" | "open" | "close" | "go_to"
    object: str | None = None
    dest: str | None = None
    facility: str | None = None
    target: str | None = None
    via_points: list[list[float]] | None = None
    serves: str | None = None
    # True only when a human/user explicitly chose this robot. Automated
    # conflict repair must not hand the action to another robot.
    robot_locked: bool = False
    # Symbolic scene-ref pin handle (e.g. "p1") a `move` binds its destination
    # point to. The LLM emits only the handle, never coordinates; decompose
    # resolves it against THIS turn's scene_refs (see decompose.decompose).
    place_at_pin: str | None = None
    # Semantic predecessors, expressed only as AugmentedAction ids.  They are
    # expanded by decompose to first-step -> predecessor-last-step edges.
    after: list[str] | None = None


@dataclass
class AuthoringResult:
    """The semantic task plan returned by one authoring-loop turn."""

    actions: list[AugmentedAction]
    message: str
    reason: str | None


# JSON Schema for the structured output stage 1 is forced to produce (see
# orchestrator/stage1.py). `reason` is required whenever `tasks` is empty, so
# the model must explain what couldn't be grounded instead of silently giving
# up or inventing something.
SEMANTIC_TASKS_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["move"]},
                    "object": {"type": "string"},
                    "dest": {"type": "string"},
                },
                "required": ["action", "object", "dest"],
                "additionalProperties": False,
            },
        },
        "reason": {
            "type": ["string", "null"],
            "description": "Required (non-null) when tasks is empty: what could "
            "not be grounded and what IS available instead.",
        },
        "message": {
            "type": "string",
            "description": "One concise sentence describing what changed this turn.",
        },
    },
    "required": ["tasks", "message"],
    "additionalProperties": False,
}


AUGMENTED_ACTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "robot": {"type": "string"},
                    "op": {
                        "type": "string",
                        "enum": ["move", "open", "close", "go_to"],
                    },
                    "object": {"type": ["string", "null"]},
                    "dest": {"type": ["string", "null"]},
                    "facility": {"type": ["string", "null"]},
                    "target": {"type": ["string", "null"]},
                    "via_points": {
                        "type": ["array", "null"],
                        "items": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                    },
                    "serves": {"type": ["string", "null"]},
                    "robot_locked": {"type": "boolean"},
                    "place_at_pin": {
                        "type": ["string", "null"],
                        "description": (
                            "Only for op=move: a scene_refs pin handle (e.g. "
                            "\"p1\") the destination point binds to. Never a "
                            "coordinate."
                        ),
                    },
                    "after": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": "Semantic action ids that must finish first.",
                    },
                },
                "required": [
                    "id",
                    "robot",
                    "op",
                    "object",
                    "dest",
                    "facility",
                    "target",
                    "via_points",
                    "serves",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["actions"],
    "additionalProperties": False,
}


AUGMENT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Stable intent id, unique in the plan.",
                    },
                    "action": {"type": "string", "enum": ["move"]},
                    "object": {"type": "string"},
                    "dest": {"type": "string"},
                    "robot": {
                        "type": "string",
                        "description": (
                            "Optional explicit robot owner for this new move. "
                            "Moves sharing a source or destination inherit the "
                            "same owner when there is no conflicting explicit "
                            "assignment."
                        ),
                    },
                },
                "required": ["id", "action", "object", "dest"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["actions"],
    "additionalProperties": False,
}


REASSIGN_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "action_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Ids of the exact semantic actions to reassign. Include every "
                "move/open/close id only when the user asks to move the whole "
                "destination workflow."
            ),
        },
        "robot": {"type": "string"},
        "after_action_id": {
            "type": ["string", "null"],
            "description": (
                "Optional exact insertion slot on the destination robot. A string "
                "places the reassigned actions immediately after that action; null "
                "places them first. Omit this field to let the server choose a "
                "topology-safe slot deterministically."
            ),
        },
    },
    "required": ["action_ids", "robot"],
    "additionalProperties": False,
}


UPDATE_MOVE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "action_id": {
            "type": "string",
            "description": "Stable id of the existing op=move action to update.",
        },
        "object": {
            "type": "string",
            "description": (
                "Replacement object name from the manifest. Omit to preserve "
                "the move's current object."
            ),
        },
        "dest": {
            "type": "string",
            "description": (
                "Replacement placeable facility name from the manifest. Omit "
                "to preserve the move's current destination."
            ),
        },
    },
    "required": ["action_id"],
    "additionalProperties": False,
}


REMOVE_TASK_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "task_ids": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string"},
            "description": (
                "Stable ids of existing semantic move tasks to remove atomically. "
                "Generated open/close envelope actions are cleaned up by the server "
                "when their last move is removed."
            ),
        },
    },
    "required": ["task_ids"],
    "additionalProperties": False,
}


REVISE_ORDER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "operations": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["place_relative", "set_sequence"],
                    },
                    "task_id": {"type": "string"},
                    "anchor_id": {"type": "string"},
                    "relation": {
                        "type": "string",
                        "enum": ["before", "after", "immediately_before", "immediately_after"],
                        "description": (
                            "immediately_before/after selects the adjacent insertion slot "
                            "on the same robot; use it for add/insert X before/after an "
                            "anchor. before/after expresses precedence without adjacency "
                            "and becomes a dependency when the actions are on different robots."
                        ),
                    },
                    "task_ids": {
                        "type": "array",
                        "minItems": 2,
                        "items": {"type": "string"},
                    },
                },
                "required": ["type"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["operations"],
    "additionalProperties": False,
}


SET_PLACE_PIN_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "action_id": {
            "type": "string",
            "description": "Id of an existing op=move action to bind or clear a pin on.",
        },
        "pin": {
            "type": ["string", "null"],
            "description": (
                "A scene_refs pin handle (e.g. \"p1\") whose bound facility "
                "matches the action's dest, or null to clear the action's "
                "existing pin. Never a coordinate."
            ),
        },
    },
    "required": ["action_id", "pin"],
    "additionalProperties": False,
}


PROPOSE_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["committed", "ungroundable"],
            "description": (
                "\"committed\": finish the turn with the working plan exactly "
                "as built by augment/remove_task/update_move/reassign/revise_order/"
                "set_place_pin. "
                "\"ungroundable\": nothing in this turn's request could be "
                "grounded; the working plan is left completely unchanged."
            ),
        },
        "message": {
            "type": "string",
            "description": "One concise sentence summarizing the latest change.",
        },
        "reason": {
            "type": ["string", "null"],
            "description": (
                "Must be null when status is \"committed\". Required and "
                "non-null when status is \"ungroundable\": explain what could "
                "not be grounded and what IS available instead."
            ),
        },
    },
    "required": ["status", "message", "reason"],
    "additionalProperties": False,
}


def to_openai(tool: ToolSpec) -> dict:
    """OpenAI chat.completions `tools=[...]` entry."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def to_claude(tool: ToolSpec) -> dict:
    """Anthropic Messages `tools=[...]` entry. (Untested — no key yet.)"""
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
    }


def to_gemini(tool: ToolSpec) -> dict:
    """Gemini function declaration; wrap a list of these in
    {"functionDeclarations": [...]} at the call site. (Untested — no key yet.)"""
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
    }


def load_tools(
    path: str | Path = DEFAULT_TOOLS_PATH,
    *,
    manifest: dict | None = None,
) -> list[ToolSpec]:
    """Load ToolSpecs and optionally bind selectors to manifest robots."""
    data = json.loads(Path(path).read_text("utf-8"))
    specs: list[ToolSpec] = []
    for entry in data.get("tools", []):
        fn = entry["function"]  # entries are {"type":"function","function":{...}}
        parameters = fn.get("parameters", {"type": "object", "properties": {}})
        if manifest is not None:
            parameters = schema_for_robot_ids(
                parameters, robot_ids_from_manifest(manifest))
        specs.append(
            ToolSpec(
                name=fn["name"],
                description=fn.get("description", ""),
                parameters=parameters,
            )
        )
    return specs
