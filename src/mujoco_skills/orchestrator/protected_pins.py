"""Validation helpers for user-authored resolver pins.

The resolver may add scheduling constraints, but it must never silently change
a manual ``set_step_after`` field.  These helpers deliberately inspect only
explicit ``after`` fields: compiler-inferred sequencing is not a user pin.
"""

from __future__ import annotations


class ExactAfterPinError(ValueError):
    """A candidate or compiler-completed plan changed a manual after field."""


def exact_after_edges(protected: object) -> tuple[tuple[str, str], ...]:
    """Return validated manual ``(step, after_step)`` pins, de-duplicated."""
    if not isinstance(protected, dict):
        return ()
    seen: set[tuple[str, str]] = set()
    edges: list[tuple[str, str]] = []
    for item in protected.get("exact_after_edges") or []:
        if not isinstance(item, dict):
            continue
        step, after_step = item.get("step"), item.get("after_step")
        if (
            isinstance(step, str)
            and step
            and isinstance(after_step, str)
            and after_step
            and step != after_step
            and (step, after_step) not in seen
        ):
            seen.add((step, after_step))
            edges.append((step, after_step))
    return tuple(edges)


def exact_after_fields(
    protected: object,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return final manual ``after`` fields, with the last value winning."""
    if not isinstance(protected, dict):
        return ()
    fields: dict[str, tuple[str, ...]] = {}
    for item in protected.get("exact_after_fields") or []:
        if not isinstance(item, dict):
            continue
        step, after = item.get("step"), item.get("after")
        if not isinstance(step, str) or not step or not isinstance(after, list):
            continue
        normalized: list[str] = []
        for value in after:
            if (
                isinstance(value, str)
                and value
                and value != step
                and value not in normalized
            ):
                normalized.append(value)
        fields[step] = tuple(normalized)
    return tuple(fields.items())


def _iter_steps(plan: object):
    if not isinstance(plan, dict):
        return
    tasks = plan.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict):
                for step in task.get("steps") or []:
                    if isinstance(step, dict):
                        yield step
        return
    for steps in plan.values():
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict):
                    yield step


def has_steps(plan: object) -> bool:
    return any(True for _ in _iter_steps(plan))


def completed_plan_or_candidate(compile_result: object, candidate: dict) -> dict:
    """Use compiler completed plan when it is meaningful, otherwise candidate.

    A few lightweight test compilers intentionally return ``completed: {}``.
    That is not a plan which can prove or disprove a preserved manual edge.
    """
    if isinstance(compile_result, dict):
        completed = compile_result.get("completed")
        if isinstance(completed, dict) and has_steps(completed):
            return completed
    return candidate


def missing_exact_after_edges(plan: object, protected: object) -> tuple[tuple[str, str], ...]:
    required = exact_after_edges(protected)
    if not required:
        return ()
    by_id = {
        step.get("id"): step
        for step in _iter_steps(plan)
        if isinstance(step.get("id"), str)
    }
    return tuple(
        (step_id, after_step)
        for step_id, after_step in required
        if after_step not in (by_id.get(step_id, {}).get("after") or [])
    )


def mismatched_exact_after_fields(
    plan: object,
    protected: object,
) -> tuple[tuple[str, tuple[str, ...], tuple[str, ...] | None], ...]:
    """Return locked fields whose explicit dependency set changed."""
    required = exact_after_fields(protected)
    if not required:
        return ()
    by_id = {
        step.get("id"): step
        for step in _iter_steps(plan)
        if isinstance(step.get("id"), str)
    }
    mismatches = []
    for step_id, expected in required:
        step = by_id.get(step_id)
        if step is None:
            mismatches.append((step_id, expected, None))
            continue
        actual = tuple(dict.fromkeys(
            value for value in (step.get("after") or [])
            if isinstance(value, str) and value and value != step_id
        ))
        if set(actual) != set(expected):
            mismatches.append((step_id, expected, actual))
    return tuple(mismatches)


def require_exact_after_edges(plan: object, protected: object) -> None:
    missing = missing_exact_after_edges(plan, protected)
    if missing:
        rendered = ", ".join(f"{step} after {after}" for step, after in missing)
        raise ExactAfterPinError(f"manual exact after pin missing: {rendered}")
    mismatches = mismatched_exact_after_fields(plan, protected)
    if mismatches:
        rendered = ", ".join(
            f"{step}: expected {list(expected)!r}, got "
            f"{None if actual is None else list(actual)!r}"
            for step, expected, actual in mismatches
        )
        raise ExactAfterPinError(f"manual exact after field changed: {rendered}")
