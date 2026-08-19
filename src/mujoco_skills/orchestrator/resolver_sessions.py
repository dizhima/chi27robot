"""Session-lineage and terminal-policy helpers for Resolver V2."""

from __future__ import annotations


def terminal_context(focus, compile_result: dict, ledger) -> dict:
    """Return late-bound terminal ownership policy for the current focus."""
    conflict = focus.conflict
    by_id = {item.get("id"): item for item in compile_result.get("schedule", [])}
    facility = (conflict.get("detail") or {}).get("facility")
    if not facility:
        group_facilities = {}
        for item in compile_result.get("schedule", []):
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
        if len(facilities) == 1:
            facility = next(iter(facilities))
    parties = conflict.get("parties") or []
    final_dwell_parties = [
        party for party in parties
        if party.get("occupancy") == "final_dwell"
        or (
            conflict.get("requires_departure_anchor")
            and party.get("motion") == "dwelling"
            and party.get("is_last_step")
        )
    ]
    terminal = bool(final_dwell_parties)
    if not terminal:
        return {"terminal": False}

    placements = [
        item for item in compile_result.get("schedule", [])
        if str(item.get("op", "")).lower() == "place"
        and item.get("facility") == facility
    ]
    completion = {}
    locked = set()
    for item in placements:
        robot = item.get("robot")
        completion[robot] = max(
            completion.get(robot, 0.0),
            float(item.get("start", 0.0)) + float(item.get("duration", 0.0)),
        )
        if item.get("robot_locked"):
            locked.add(robot)
    if len(completion) < 2:
        # Fallback for synthetic/unit fixtures: final dwell start represents
        # that participant's program completion.
        for party in parties:
            item = by_id.get(party.get("step"))
            if item:
                completion.setdefault(party.get("robot"), float(item.get("start", 0.0)))
                if item.get("robot_locked"):
                    locked.add(party.get("robot"))
    if not facility:
        eligible_robots = {party.get("robot") for party in final_dwell_parties}
        earlier = min(
            eligible_robots,
            key=lambda robot: (completion.get(robot, float("inf")), str(robot)),
        )
        others = [robot for robot in completion if robot != earlier]
        later = max(
            others or [earlier],
            key=lambda robot: (completion.get(robot, 0.0), str(robot)),
        )
        ordered = [earlier, later]
    else:
        ordered = sorted(completion, key=lambda robot: (completion[robot], str(robot)))
    if len(ordered) < 2:
        return {"terminal": True, "eligible": False, "reason": "completion_order_unknown"}
    return {
        "terminal": True,
        "eligible": True,
        "facility": facility,
        "completion": completion,
        "earlier_robot": ordered[0],
        "later_robot": ordered[-1],
        "locked_robots": sorted(locked),
        "departure_robot": ledger.departure_robot,
        "close_owner": ledger.close_owner,
    }
