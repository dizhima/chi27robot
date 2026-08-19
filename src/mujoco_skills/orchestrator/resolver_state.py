"""Deterministic state and ordering primitives for Resolver V2."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from mujoco_skills.orchestrator.conflict_payload import DELEGABLE_KINDS


SESSION_ATTEMPT_CAP = 6
MAX_SESSIONS_PER_RUN = 3
# Derived safety ceiling, not an independent scheduling budget. Session
# admission and each admitted session's own attempt cap are authoritative.
GLOBAL_ATTEMPT_CAP = MAX_SESSIONS_PER_RUN * SESSION_ATTEMPT_CAP


def stable_fingerprint(conflict: dict) -> str:
    """Identity that survives detector id/window renumbering."""
    material = {
        "kind": conflict.get("kind"),
        "steps": sorted(str(value) for value in conflict.get("steps", [])),
        "robots": sorted(str(value) for value in conflict.get("robots", [])),
        "facility": (conflict.get("detail") or {}).get("facility"),
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return f"{material['kind']}:{digest}"


def session_id_for(conflict: dict, schedule: list[dict] | None = None) -> str:
    facility = (conflict.get("detail") or {}).get("facility")
    if not facility:
        by_id = {item.get("id"): item for item in schedule or []}
        group_facilities = {}
        for item in schedule or []:
            item_facility = item.get("facility")
            group = item.get("group") or item.get("id")
            if item_facility and group:
                group_facilities.setdefault(group, set()).add(item_facility)
        group_facility = {
            group: next(iter(values))
            for group, values in group_facilities.items()
            if len(values) == 1
        }
        facilities = {
            (
                by_id.get(step_id, {}).get("facility")
                or group_facility.get(by_id.get(step_id, {}).get("group"))
            )
            for step_id in conflict.get("steps", [])
        }
        facilities.discard(None)
        # A terminal final-dwell path conflict inherits the semantic facility
        # lineage of the involved close/place step.
        if len(facilities) == 1:
            facility = next(iter(facilities))
    if facility:
        return f"facility:{facility}"
    if conflict.get("kind") in ("object", "placement"):
        return f"human:{conflict.get('kind')}"
    groups = {}
    for item in schedule or []:
        groups[item.get("id")] = item.get("group") or item.get("id")
    robots = "+".join(sorted(str(r) for r in conflict.get("robots", [])))
    semantic = "+".join(sorted(str(groups.get(s, s)) for s in conflict.get("steps", [])))
    digest = hashlib.sha256(f"{robots}|{semantic}".encode()).hexdigest()[:12]
    return f"path:{robots}:{digest}"


def _severity_rank(conflict: dict) -> int:
    classification = conflict.get("class")
    if classification == "both_dwelling" and conflict.get("kind") == "facility":
        return 0
    return {"one_moving": 1, "both_moving": 2}.get(classification, 3)


@dataclass(frozen=True)
class FocusConflict:
    id: str
    fingerprint: str
    session_id: str
    conflict: dict
    order_key: tuple

    @property
    def start(self) -> float:
        window = self.conflict.get("window") or [float("inf"), float("inf")]
        return float(window[0])


@dataclass
class SessionLedger:
    session_id: str
    attempts_used: int = 0
    conflict_history: list = field(default_factory=list)
    tool_history: list = field(default_factory=list)
    failed_action_keys: set = field(default_factory=set)
    attempted_action_keys: set = field(default_factory=set)
    replan_keys: set = field(default_factory=set)
    departure_robot: str | None = None
    close_owner: str | None = None
    handoff_direction: tuple[str, str] | None = None
    resolved_until: float = 0.0
    status: str = "active"
    residual_reason: str | None = None

    @property
    def exhausted(self) -> bool:
        return self.attempts_used >= SESSION_ATTEMPT_CAP or self.status == "exhausted"


@dataclass
class ResolutionState:
    plan: dict
    compile_result: dict
    sessions: dict[str, SessionLedger] = field(default_factory=dict)
    queue: list[FocusConflict] = field(default_factory=list)
    human_conflicts: list[dict] = field(default_factory=list)
    rounds: list[dict] = field(default_factory=list)
    messages: list = field(default_factory=list)
    attempts_used: int = 0
    compile_count: int = 0
    initial_snapshot_reused: bool = False
    frozen_structure: dict = field(default_factory=dict)
    path_lineage: dict[str, str] = field(default_factory=dict)
    decision_failures: dict[str, int] = field(default_factory=dict)
    blocked_fingerprints: dict[str, str] = field(default_factory=dict)
    session_cap: int = MAX_SESSIONS_PER_RUN
    conflict_sessions: dict[str, str] = field(default_factory=dict)
    deferred_conflicts: dict[str, dict] = field(default_factory=dict)
    # S3 (compound_turn_integration_spec.md §4 items 11-13): the manual-edit/
    # allocation "protected set" for this run's focuses -- see
    # conflict_payload / resolver_v2's pin-veto matrix. Empty for every
    # pre-S3 caller, so this is a pure additive default.
    protected: dict = field(default_factory=dict)
    # Focuses whose only legal tools were removed SOLELY by a pin (item 13):
    # {"pin": "...", "conflicting_task": "..."} per entry. Surfaced verbatim
    # in the report so resolve_summary_text can name the pin instead of
    # blending it into a generic "unresolved" count.
    deferred_pins: list = field(default_factory=list)

    def rebuild_queue(self) -> None:
        self.human_conflicts = [
            c for c in self.compile_result.get("conflicts", [])
            if c.get("kind") in ("object", "placement")
        ]
        conflicts = [
            conflict for conflict in self.compile_result.get("conflicts", [])
            if conflict.get("kind") in DELEGABLE_KINDS
        ]
        session_assignments = self._path_session_assignments(conflicts)
        candidates = []
        self.conflict_sessions = {}
        self.deferred_conflicts = {}
        for conflict in conflicts:
            if conflict.get("kind") not in DELEGABLE_KINDS:
                continue
            fingerprint = stable_fingerprint(conflict)
            if fingerprint in self.blocked_fingerprints:
                continue
            session_id = session_assignments.get(fingerprint) or session_id_for(
                conflict, self.compile_result.get("schedule", []))
            self.conflict_sessions[fingerprint] = session_id
            window = conflict.get("window") or [float("inf"), float("inf")]
            candidates.append((
                (float(window[0]), _severity_rank(conflict), float(window[1]), fingerprint),
                fingerprint,
                session_id,
                conflict,
            ))
        candidates.sort(key=lambda item: item[0])
        admitted = []
        for key, fingerprint, session_id, conflict in candidates:
            ledger = self.sessions.get(session_id)
            if ledger is None:
                if len(self.sessions) >= self.session_cap:
                    self.deferred_conflicts[fingerprint] = {
                        "kind": conflict.get("kind"),
                        "steps": conflict.get("steps"),
                        "robots": conflict.get("robots"),
                        "window": conflict.get("window"),
                        "fingerprint": fingerprint,
                        "session_id": session_id,
                        "reason": "session_cap_reached",
                    }
                    continue
                ledger = SessionLedger(session_id)
                self.sessions[session_id] = ledger
            if ledger.exhausted:
                continue
            admitted.append((key, fingerprint, session_id, conflict))
        self.queue = [
            FocusConflict(f"c{index}", fingerprint, session_id, conflict, key)
            for index, (key, fingerprint, session_id, conflict) in enumerate(admitted)
        ]

    def _path_session_assignments(self, conflicts: list[dict]) -> dict[str, str]:
        schedule = self.compile_result.get("schedule", [])
        by_id = {item.get("id"): item for item in schedule}
        nodes = []
        for conflict in conflicts:
            if conflict.get("kind") != "path":
                continue
            inferred = session_id_for(conflict, schedule)
            if inferred.startswith("facility:"):
                continue
            pair = tuple(sorted(str(value) for value in conflict.get("robots", [])))
            groups = {
                str(by_id.get(step_id, {}).get("group") or step_id)
                for step_id in conflict.get("steps", [])
            }
            nodes.append({
                "conflict": conflict,
                "fingerprint": stable_fingerprint(conflict),
                "pair": pair,
                "groups": groups,
                "window": conflict.get("window") or [float("inf"), float("inf")],
            })
        parent = list(range(len(nodes)))

        def root(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left, right):
            left, right = root(left), root(right)
            if left != right:
                parent[right] = left

        for left in range(len(nodes)):
            for right in range(left + 1, len(nodes)):
                a, b = nodes[left], nodes[right]
                overlap = max(float(a["window"][0]), float(b["window"][0])) <= min(
                    float(a["window"][1]), float(b["window"][1]))
                if a["pair"] == b["pair"] and (overlap or a["groups"] & b["groups"]):
                    union(left, right)

        components = {}
        for index, node in enumerate(nodes):
            components.setdefault(root(index), []).append(node)
        assignments = {}
        for component in components.values():
            pair = component[0]["pair"]
            tokens = {
                f"group:{'+'.join(pair)}:{group}"
                for node in component for group in node["groups"]
            }
            tokens.update(f"fp:{node['fingerprint']}" for node in component)
            existing = sorted({
                self.path_lineage[token] for token in tokens
                if token in self.path_lineage
            })
            if existing:
                session_id = existing[0]
            else:
                groups = sorted(group for node in component for group in node["groups"])
                digest = hashlib.sha256(
                    ("|".join(pair) + "|" + "|".join(groups)).encode()
                ).hexdigest()[:12]
                session_id = f"path:{'+'.join(pair)}:{digest}"
            for token in tokens:
                self.path_lineage[token] = session_id
            for node in component:
                assignments[node["fingerprint"]] = session_id
        return assignments


def makespan(compile_result: dict) -> float:
    return max((
        float(item.get("start", 0.0)) + float(item.get("duration", 0.0))
        for item in compile_result.get("schedule", [])
    ), default=0.0)


def conflict_metric(conflict: dict | None) -> tuple:
    if conflict is None:
        return (-1.0, -float("inf"), -1)
    window = conflict.get("window") or [0.0, 0.0]
    duration = max(0.0, float(window[1]) - float(window[0]))
    detail = conflict.get("detail") or {}
    distance = detail.get("min_dist")
    # Smaller tuple is better: shorter overlap, then greater clearance.
    return (
        duration,
        -float(distance) if distance is not None else 0.0,
        _severity_rank(conflict),
    )


def find_conflict(compile_result: dict, fingerprint: str) -> dict | None:
    return next((
        conflict for conflict in compile_result.get("conflicts", [])
        if stable_fingerprint(conflict) == fingerprint
    ), None)


def step_index(plan: dict) -> dict[str, tuple[str, dict]]:
    return {
        str(step["id"]): (str(robot), step)
        for robot, steps in plan.items()
        for step in steps
    }


def step_structure(plan: dict, step_ids: set[str]) -> dict:
    """Authored structure protected by logical freezing (not compiler tracks)."""
    protected_keys = (
        "id", "op", "target", "object", "dest", "facility", "at",
        "via_points", "standoff", "after", "group", "robot_locked",
    )
    index = step_index(plan)
    return {
        step_id: {
            "robot": index[step_id][0],
            **{key: index[step_id][1].get(key) for key in protected_keys},
        }
        for step_id in step_ids if step_id in index
    }


def frozen_step_ids(compile_result: dict, causal_floor: float,
                    mutable_descendants: set[str] | None = None) -> set[str]:
    mutable_descendants = mutable_descendants or set()
    return {
        str(item["id"])
        for item in compile_result.get("schedule", [])
        if float(item.get("start", 0.0)) + float(item.get("duration", 0.0))
        <= causal_floor
        and str(item["id"]) not in mutable_descendants
    }
