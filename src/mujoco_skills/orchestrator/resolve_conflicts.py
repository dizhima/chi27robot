"""The conflict-resolution LLM scheduler's round loop (design doc §3d/§4/B).

One round: build the payload -> `propose_fn(payload)` returns a batch of
tool calls -> validate the batch (no two calls touch the same step/edge,
design doc B1) -> apply the survivors (conflict_tools, deterministic) ->
`compile_fn(plan)` (hard verify, design doc §3e: geometry is NEVER decided by
the LLM) -> diff conflicts to measure progress. `run_resolution_loop` repeats
this to convergence or a round cap, tracking per-conflict no-progress rounds
so a conflict that won't budge gets marked unresolvable instead of retried
forever (design doc B2).

`compile_fn`/`propose_fn` are injected so this module has no HTTP/LLM/mujoco
dependency of its own — tests drive it with stubs; the real wiring (skill_client
HTTP compile + an LLM provider.chat call restricted to the bounded repair tools) is
a thin adapter around this loop, not part of it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from mujoco_skills.orchestrator.conflict_payload import (
    build_round_payload, conflict_fingerprint, delegable_conflicts,
)
from mujoco_skills.orchestrator.conflict_tools import (
    ToolError, apply_add_after, apply_handoff_terminal_close,
    apply_insert_go_to, apply_replan_path,
)
from mujoco_skills.orchestrator.protected_pins import (
    completed_plan_or_candidate, require_exact_after_edges,
)

DEFAULT_ROUND_CAP = 6
DEFAULT_STALE_LIMIT = 2

# insert_go_to must apply before any add_after in the same batch that
# references the departure step it creates (design doc: the id is
# deterministic so the LLM can name it without waiting for a result).
_TOOL_ORDER = {
    "handoff_terminal_close": 0,
    "insert_go_to": 1,
    "replan_path": 2,
    "add_after": 3,
}


@dataclass
class RoundResult:
    plan: dict
    compile_result: dict       # POST-round (after apply + recompile)
    payload: dict              # built from the PRE-round compile
    pre_fingerprints: set = field(default_factory=set)
    applied: list = field(default_factory=list)
    deferred: list = field(default_factory=list)
    rejected: list = field(default_factory=list)


def _touch_keys(name, args):
    """What this call edits, for the same-round collision rule (design doc
    B1): two calls in one batch may not touch the same step or the same
    robot's step-list."""
    if name == "add_after":
        return {("step", args.get("step"))}
    if name == "replan_path":
        return {("step", args.get("mover"))}
    if name == "insert_go_to":
        return {("robot_program", args.get("robot"))}
    if name == "handoff_terminal_close":
        # Deliberately not keyed as either robot_program: a useful composite
        # round may hand off the close and then retire the old owner.
        return {("close_group", args.get("close_step"))}
    return set()


def run_round(plan, compile_fn, propose_fn, round_num, round_cap=DEFAULT_ROUND_CAP,
              exclude_fingerprints=None, compile_result=None,
              topology_check_fn=None, protected=None):
    """Run exactly one round against `plan`. Does not mutate `plan`."""
    compile_result = compile_result or compile_fn(plan)
    payload = build_round_payload(
        compile_result, round_num, round_cap, exclude_fingerprints)
    delegable = delegable_conflicts(compile_result, exclude_fingerprints)
    conflicts_by_id = {cid: c for cid, c, _fp in delegable}
    payload_conflicts_by_id = {c["id"]: c for c in payload["conflicts"]}
    pre_fps = {fp for _cid, _c, fp in delegable}
    if not payload["conflicts"]:
        return RoundResult(plan, compile_result, payload, pre_fps)

    tool_calls = sorted(
        propose_fn(payload) or [],
        key=lambda call: _TOOL_ORDER.get(call.get("tool"), 99))
    # Ownership transfer rewrites per-robot sequential edges. Any temporal
    # repair proposed from the pre-handoff payload was reasoned over the old
    # topology, so verify the handoff alone and reconsider all other conflicts
    # from the freshly compiled next round.
    isolate_handoff = any(
        call.get("tool") == "handoff_terminal_close" for call in tool_calls)

    new_plan = copy.deepcopy(plan)
    dep_graph = {k: list(v) for k, v in payload["dep_graph"].items()}
    result = RoundResult(new_plan, compile_result, payload, pre_fps)
    touched = set()

    for call in tool_calls:
        name, args = call.get("tool"), call.get("args", {})
        record = {"conflict_id": call.get("conflict_id"), "tool": name, "args": args}
        if isolate_handoff and name != "handoff_terminal_close":
            result.deferred.append({
                **record,
                "reason": "handoff changes dependency topology; reconsider "
                "this action after the handoff-only verification compile",
            })
            continue
        if name not in _TOOL_ORDER:
            result.rejected.append({**record, "reason": f"unknown tool {name!r}"})
            continue
        conflict = conflicts_by_id.get(call.get("conflict_id"))
        payload_conflict = payload_conflicts_by_id.get(call.get("conflict_id"))
        if conflict is None or payload_conflict is None:
            result.rejected.append({
                **record,
                "reason": "tool call needs a valid conflict_id from this round",
            })
            continue
        insert_is_departure_prerequisite = (
            name == "insert_go_to"
            and (
                any(
                    party.get("robot") == args.get("robot")
                    and party.get("motion") == "dwelling"
                    and party.get("is_last_step")
                    for party in payload_conflict.get("parties", [])
                )
                or any(
                    candidate.get("from_robot") == args.get("robot")
                    for candidate in payload_conflict.get("handoff_candidates", [])
                )
            )
        )
        if (name not in payload_conflict.get("allowed_tools", [])
                and not insert_is_departure_prerequisite):
            result.rejected.append({
                **record,
                "reason": f"{name} is not allowed for this conflict class",
            })
            continue
        keys = _touch_keys(name, args)
        if any(value is None for _kind, value in keys):
            result.rejected.append({
                **record,
                "reason": f"missing required arguments for {name}",
            })
            continue
        if keys & touched:
            result.deferred.append(
                {**record, "reason": "another call this round already "
                 "touched the same step/robot-program"})
            continue
        try:
            if name == "add_after":
                candidates = payload_conflict.get("add_after_candidates", [])
                if candidates and not any(
                    candidate.get("step") == args.get("step")
                    and candidate.get("after_step") == args.get("after_step")
                    for candidate in candidates
                ):
                    raise ToolError(
                        "add_after must match a topology-safe "
                        "add_after_candidate for this conflict")
                edited_id = apply_add_after(
                    new_plan, dep_graph, args["step"], args["after_step"])
                dep_graph.setdefault(edited_id, [])
                if args["after_step"] not in dep_graph[edited_id]:
                    dep_graph[edited_id].append(args["after_step"])
            elif name == "replan_path":
                apply_replan_path(
                    new_plan, args["mover"], args["avoid"], conflict,
                    compile_result,
                )
            elif name == "insert_go_to":
                rest_xy = compile_result.get("rest_points", {}).get(args["robot"])
                new_id = apply_insert_go_to(new_plan, args["robot"], rest_xy)
                dep_graph.setdefault(new_id, [])
            elif name == "handoff_terminal_close":
                candidate = next(
                    (candidate for candidate
                     in payload_conflict.get("handoff_candidates", [])
                     if candidate["close_step"] == args.get("close_step")
                     and candidate["to_robot"] == args.get("to_robot")),
                    None)
                if candidate is None:
                    raise ToolError(
                        "handoff_terminal_close must exactly match a "
                        "handoff_candidate for this conflict")
                apply_handoff_terminal_close(
                    new_plan, candidate["close_step"], candidate["to_robot"])
        except (KeyError, TypeError, ToolError) as exc:
            result.rejected.append({**record, "reason": str(exc)})
            continue
        touched |= keys
        result.applied.append(record)

    try:
        # Do this before topology/geometry work: a tool implementation that
        # rebuilt a step and lost a user edge must not reach the compiler.
        require_exact_after_edges(new_plan, protected)
        if topology_check_fn is not None:
            topology_check_fn(new_plan)
        result.compile_result = compile_fn(new_plan)
        require_exact_after_edges(
            completed_plan_or_candidate(result.compile_result, new_plan), protected)
    except Exception as exc:  # hard verification failed: reject the whole batch
        prefix = (
            "topology rejected"
            if getattr(exc, "code", None) is not None
            else "post-repair recompile failed"
        )
        reason = f"{prefix}: {exc}"
        result.rejected.extend({**call, "reason": reason} for call in result.applied)
        result.applied = []
        result.plan = plan
        result.compile_result = compile_result
    return result


def run_resolution_loop(plan, compile_fn, propose_fn,
                         round_cap=DEFAULT_ROUND_CAP,
                         stale_limit=DEFAULT_STALE_LIMIT,
                         initial_compile_result=None,
                         topology_check_fn=None, protected=None):
    """Batch-delegate to convergence or `round_cap` rounds (design doc §3d.3).

    Returns (final_plan, report) where `report` is the structured residual
    report (design doc B4): what changed, what's still broken and why, plus
    round/convergence bookkeeping.
    """
    current_plan = plan
    stale_rounds = {}   # fingerprint -> consecutive rounds seen with no progress
    unresolvable = {}   # fingerprint -> reason
    rounds = []
    current_compile_result = initial_compile_result

    for round_num in range(1, round_cap + 1):
        exclude = set(unresolvable)
        outcome = run_round(current_plan, compile_fn, propose_fn, round_num,
                            round_cap, exclude, current_compile_result,
                            topology_check_fn, protected)
        rounds.append(outcome)
        current_plan = outcome.plan
        # The post-round hard verification is exactly the next round's input;
        # reuse it instead of repeating the same expensive geometry compile.
        current_compile_result = outcome.compile_result

        pre_fps = outcome.pre_fingerprints
        cur_fps = {fp for _cid, _c, fp in
                   delegable_conflicts(outcome.compile_result, exclude)}

        # A conflict that's still present after this round's recompile made
        # no progress this round; one that vanished is resolved and drops its
        # stale counter; a brand-new one (introduced by e.g. replan_path,
        # design doc §3c) starts fresh rather than inheriting staleness.
        made_structural_progress = bool(outcome.applied)
        for fp in cur_fps:
            if made_structural_progress:
                # handoff/insert_go_to can be a necessary topology preparation
                # whose target conflict remains visible until the next round's
                # temporal edge. Do not exhaust its retry budget here.
                stale_rounds[fp] = 0
            else:
                stale_rounds[fp] = (
                    stale_rounds.get(fp, 0) + 1 if fp in pre_fps else 0)
            if stale_rounds[fp] >= stale_limit:
                unresolvable[fp] = "no progress after repeated rounds"
        for fp in pre_fps - cur_fps:
            stale_rounds.pop(fp, None)

        if not cur_fps:
            break

    final_conflicts = list(rounds[-1].compile_result["conflicts"]) if rounds else []
    still_delegable = [c for c in final_conflicts if c["kind"] in ("path", "facility")]
    still_unresolvable = [c for c in still_delegable
                           if conflict_fingerprint(c) in unresolvable]
    still_pending = [c for c in still_delegable
                      if conflict_fingerprint(c) not in unresolvable]
    escalated = [c for c in final_conflicts if c["kind"] in ("object", "placement")]

    report = {
        "rounds_used": len(rounds),
        "round_cap": round_cap,
        "converged": len(still_delegable) == 0,
        "applied": [
            {"round": i + 1, **call}
            for i, r in enumerate(rounds) for call in r.applied
        ],
        "rejected": [
            {"round": i + 1, **call}
            for i, r in enumerate(rounds) for call in r.rejected
        ],
        "deferred": [
            {"round": i + 1, **call}
            for i, r in enumerate(rounds) for call in r.deferred
        ],
        "unresolved": [
            {"kind": c["kind"], "steps": c["steps"], "robots": c["robots"],
             "reason": "no_alternate_strategy_found_in_time"}
            for c in still_unresolvable
        ] + [
            {"kind": c["kind"], "steps": c["steps"], "robots": c["robots"],
             "reason": "round_cap_reached"}
            for c in still_pending
        ],
        "escalated_to_human": [
            {"kind": c["kind"], "steps": c["steps"], "robots": c["robots"],
             "message": c["message"]}
            for c in escalated
        ],
    }
    # Every accepted round has already verified its compiler-completed plan;
    # retain this end-of-run assertion for zero-round and future return paths.
    require_exact_after_edges(current_plan, protected)
    return current_plan, report


def render_residual_report(report):
    """Template the structured report into the chat NL "accountability"
    message (design doc §3e/B4) — deterministic, not another LLM call, so
    the accountability framing can't drift from what the structured report
    actually says."""
    lines = []
    n_applied = len(report["applied"])
    if n_applied:
        lines.append(
            f"Resolved {n_applied} conflict action(s) over "
            f"{report['rounds_used']} round(s):")
        for call in report["applied"]:
            lines.append(
                f"  - round {call['round']}: {call['tool']}({call['args']}) "
                f"for conflict {call.get('conflict_id')}")
    else:
        lines.append(f"No conflict actions were applied ({report['rounds_used']} round(s) run).")

    if report["unresolved"]:
        lines.append(f"{len(report['unresolved'])} conflict(s) could not be resolved:")
        for u in report["unresolved"]:
            lines.append(
                f"  - {u['kind']} between {u['steps'][0]} ({u['robots'][0]}) "
                f"and {u['steps'][1]} ({u['robots'][1]}): {u['reason']}")

    if report["rejected"]:
        lines.append(f"{len(report['rejected'])} proposed action(s) were rejected by verification:")
        for call in report["rejected"]:
            lines.append(
                f"  - round {call['round']}: {call['tool']}: {call['reason']}")

    if report["deferred"]:
        lines.append(f"{len(report['deferred'])} colliding batch action(s) were deferred.")

    if report["escalated_to_human"]:
        lines.append(
            f"{len(report['escalated_to_human'])} conflict(s) need a human "
            "decision (object/placement, out of scheduling scope):")
        for e in report["escalated_to_human"]:
            lines.append(f"  - {e['message']}")

    lines.append("Converged." if report["converged"] else
                 "Did not fully converge within the round cap.")
    return "\n".join(lines)
