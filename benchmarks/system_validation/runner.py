"""Run system-validation cases through the production compound-turn core.

The runner owns benchmark concerns (case loading, fixed base fixtures, scoring,
and JSONL persistence). Plan mutation remains in ``stream_compound_turn`` and
``plan_edits.replay_edits`` so the benchmark cannot drift into a second
implementation of local revision.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import statistics
import subprocess
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from mujoco_skills.eligibility import (
    AssignmentEligibilityError,
    format_eligibility_rejection,
    require_plan_assignments,
    validate_semantic_action,
)
from mujoco_skills.orchestrator import authoring, conversation, decompose
from mujoco_skills.orchestrator.providers.openai_provider import OpenAIProvider
from mujoco_skills.orchestrator.service import (
    TopologyValidationError,
    _compile_via_skill_service,
    _nest_resolved_plan,
    _validate_via_skill_service,
)
from mujoco_skills.skills import skill_generators as sg


REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_ROOT = Path(__file__).resolve().parent / "cases"
RESULTS_ROOT = Path(__file__).resolve().parent / "results"
VALID_EDIT_OPS = {
    "remove_task",
    "move_task",
    "set_task_robot",
    "set_step_after",
    "set_step_at",
    "set_step_via",
    "set_step_standoff",
}


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top-level value must be an object")
    return value


def _repo_path(value: str) -> Path:
    path = (REPO_ROOT / value).resolve()
    if REPO_ROOT not in path.parents and path != REPO_ROOT:
        raise ValueError(f"path escapes repository: {value}")
    return path


def discover_cases() -> list[Path]:
    return sorted(CASES_ROOT.glob("*/*.json"))


def select_cases(case_ids: list[str], scenes: list[str], all_scenes: bool) -> list[Path]:
    paths = discover_cases()
    if case_ids:
        wanted = set(case_ids)
        paths = [path for path in paths if path.stem in wanted]
        missing = wanted - {path.stem for path in paths}
        if missing:
            raise ValueError(f"unknown case set(s): {', '.join(sorted(missing))}")
    elif scenes:
        wanted = set(scenes)
        paths = [path for path in paths if path.parent.name in wanted]
        missing = wanted - {path.parent.name for path in paths}
        if missing:
            raise ValueError(f"unknown scene(s): {', '.join(sorted(missing))}")
    elif not all_scenes:
        raise ValueError("select --case, --scene, or --all-scenes")
    return paths


def _base_plan(case: dict, manifest: dict) -> dict:
    actions = authoring.parse_actions(case["revision_base"]["actions"], manifest)
    return decompose.decompose(actions, manifest, [])


def _task_index(plan: dict) -> tuple[dict[str, dict], dict[str, tuple[dict, dict]]]:
    tasks: dict[str, dict] = {}
    steps: dict[str, tuple[dict, dict]] = {}
    for task in plan.get("tasks", []):
        task_id = task.get("task")
        if isinstance(task_id, str):
            tasks[task_id] = task
        for step in task.get("steps", []):
            step_id = step.get("id")
            if isinstance(step_id, str):
                steps[step_id] = (task, step)
    return tasks, steps


def validate_case(path: Path, case: dict) -> tuple[dict, dict]:
    errors: list[str] = []
    if case.get("schema_version") != 2:
        errors.append("schema_version must be 2")
    if case.get("case_set_id") != path.stem:
        errors.append("case_set_id must match the filename")
    scene = case.get("scene") or {}
    for field in ("xml", "manifest"):
        value = scene.get(field)
        if not isinstance(value, str) or not _repo_path(value).is_file():
            errors.append(f"scene.{field} does not identify an existing file")
    variants = (case.get("initial") or {}).get("prompt_variants") or []
    if not variants:
        errors.append("initial.prompt_variants must not be empty")
    variant_ids = [variant.get("id") for variant in variants]
    if len(variant_ids) != len(set(variant_ids)):
        errors.append("initial prompt variant ids must be unique")
    revision_ids = [revision.get("id") for revision in case.get("revisions", [])]
    if len(revision_ids) != len(set(revision_ids)):
        errors.append("revision ids must be unique")
    if errors:
        raise ValueError(f"{path}: " + "; ".join(errors))

    manifest = _read_json(_repo_path(scene["manifest"]))
    try:
        plan = _base_plan(case, manifest)
    except Exception as exc:  # noqa: BLE001 - report case context
        raise ValueError(f"{path}: invalid revision_base: {exc}") from exc
    tasks, steps = _task_index(plan)
    for revision in case.get("revisions", []):
        turn = revision.get("turn") or {}
        channel = turn.get("channel")
        if channel not in {"conversation", "timeline"}:
            errors.append(f"{revision.get('id')}: unsupported channel {channel!r}")
            continue
        if channel == "timeline":
            if turn.get("intent_hint") != "edit":
                errors.append(f"{revision.get('id')}: timeline intent_hint must be 'edit'")
            edits = turn.get("edits")
            if not isinstance(edits, list) or not edits:
                errors.append(f"{revision.get('id')}: timeline edits must not be empty")
                continue
            for edit in edits:
                op = edit.get("op")
                target = edit.get("target") or {}
                if op not in VALID_EDIT_OPS:
                    errors.append(f"{revision.get('id')}: invalid edit op {op!r}")
                action_id = target.get("actionId")
                step_id = target.get("stepId")
                if action_id and action_id not in tasks:
                    errors.append(f"{revision.get('id')}: unknown action target {action_id!r}")
                if step_id and step_id not in steps:
                    errors.append(f"{revision.get('id')}: unknown step target {step_id!r}")
                after_action = edit.get("afterActionId")
                if after_action and after_action not in tasks:
                    errors.append(f"{revision.get('id')}: unknown afterActionId {after_action!r}")
                for after_step in edit.get("after", []):
                    if after_step not in steps:
                        errors.append(f"{revision.get('id')}: unknown after step {after_step!r}")
        elif not turn.get("messages"):
            errors.append(f"{revision.get('id')}: conversation messages must not be empty")
    if errors:
        raise ValueError(f"{path}: " + "; ".join(errors))
    return manifest, plan


def build_protected_set(edits: list[dict], plan: dict) -> dict:
    """Python mirror of the frontend's buildProtectedSet for benchmark requests."""
    tasks, steps = _task_index(plan)
    group_of = {
        step_id: step.get("group") or task.get("task") or step_id
        for step_id, (task, step) in steps.items()
    }
    out = {
        "allocations": [],
        "orderings": [],
        "exact_after_edges": [],
        "exact_after_fields": [],
        "destinations": [],
        "waypoints": [],
    }
    final_moves = {
        edit.get("target", {}).get("actionId"): edit
        for edit in edits if edit.get("op") == "move_task"
    }
    final_after: dict[str, dict] = {}
    for edit in edits:
        if edit.get("op") != "set_step_after":
            continue
        target = edit.get("target") or {}
        key = f"step:{target['stepId']}" if target.get("stepId") else (
            f"group:{target['group']}" if target.get("group") else None
        )
        if key:
            final_after[key] = edit
    for edit in edits:
        op = edit.get("op")
        target = edit.get("target") or {}
        if op == "move_task":
            action_id = target.get("actionId")
            if final_moves.get(action_id) is not edit or action_id not in tasks:
                continue
            if tasks[action_id].get("robot") != edit.get("robot"):
                out["allocations"].append({"group": action_id, "robot": edit["robot"]})
            after_action = edit.get("afterActionId")
            if after_action and after_action != action_id:
                out["orderings"].append({"before": after_action, "after": action_id})
        elif op == "set_task_robot":
            group = target.get("actionId") or target.get("group")
            if group:
                out["allocations"].append({"group": group, "robot": edit["robot"]})
        elif op == "set_step_after":
            step_id = target.get("stepId")
            key = f"step:{step_id}" if step_id else (
                f"group:{target['group']}" if target.get("group") else None
            )
            if key and final_after.get(key) is not edit:
                continue
            step_group = group_of.get(step_id) if step_id else target.get("group")
            if not step_group:
                continue
            if step_id:
                out["exact_after_fields"].append({"step": step_id, "after": list(edit["after"])})
            for after_step in edit.get("after", []):
                after_group = group_of.get(after_step)
                if step_id and after_step in group_of and after_step != step_id:
                    out["exact_after_edges"].append({"step": step_id, "after_step": after_step})
                if after_group and after_group != step_group:
                    out["orderings"].append({"before": after_group, "after": step_group})
        elif op == "set_step_at" and target.get("stepId"):
            out["destinations"].append({"step": target["stepId"]})
        elif op == "set_step_via" and target.get("stepId"):
            out["waypoints"].append({"step": target["stepId"]})
    return out


def semantic_compile(plan: dict, *, retain_snapshot: bool = False, progress_callback=None) -> dict:
    """Fast smoke-test compiler; compound editing remains production code."""
    if progress_callback:
        progress_callback(1, 1)
    return {
        "schedule": [],
        "warnings": [],
        "conflicts": [],
        "completed": copy.deepcopy(plan),
        "compile_id": f"semantic-{uuid.uuid4().hex}" if retain_snapshot else None,
    }


def local_topology_check(manifest: dict, manifest_path: Path) -> Callable[[dict], None]:
    def check(plan: dict) -> None:
        try:
            require_plan_assignments(manifest, plan)
        except AssignmentEligibilityError as exc:
            # Match /validate_plan's wire-to-core adapter so semantic smoke
            # runs exercise the same normal no-change rejection path as the
            # production skill service.
            raise TopologyValidationError(exc.as_dict()) from exc
        sg.validate_plan_topology(plan, manifest_path)
    return check


def _materialize_base(
    base_plan: dict,
    compile_fn: Callable[..., dict],
) -> tuple[dict, dict]:
    compiled = compile_fn(copy.deepcopy(base_plan), retain_snapshot=True)
    completed = compiled.get("completed")
    if isinstance(completed, dict) and isinstance(completed.get("tasks"), list):
        resolved = completed
    elif isinstance(completed, dict):
        resolved = _nest_resolved_plan(completed)
    else:
        resolved = base_plan
    return resolved, compiled


def _semantic_actions(raw: list[dict] | None, manifest: dict) -> list[dict]:
    if not isinstance(raw, list):
        return []
    return [asdict(action) for action in authoring.parse_actions(raw, manifest)]


def _dependency_reachable(
    actions: list[dict], action_id: str | None, predecessor_id: str | None
) -> bool:
    """Check semantic precedence, including implicit per-robot program order."""
    if not action_id or not predecessor_id:
        return False
    graph: dict[str, set[str]] = {}
    previous_by_robot: dict[str, str] = {}
    for action in actions:
        current_id = action.get("id")
        robot = action.get("robot")
        if not isinstance(current_id, str):
            continue
        predecessors = {
            item for item in (action.get("after") or [])
            if isinstance(item, str)
        }
        if isinstance(robot, str) and robot in previous_by_robot:
            predecessors.add(previous_by_robot[robot])
        graph[current_id] = predecessors
        if isinstance(robot, str):
            previous_by_robot[robot] = current_id

    pending = list(graph.get(action_id, ()))
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current == predecessor_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        pending.extend(graph.get(current, ()))
    return False


def _score_initial(artifact: dict | None, expected: dict, manifest: dict) -> dict:
    actions = _semantic_actions((artifact or {}).get("actions"), manifest)
    moves = [action for action in actions if action.get("op") == "move"]
    required = expected.get("required_moves", [])
    required_ok = all(any(
        action.get("object") == item.get("object")
        and action.get("dest") == item.get("dest")
        and (item.get("robot") is None or action.get("robot") == item.get("robot"))
        for action in moves
    ) for item in required)
    no_extra = True
    if expected.get("allow_additional_user_moves") is False:
        wanted = {(item.get("object"), item.get("dest")) for item in required}
        no_extra = all((action.get("object"), action.get("dest")) in wanted for action in moves)
    required_robots = set((expected.get("assignment") or {}).get("required_robots", []))
    robots_ok = not required_robots or required_robots <= {action.get("robot") for action in moves}
    facility_ok = True
    for facility, state in expected.get("required_final_facility_states", {}).items():
        facility_ops = [a.get("op") for a in actions if a.get("facility") == facility]
        desired = "close" if state == "closed" else "open"
        facility_ok = facility_ok and bool(facility_ops) and facility_ops[-1] == desired
    plan = (artifact or {}).get("plan")
    robot_sequences: dict[str, list[str]] = {}
    if isinstance(plan, dict):
        for task in plan.get("tasks", []):
            robot_sequences.setdefault(task.get("robot"), []).append(task.get("task"))

    def _within_robot_ordered(before: str, after: str) -> bool:
        for sequence in robot_sequences.values():
            if before in sequence and after in sequence:
                return sequence.index(before) < sequence.index(after)
        return False

    orderings_ok = all(
        _within_robot_ordered(pair["before"], pair["after"])
        for pair in expected.get("required_orderings", [])
    )
    dependencies_ok = all(
        _dependency_reachable(
            actions,
            dependency.get("action_id"),
            dependency.get("after_action_id"),
        )
        for dependency in expected.get("required_dependencies", [])
    )
    checks = {
        "turn_result": (artifact or {}).get("kind") == "turn_result",
        "required_moves": required_ok,
        "required_robots": robots_ok,
        "no_additional_moves": no_extra,
        "final_facility_states": facility_ok,
        "required_orderings": orderings_ok,
        "required_dependencies": dependencies_ok,
    }
    return {"success": all(checks.values()), "checks": checks}


def _score_revision(
    artifact: dict | None,
    revision: dict,
    base_actions: list[dict],
    manifest: dict,
) -> dict:
    artifact = artifact or {}
    expected = revision["expected"]
    if expected.get("outcome") == "rejected":
        rejection_expected = expected["rejection"]
        rejection_actual = artifact.get("rejection") or {}
        probe = rejection_expected.get("probe")
        derived = validate_semantic_action(manifest, probe) if isinstance(probe, dict) else {}
        actual_reason_code = rejection_actual.get("reason_code") or derived.get("code")
        deterministic_reason = (
            format_eligibility_rejection(derived, manifest)
            if derived and not derived.get("eligible", True)
            else None
        )
        artifact_reason = artifact.get("reason") or artifact.get("author_message")
        checks = {
            "no_change_result": artifact.get("kind") == "no_change_result",
            "reason_code": actual_reason_code == rejection_expected.get("reason_code"),
            "deterministic_reason": (
                deterministic_reason is None or artifact_reason == deterministic_reason
            ),
            "plan_unchanged": _semantic_actions(artifact.get("actions"), manifest) == base_actions,
            "compiler_not_run": "compiling" not in artifact.get("stages", []),
        }
        return {"success": all(checks.values()), "checks": checks}

    change_list = expected.get("changes") or [expected["change"]]
    actions = _semantic_actions(artifact.get("actions"), manifest)
    action_map = {action["id"]: action for action in actions}
    tasks, steps = _task_index(artifact.get("plan") or {})
    changed = all(
        _change_applied(change, artifact, actions, action_map, tasks, steps, revision)
        for change in change_list
    )

    base_user_ids = {action["id"] for action in base_actions if action.get("op") == "move"}
    for change in change_list:
        if change["type"] == "remove":
            base_user_ids.discard(change["action_id"])
    if revision["turn"]["channel"] == "conversation":
        preserved = base_user_ids <= set(action_map)
    else:
        preserved = base_user_ids <= set(tasks)
    base_action_map = {action["id"]: action for action in base_actions}
    facility_assignments_preserved = all(
        action_id in action_map
        and action_id in base_action_map
        and action_map[action_id].get("robot")
        == base_action_map[action_id].get("robot")
        for action_id in expected.get("preserve_facility_assignments", [])
    )
    required_dependencies = all(
        _dependency_reachable(
            actions,
            dependency.get("action_id"),
            dependency.get("after_action_id"),
        )
        for dependency in expected.get("required_dependencies", [])
    )
    absent_ok = all(
        action_id not in action_map
        for action_id in expected.get("required_absent_action_ids", [])
    )
    checks = {
        "turn_result": artifact.get("kind") == "turn_result",
        "requested_change": changed,
        "unmentioned_user_tasks_preserved": preserved,
        "unmentioned_facility_assignments_preserved": facility_assignments_preserved,
        "required_semantic_dependencies": required_dependencies,
        "required_absent_actions": absent_ok,
        "no_dropped_edits": not artifact.get("dropped_edits"),
    }
    return {"success": all(checks.values()), "checks": checks}


def _change_applied(
    change: dict,
    artifact: dict,
    actions: list[dict],
    action_map: dict[str, dict],
    tasks: dict[str, dict],
    steps: dict[str, tuple[dict, dict]],
    revision: dict,
) -> bool:
    change_type = change["type"]
    changed = False
    if change_type == "add":
        wanted = change["action"]
        matches = [
            action
            for action in actions
            if all(action.get(key) == value for key, value in wanted.items())
        ]
        changed = bool(matches)
        after_id = change.get("after_action_id")
        if changed and after_id:
            added_id = matches[0].get("id")
            added_robot = matches[0].get("robot")
            ordered = [
                task.get("task")
                for task in (artifact.get("plan") or {}).get("tasks", [])
                if task.get("robot") == added_robot
            ]
            changed = added_id in ordered and after_id in ordered
            if changed and change.get("immediately_after"):
                changed = ordered.index(added_id) == ordered.index(after_id) + 1
            elif changed:
                changed = ordered.index(added_id) > ordered.index(after_id)
    elif change_type == "remove":
        changed = change["action_id"] not in action_map
    elif change_type == "retarget":
        changed = action_map.get(change["action_id"], {}).get("dest") == change["dest"]
    elif change_type == "reassign":
        task = tasks.get(change["action_id"], {})
        changed = task.get("robot") == change["robot"]
        after_id = change.get("after_action_id")
        if changed and after_id:
            ordered = [task.get("task") for task in (artifact.get("plan") or {}).get("tasks", [])
                       if task.get("robot") == change["robot"]]
            changed = change["action_id"] in ordered and after_id in ordered and (
                ordered.index(change["action_id"]) == ordered.index(after_id) + 1
            )
    elif change_type == "reorder":
        ordered = [task.get("task") for task in (artifact.get("plan") or {}).get("tasks", [])]
        before, after = change["before_action_id"], change["after_action_id"]
        changed = before in ordered and after in ordered and ordered.index(before) < ordered.index(after)
        if changed and change.get("immediately_before"):
            changed = ordered.index(before) + 1 == ordered.index(after)
    elif change_type == "precedence":
        edits = revision["turn"].get("edits", [])
        edge = next((edit for edit in edits if edit.get("op") == "set_step_after"), None)
        if edge:
            target = steps.get(edge["target"]["stepId"], ({}, {}))[1]
            changed = target.get("after", []) == edge.get("after", [])
        else:
            # A minimal barrier on the predecessor robot's last task is enough
            # to constrain every earlier task on that robot.  Score semantic
            # reachability rather than requiring redundant direct edges.
            changed = _dependency_reachable(
                actions,
                change.get("target_action_id"),
                change.get("after_action_id"),
            )
    return changed


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _hash_json(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _run_turn(
    *, body: dict, manifest: dict, provider: Any, compile_fn: Callable[..., dict],
    topology_check_fn: Callable[[dict], None],
) -> tuple[list[dict], dict | None, float]:
    events: list[dict] = []
    started = time.perf_counter()
    conversation.stream_compound_turn(
        body,
        lambda event: events.append(copy.deepcopy(event)),
        provider=provider,
        manifest=manifest,
        compile_fn=compile_fn,
        topology_check_fn=topology_check_fn,
    )
    latency_ms = (time.perf_counter() - started) * 1000.0
    artifact = next(
        (event.get("artifact") for event in reversed(events) if event.get("type") == "result"),
        None,
    )
    return events, artifact, latency_ms


def run_case(
    path: Path,
    *, kind: str,
    variant_filter: str | None,
    revision_filter: str | None,
    repeats: int,
    model: str,
    compile_mode: str,
    output_path: Path,
) -> list[dict]:
    case = _read_json(path)
    manifest, authored_base_plan = validate_case(path, case)
    manifest_path = _repo_path(case["scene"]["manifest"])
    if compile_mode == "service":
        compile_fn = _compile_via_skill_service
        topology_check_fn = _validate_via_skill_service
    else:
        compile_fn = semantic_compile
        topology_check_fn = local_topology_check(manifest, manifest_path)
    resolved_base_plan, base_compile = _materialize_base(authored_base_plan, compile_fn)
    base_actions = _semantic_actions(case["revision_base"]["actions"], manifest)
    records: list[dict] = []

    units: list[tuple[str, str, dict, dict]] = []
    if kind in {"all", "initial"}:
        for variant in case["initial"]["prompt_variants"]:
            if variant_filter is None or variant["id"] == variant_filter:
                units.append(("initial", variant["id"], variant, case["initial"]["expected"]))
    if kind in {"all", "revision"}:
        for revision in case.get("revisions", []):
            if revision_filter is None or revision["id"] == revision_filter:
                units.append(("revision", revision["id"], revision, revision["expected"]))
    if not units:
        raise ValueError(f"{case['case_set_id']}: filters selected no benchmark units")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    for unit_kind, unit_id, payload, expected in units:
        for repeat in range(1, repeats + 1):
            turn = payload if unit_kind == "initial" else payload["turn"]
            channel = turn.get("channel", "conversation")
            if unit_kind == "initial":
                current_actions, previous_plan, edits = [], None, []
            else:
                current_actions = copy.deepcopy(case["revision_base"]["actions"])
                previous_plan = copy.deepcopy(resolved_base_plan)
                edits = copy.deepcopy(turn.get("edits", []))
            body = {
                "turn_id": f"bench-{case['case_set_id']}-{unit_id}-{repeat}-{uuid.uuid4().hex[:8]}",
                "intent_hint": turn.get("intent_hint"),
                "messages": copy.deepcopy(turn.get("messages", [])),
                "current_actions": current_actions,
                "plan_state": {
                    "status": "completed" if previous_plan else "empty",
                    "dirty": False,
                    "in_sync": bool(previous_plan),
                    "delegable_conflict_count": 0,
                    "completed_plan": previous_plan,
                    "compile_id": base_compile.get("compile_id") if previous_plan else None,
                },
                "scene_refs": copy.deepcopy(turn.get("scene_refs", [])),
                "plan_refs": copy.deepcopy(turn.get("plan_refs", [])),
                "previous_plan": previous_plan,
                "base_revision": 0,
                "edits": edits,
                "protected": build_protected_set(edits, previous_plan or {"tasks": []}),
            }
            provider = OpenAIProvider(model=model) if channel == "conversation" else None
            error = None
            try:
                events, artifact, latency_ms = _run_turn(
                    body=body,
                    manifest=manifest,
                    provider=provider,
                    compile_fn=compile_fn,
                    topology_check_fn=topology_check_fn,
                )
                score = (
                    _score_initial(artifact, expected, manifest)
                    if unit_kind == "initial"
                    else _score_revision(artifact, payload, base_actions, manifest)
                )
            except Exception as exc:  # noqa: BLE001 - persist failed benchmark runs
                events, artifact, latency_ms = [], None, 0.0
                error = f"{type(exc).__name__}: {exc}"
                score = {"success": False, "checks": {"exception_free": False}}
            record = {
                "timestamp": datetime.now(UTC).isoformat(),
                "case_set_id": case["case_set_id"],
                "scene": case["scene"]["id"],
                "kind": unit_kind,
                "unit_id": unit_id,
                "operation": payload.get("operation") if unit_kind == "revision" else None,
                "channel": channel,
                "repeat": repeat,
                "model": model if channel == "conversation" else None,
                "compile_mode": compile_mode,
                "latency_ms": round(latency_ms, 3),
                "usage": copy.deepcopy(provider.usage) if provider else None,
                **score,
                "error": error,
                "artifact": artifact,
                "events": events,
                "case_hash": _hash_json(case),
                "git_commit": _git_commit(),
            }
            with output_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            records.append(record)
            state = "PASS" if record["success"] else "FAIL"
            print(f"{state} {case['case_set_id']} {unit_kind}/{unit_id} {latency_ms:.1f} ms")
    return records


def _summary(records: list[dict]) -> dict:
    usage_fields = (
        "requests",
        "input_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    )

    def summed_usage(items: list[dict]) -> dict[str, int]:
        return {
            field: sum((item.get("usage") or {}).get(field, 0) for item in items)
            for field in usage_fields
        }

    groups: dict[str, list[dict]] = {}
    for record in records:
        groups.setdefault(record["kind"], []).append(record)
    summary = {
        kind: {
            "runs": len(items),
            "successes": sum(bool(item["success"]) for item in items),
            "success_rate": sum(bool(item["success"]) for item in items) / len(items),
            "median_latency_ms": statistics.median(item["latency_ms"] for item in items),
            "usage": summed_usage(items),
        }
        for kind, items in groups.items()
    }
    summary["total_usage"] = summed_usage(records)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--case", action="append", default=[], dest="case_ids")
    selection.add_argument("--scene", action="append", default=[], dest="scenes")
    selection.add_argument("--all-scenes", action="store_true")
    parser.add_argument("--kind", choices=("all", "initial", "revision"), default="all")
    parser.add_argument("--variant")
    parser.add_argument("--revision")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-5.6-terra"))
    parser.add_argument(
        "--compile-mode",
        choices=("llm-only", "service", "semantic"),
        default="llm-only",
        help=(
            "llm-only is the formal system-validation mode and skips physical "
            "MuJoCo/RRT compilation; service is an optional end-to-end integration "
            "check; semantic is a legacy alias for llm-only"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args(argv)
    if args.repeats < 1:
        raise SystemExit("--repeats must be positive")
    try:
        paths = select_cases(args.case_ids, args.scenes, args.all_scenes)
        if args.dry_run:
            for path in paths:
                case = _read_json(path)
                validate_case(path, case)
                print(
                    f"VALID {case['case_set_id']}: "
                    f"{len(case['initial']['prompt_variants'])} initial variants, "
                    f"{len(case.get('revisions', []))} revisions"
                )
            return 0
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = (args.output or RESULTS_ROOT / f"run-{stamp}.jsonl").resolve()
        records: list[dict] = []
        for path in paths:
            records.extend(run_case(
                path,
                kind=args.kind,
                variant_filter=args.variant,
                revision_filter=args.revision,
                repeats=args.repeats,
                model=args.model,
                compile_mode=args.compile_mode,
                output_path=output,
            ))
        print(json.dumps({"output": str(output), "summary": _summary(records)}, indent=2))
        return 0 if all(record["success"] for record in records) else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
