"""Per-round JSON payload for the conflict-resolution LLM scheduler.

Design doc: `docs/conflict_resolution_scheduler_design.md` §4 ("#3 — the open
task"). Pure data shaping over whatever `/compile_plan` already returns — no
LLM calls, no geometry. Only `path`/`facility` conflicts are LLM-delegable
(`object`/`placement` are intent/capacity issues escalated to a human, design
doc §3c/§3d); this module filters to those two kinds. Geometry exposure is
deliberately symbolic-first: the only continuous quantity handed to the LLM
is `d_min` (plus the time `window`) — never base paths, waypoints, or
anything the LLM could use to "plan motion" itself (§3e).
"""

from __future__ import annotations


# kind→allowed-tools pruning and per-party motion/blocked/is_last_step are
# already computed server-side by skill_generators.detect_conflicts (design
# doc §3b/§3c). This module reshapes them and derives only symbolic,
# schedule-level terminal-close handoff candidates; it does no geometry.
DELEGABLE_KINDS = ("path", "facility")

# Navigate now routes authored constraints through the static scene navgrid;
# replan_path adds a temporary robot-swept obstacle to that same planner.
REPLAN_PATH_ENABLED = True

TOOLS = [
    {
        "name": "handoff_terminal_close",
        "args": {"conflict_id": "<conflict id>",
                  "close_step": "<Close* step id>",
                  "to_robot": "<robot>"},
        "doc": (
            "Hand a terminal container-close task (its complete "
            "approach/close/reset group) to the robot that completed the "
            "last placement in that container. Only use an exact entry "
            "from `handoff_candidates`; robot-locked or non-terminal close "
            "groups are never candidates. Recompile is the hard capability "
            "and geometry check."
        ),
    },
    {
        "name": "add_after",
        "args": {"conflict_id": "<conflict id>", "step": "<step id>",
                  "after_step": "<step id>"},
        "doc": (
            "`step` (always navigate/go_to) waits until `after_step` ends "
            "(adds a cross-robot dependency). Copy the `step` and `after_step` from an exact "
            "entry in `add_after_candidates` "
            "when that array is non-empty; those directions are already "
            "checked against the current dependency topology. Candidates "
            "with strategy `departure_start` let both robots start moving "
            "together and are tried before the conservative "
            "`departure_complete` anchor. If the blocked party's "
            "conflicting dwell has no departure (its `is_last_step` is "
            "true), call insert_go_to on it first — reference its new "
            "step id directly as `after_step` in the SAME round, its id is "
            "always '<robot>#go_to_rest' (deterministic, no need to wait "
            "for a result)."
        ),
    },
    {
        "name": "insert_yield",
        "args": {"conflict_id": "<conflict id>",
                 "yielding_step": "<moving step id>",
                 "winner_step": "<other moving step id>"},
        "doc": (
            "Resolve a proven spatial priority deadlock by inserting a "
            "deterministically planned temporary parking navigate, then "
            "serializing winner-pass-resume. Copy an exact yield_candidate; "
            "the engine owns all navgrid coordinates and routes."
        ),
    },
    {
        "name": "replan_path",
        "args": {"conflict_id": "<conflict id>",
                  "mover": "<step id, must have motion=moving>", "avoid": "<robot>"},
        "doc": (
            "Priority reroute: treat `avoid`'s swept path/dwell during the "
            "conflict window as an obstacle and reroute `mover` around it; "
            "the engine computes the full route and recompile-verifies. Only valid on a moving "
            "party. If no alternate lane exists, the round will report "
            "this call as failed — retry as add_after next round."
        ),
    },
    {
        "name": "insert_go_to",
        "args": {"conflict_id": "<conflict id>", "robot": "<robot>"},
        "doc": (
            "Retire a robot that has finished its program to its rest "
            "point (from `rest_points`), creating a departure step id "
            "'<robot>#go_to_rest' to anchor an add_after against. Only "
            "use when the blocked party's conflicting dwell is that "
            "robot's final step (`is_last_step` true). Idempotent: calling "
            "it again for the same robot reuses the existing step."
        ),
    },
]


def _has_cycle(edges):
    """Return whether an id -> dependencies graph contains a cycle."""
    visiting = set()
    done = set()

    def visit(node):
        if node in visiting:
            return True
        if node in done:
            return False
        visiting.add(node)
        if any(visit(dep) for dep in edges.get(node, ())):
            return True
        visiting.remove(node)
        done.add(node)
        return False

    return any(visit(node) for node in edges if node not in done)


def _handoff_candidates(schedule):
    """Derive the only reassignment form exposed to the scheduler.

    A close group can move only when it is a terminal suffix, is not
    robot-locked, and another robot is the latest placer in the same
    facility.  This keeps generic ownership decisions in the authored plan
    while making the common multi-object close handoff deterministic.
    """
    by_robot = {}
    for item in schedule:
        by_robot.setdefault(item["robot"], []).append(item)
    for items in by_robot.values():
        items.sort(key=lambda it: (it["start"], it["id"]))

    candidates = []
    for close in schedule:
        if not str(close.get("op", "")).lower().startswith("close"):
            continue
        facility = close.get("facility")
        if not facility:
            continue
        group = close.get("group") or close["id"]
        robot_items = by_robot.get(close["robot"], [])
        member_ids = [
            it["id"] for it in robot_items
            if (it.get("group") or it["id"]) == group
        ]
        member_set = set(member_ids)
        first = next(
            (i for i, it in enumerate(robot_items) if it["id"] in member_set),
            None)
        trailing = robot_items[first + len(member_ids):] if first is not None else []
        departure_group = f"{close['robot']}#go_to_rest"
        if (first is None
                or any(it["id"] not in member_set
                       for it in robot_items[first:first + len(member_ids)])
                or any((it.get("group") or it["id"]) != departure_group
                       for it in trailing)):
            continue
        if any(it.get("robot_locked", False) for it in robot_items
               if it["id"] in member_set):
            continue

        placements = [
            it for it in schedule
            if str(it.get("op", "")).lower() == "place"
            and it.get("facility") == facility
        ]
        if not placements:
            continue
        latest = max(
            placements,
            key=lambda it: (it["start"] + it["duration"], it["id"]))
        if latest["robot"] == close["robot"]:
            continue
        candidates.append({
            "close_step": close["id"],
            "close_group": group,
            "facility": facility,
            "from_robot": close["robot"],
            "to_robot": latest["robot"],
            "last_place_step": latest["id"],
            "member_steps": member_ids,
        })
    return candidates


def conflict_fingerprint(c):
    """Stable identity for a conflict across rounds (design doc §4/B2): the
    two step ids don't get renamed by any repair tool, so `(kind, steps)` is
    diff-able round over round even though window/detail drift slightly with
    each recompile. `c` is a raw detect_conflicts-shaped record (has "steps"),
    not a reshaped payload entry."""
    return (c["kind"], frozenset(c["steps"]))


def delegable_conflicts(compile_result, exclude_fingerprints=None):
    """The LLM-delegable (path/facility) conflicts this round, minus any
    already given up on, each tagged with a stable round-local id and its
    cross-round fingerprint. Shared by build_round_payload (LLM-facing shape)
    and the resolution loop (fingerprint bookkeeping) so the filter/order
    logic lives in exactly one place."""
    exclude_fingerprints = exclude_fingerprints or set()
    out = []
    seen_fingerprints = set()
    idx = 0
    for c in compile_result["conflicts"]:
        if c["kind"] not in DELEGABLE_KINDS:
            continue
        fp = conflict_fingerprint(c)
        if fp in exclude_fingerprints or fp in seen_fingerprints:
            continue
        seen_fingerprints.add(fp)
        out.append((f"c{idx}", c, fp))
        idx += 1
    return out


def build_round_payload(compile_result, round_num, round_cap=6, exclude_fingerprints=None):
    """`compile_result`: a /compile_plan-shaped dict for the CURRENT compile
    (schedule, conflicts, rest_points, ...). Returns the frozen per-round
    payload the LLM conflict-resolution scheduler reads.

    `exclude_fingerprints`: conflicts already given up on (design doc B2's
    no-progress guard) are left out of what the LLM sees, so it doesn't keep
    proposing the same tool against something already marked unresolvable."""
    schedule = compile_result["schedule"]
    makespan = max(
        (it["start"] + it["duration"] for it in schedule), default=0.0)

    schedule_summary = [
        {
            "id": it["id"],
            "robot": it["robot"],
            "op": it["op"],
            "motion": it["motion"],
            "facility": it["facility"],
            "object": it["object"],
            "window": [round(it["start"], 3), round(it["start"] + it["duration"], 3)],
            "group": it["group"],
            "is_last_in_robot": it["is_last_in_robot"],
            "robot_locked": bool(it.get("robot_locked", False)),
        }
        for it in schedule
    ]
    handoff_candidates = _handoff_candidates(schedule)

    dep_graph = {it["id"]: list(it.get("after", [])) for it in schedule}

    by_robot = {}
    for item in schedule:
        by_robot.setdefault(item["robot"], []).append(item)
    for items in by_robot.values():
        items.sort(key=lambda item: (item["start"], item["id"]))

    # The compiler's schedule already reflects authored, shared-container and
    # per-robot ordering. Use that complete topology to remove add_after
    # directions that are guaranteed to cycle before the LLM sees them.
    base_edges = {step_id: set(deps) for step_id, deps in dep_graph.items()}
    for items in by_robot.values():
        for previous, current in zip(items, items[1:]):
            base_edges.setdefault(current["id"], set()).add(previous["id"])

    def departure_step(conflict, party):
        """The event after which another robot may safely enter this spot.

        A symbolic facility is occupied through arrival/dwell until the next
        base movement. A moving path segment clears its corridor when that
        segment ends; dwelling/final-pose occupancy instead needs the next
        moving program item.
        """
        items = by_robot.get(party["robot"], [])
        index = next(
            (i for i, item in enumerate(items) if item["id"] == party["step"]),
            None,
        )
        if index is None:
            return None
        occupancy = party.get("occupancy", "active")
        if (conflict["kind"] == "path"
                and party.get("motion") == "moving"
                and occupancy == "active"):
            return party["step"]
        return next(
            (item["id"] for item in items[index + 1:]
             if item.get("motion") == "moving"),
            None,
        )

    def departure_start_anchor(party, departure_id):
        """Finish anchor that nominally coincides with departure start.

        ``after`` is finish-to-start only. When a dwelling party's later
        departure immediately follows another step, depending on that direct
        predecessor makes the waiter and departure become ready together.
        Gapped departures are excluded because their predecessor would let the
        waiter start before the departure actually begins.
        """
        if not departure_id or departure_id == party["step"]:
            return None
        items = by_robot.get(party["robot"], [])
        index = next(
            (i for i, item in enumerate(items) if item["id"] == departure_id),
            None,
        )
        if index is None or index == 0:
            return None
        previous, departure = items[index - 1], items[index]
        previous_end = float(previous["start"]) + float(previous["duration"])
        if abs(previous_end - float(departure["start"])) > 1e-6:
            return None
        return previous["id"]

    def waiter_step(party):
        """Return the navigation step that may safely be delayed.

        ``add_after`` is an *entry* scheduling tool: waiting must happen
        before a robot enters a contested area, never while it is already
        dwelling there.  A moving party can therefore be delayed directly.
        For a dwelling party, use its most recent preceding navigation on the
        same robot (the step that brought it into the current dwell).  Do not
        manufacture a dwelling waiter when there is no such entry step.
        """
        items = by_robot.get(party["robot"], [])
        index = next(
            (i for i, item in enumerate(items) if item["id"] == party["step"]),
            None,
        )
        if index is None:
            return None, None

        current = items[index]
        if str(current.get("op", "")).lower() in ("navigate", "go_to"):
            return current["id"], "direct_moving"

        for item in reversed(items[:index]):
            if str(item.get("op", "")).lower() in ("navigate", "go_to"):
                return item["id"], "entry_navigation"
        return None, None

    conflict_payload = []
    for cid, c, _fp in delegable_conflicts(compile_result, exclude_fingerprints):
        # symbolic label for "where": a facility name if there is one,
        # else the sampled crossing point as a label only (never fed back
        # as something to compute on).
        at = c["detail"].get("facility", c["detail"].get("at_xy"))
        allowed_tools = list(c["allowed_tools"])
        if not REPLAN_PATH_ENABLED:
            allowed_tools = [tool for tool in allowed_tools if tool != "replan_path"]
        # A facility record is a symbolic resource clash and carries no sampled
        # at_xy for the deterministic rerouter. Resolve it in time (or create a
        # final-dwell departure) rather than advertising an inapplicable tool.
        if c["kind"] == "facility":
            allowed_tools = [tool for tool in allowed_tools if tool != "replan_path"]
        # Priority rerouting is intentionally a one-shot strategy. If it did
        # not clear the conflict after hard verification, later rounds must
        # switch to temporal serialization instead of repeatedly replanning
        # the same conflict-window occupancy.
        if round_num > 1:
            allowed_tools = [tool for tool in allowed_tools if tool != "replan_path"]
        relevant_handoffs = [
            candidate for candidate in handoff_candidates
            if set(c["steps"]) & set(candidate["member_steps"])
        ]
        if relevant_handoffs and "handoff_terminal_close" not in allowed_tools:
            allowed_tools.insert(0, "handoff_terminal_close")
        parties = []
        for party in c["parties"]:
            departure = departure_step(c, party)
            parties.append({
                **party,
                "departure_step": departure,
                "departure_start_anchor": departure_start_anchor(
                    party, departure),
            })
        add_after_candidates = []
        seen_add_after = set()
        # Prefer the compiler-designated blocked party, but retain the safe
        # reverse orientation when the preferred direction is causally
        # downstream (the common insert-go-to/container-close case).
        party_order = sorted(
            parties, key=lambda party: party["step"] != c.get("blocked"))
        for waiter in party_order:
            wait_step, wait_strategy = waiter_step(waiter)
            if not wait_step:
                continue
            other = next(
                (party for party in parties
                 if party["robot"] != waiter["robot"]),
                None,
            )
            if not other:
                continue
            anchors = [
                (other.get("departure_start_anchor"), "departure_start"),
                (other.get("departure_step"), "departure_complete"),
            ]
            seen_anchors = set()
            for anchor, strategy in anchors:
                if (not anchor or anchor in seen_anchors
                        or anchor not in base_edges):
                    continue
                seen_anchors.add(anchor)
                edges = {
                    step_id: set(deps) for step_id, deps in base_edges.items()
                }
                edges.setdefault(wait_step, set()).add(anchor)
                candidate_key = (wait_step, anchor)
                if candidate_key not in seen_add_after and not _has_cycle(edges):
                    seen_add_after.add(candidate_key)
                    add_after_candidates.append({
                        "step": wait_step,
                        "after_step": anchor,
                        "strategy": strategy,
                        "conflict_step": waiter["step"],
                        "wait_strategy": wait_strategy,
                    })
        conflict_payload.append({
            "id": cid,
            "kind": c["kind"],
            "class": c["class"],
            "parties": parties,
            "blocked": c["blocked"],
            "window": c["window"],
            "d_min": c["detail"].get("min_dist"),
            "at": at,
            "allowed_tools": allowed_tools,
            "add_after_candidates": add_after_candidates,
            "handoff_candidates": relevant_handoffs,
            "requires_departure_anchor": c.get(
                "requires_departure_anchor", False),
        })

    return {
        "round": round_num,
        "round_cap": round_cap,
        "makespan": round(makespan, 3),
        "schedule": schedule_summary,
        "conflicts": conflict_payload,
        "dep_graph": dep_graph,
        "rest_points": compile_result.get("rest_points", {}),
        "handoff_candidates": handoff_candidates,
        "tools": TOOLS,
    }
