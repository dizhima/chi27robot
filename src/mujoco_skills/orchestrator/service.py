"""Stateless HTTP service for multi-turn stage-1 grounding.

Run with:
    python -m mujoco_skills.orchestrator.service
"""

from __future__ import annotations

import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import truststore
from dotenv import load_dotenv

# These must happen before an OpenAI client (and therefore an HTTPS client) is
# constructed. This mirrors orchestrator.__main__.
load_dotenv()
truststore.inject_into_ssl()

from dataclasses import asdict

from mujoco_skills.orchestrator import authoring, conversation, decompose
from mujoco_skills.orchestrator.conflict_resolver import propose_repairs
from mujoco_skills.orchestrator.providers.openai_provider import OpenAIProvider
from mujoco_skills.orchestrator.protected_pins import (
    completed_plan_or_candidate,
    require_exact_after_edges,
)
from mujoco_skills.orchestrator.resolve_conflicts import (
    render_residual_report,
    run_resolution_loop,
)
from mujoco_skills.orchestrator.resolver_v2 import (
    render_v2_report,
    run_resolution_v2,
)
from mujoco_skills.orchestrator.schema import SemanticTask, robot_ids_from_manifest
from mujoco_skills.orchestrator.stage1 import ground
from mujoco_skills.service.scene_runtime import scene_runtime_from_env

RUNTIME = scene_runtime_from_env(
    default_public=Path(__file__).resolve().parents[3] / "frontend/public"
)
PUBLIC = RUNTIME.public
MANIFEST = RUNTIME.manifest
PORT = int(os.environ.get("ORCHESTRATOR_PORT", 8900))
HOST = os.environ.get("ORCHESTRATOR_HOST", "127.0.0.1")
SKILL_SERVICE_URL = os.environ.get("SKILL_SERVICE_URL", "http://127.0.0.1:8899")
COMPILE_TIMEOUT_SECONDS = float(os.environ.get("RESOLVER_COMPILE_TIMEOUT", "600"))
RESOLVER_VERSION = os.environ.get("RESOLVER_VERSION", "v2").lower()

# Same English text the (now-removed) client-side resolve gate used
# (frontend/src/conversation/conversationStreamClient.ts
# RESOLVE_PRECONDITION_MESSAGE) -- kept identical for continuity.
RESOLVE_PRECONDITION_MESSAGE = (
    "Can't resolve yet: need a compiled, in-sync plan with at least one "
    "delegable conflict. Compile the current draft first."
)


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text("utf-8"))


def do_ground(body: dict, *, provider=None, manifest: dict | None = None) -> dict:
    messages = body.get("messages")
    current = body.get("current_tasks")
    if not isinstance(messages, list) or not isinstance(current, list):
        raise ValueError("messages and current_tasks must be arrays")
    current_tasks = [
        SemanticTask(
            id=str(task["id"]),
            action=str(task["action"]),
            object=str(task["object"]),
            dest=str(task["dest"]),
        )
        for task in current
    ]
    result = ground(
        messages=messages,
        current_tasks=current_tasks,
        provider=provider or OpenAIProvider(),
        manifest=manifest or load_manifest(),
    )
    return {
        "tasks": [
            {"id": t.id, "action": t.action, "object": t.object, "dest": t.dest}
            for t in result.tasks
        ],
        "message": result.message,
        "reason": result.reason,
    }


def do_author(body: dict, *, provider=None, manifest: dict | None = None) -> dict:
    """Path Y: agentic semantic authoring loop -> deterministic decompose.

    Stateless. The frontend re-sends the full NL transcript plus the previous
    turn's AugmentedAction[] (``current_actions``) so refine-by-chat and manual
    robot reassignments stay consistent. Returns both the semantic ``actions``
    (to echo back next turn) and the decomposed ``plan`` (an AuthoredPlan ready
    for skill_service /compile_plan).
    """
    messages = body.get("messages")
    current = body.get("current_actions", [])
    scene_refs = body.get("scene_refs") or []
    plan_refs = body.get("plan_refs") or []
    if not isinstance(messages, list) or not isinstance(current, list):
        raise ValueError("messages and current_actions must be arrays")
    manifest = manifest or load_manifest()
    current_plan = authoring.parse_actions(current, manifest)
    result = authoring.author(
        messages=messages,
        current_plan=current_plan,
        provider=provider or OpenAIProvider(),
        manifest=manifest,
        scene_refs=scene_refs,
        plan_refs=plan_refs,
    )
    actions = [asdict(action) for action in result.actions]
    plan = decompose.decompose(result.actions, manifest, scene_refs) if result.actions else None
    return {
        "actions": actions,
        "plan": plan,
        "message": result.message,
        "reason": result.reason,
    }


class CompileServiceError(ValueError):
    def __init__(self, message: str, detail: dict | None = None):
        self.detail = detail or {}
        super().__init__(message)


def _compile_progress_event(completed: int, total: int) -> dict:
    percentage = (
        min(100, max(0, round(completed * 100 / total)))
        if total > 0 else 0
    )
    return {
        "type": "progress",
        "stage": "compiling",
        "text": f"Compiling the plan. ({percentage}%)",
        "completed": completed,
        "total": total,
    }


def _compile_via_skill_service(
    plan: dict, *, retain_snapshot: bool = False, progress_callback=None,
) -> dict:
    # Resolver fallback/candidate compiles never enter the snapshot registry
    # (retain_snapshot=False, the default). The compound tail's round-0
    # compile is the exception: it is the user-visible compile, and it passes
    # True so its compile_id survives for a later sync press to reuse.
    data = json.dumps({"plan": plan, "retain_snapshot": retain_snapshot}).encode("utf-8")
    streaming = progress_callback is not None and retain_snapshot
    route = "/compile_plan_stream" if streaming else "/compile_plan"
    request = Request(
        f"{SKILL_SERVICE_URL}{route}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=COMPILE_TIMEOUT_SECONDS) as response:
            if streaming:
                payload = None
                for raw_line in response:
                    if not raw_line.strip():
                        continue
                    event = json.loads(raw_line.decode("utf-8"))
                    if event.get("type") == "progress":
                        progress_callback(
                            int(event["completed"]), int(event["total"]))
                    elif event.get("type") == "result":
                        payload = event.get("result")
                    elif event.get("type") == "error":
                        status = int(event.get("status", 400))
                        raise CompileServiceError(
                            f"skill_service compile failed ({status}): "
                            f"{event.get('error', 'unknown error')}",
                            event.get("detail")
                            if isinstance(event.get("detail"), dict) else None,
                        )
            else:
                payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw_detail = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_detail)
        except json.JSONDecodeError:
            payload = {}
        message = payload.get("error") or raw_detail
        raise CompileServiceError(
            f"skill_service compile failed ({exc.code}): {message}",
            payload.get("detail") if isinstance(payload.get("detail"), dict) else None,
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"skill_service unavailable at {SKILL_SERVICE_URL}: {exc.reason}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError(f"invalid skill_service compile response: {payload}")
    return payload


class TopologyValidationError(ValueError):
    """Structured /validate_plan rejection propagated to edit/resolve callers."""

    def __init__(self, error: dict):
        self.error = dict(error)
        self.code = str(error.get("code", "invalid_plan_topology"))
        super().__init__(str(error.get("message", "plan topology is invalid")))


def _validate_via_skill_service(plan: dict) -> None:
    data = json.dumps({"plan": plan}).encode("utf-8")
    request = Request(
        f"{SKILL_SERVICE_URL}/validate_plan",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=COMPILE_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(
            f"skill_service topology validation failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"skill_service unavailable at {SKILL_SERVICE_URL}: {exc.reason}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid skill_service topology response: {payload}")
    if payload.get("ok") is True:
        return
    error = payload.get("error")
    if not isinstance(error, dict):
        error = {"code": "invalid_plan_topology", "message": str(error or payload)}
    raise TopologyValidationError(error)


def _snapshot_via_skill_service(compile_id: str, plan: dict) -> dict:
    data = json.dumps({"compile_id": compile_id, "plan": plan}).encode("utf-8")
    request = Request(
        f"{SKILL_SERVICE_URL}/compile_snapshot",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=COMPILE_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError) as exc:
        raise LookupError(f"compile snapshot unavailable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise LookupError(f"invalid compile snapshot response: {payload}")
    return payload


def _nest_resolved_plan(flat_plan: dict) -> dict:
    """Convert the resolver's flat completed-plan form back to AuthoredPlan."""
    grouped: dict[tuple[str, str], dict] = {}
    fallback_order = 1 + max(
        (
            int(step.get("_author_order", -1))
            for steps in flat_plan.values()
            for step in steps
            if isinstance(step, dict)
            and isinstance(step.get("_author_order", -1), (int, float))
        ),
        default=-1,
    )
    for robot, steps in flat_plan.items():
        if not isinstance(steps, list):
            raise ValueError(f"resolved plan for {robot!r} must be an array")
        for robot_order, raw_step in enumerate(steps):
            if not isinstance(raw_step, dict):
                raise ValueError("resolved plan steps must be objects")
            step = dict(raw_step)
            group = str(step.get("group") or step.get("id") or f"{robot}#{robot_order}")
            order_value = step.get("_author_order")
            if not isinstance(order_value, (int, float)):
                order_value = fallback_order
                fallback_order += 1
            for internal_key in ("_author_order", "_robot_order", "robot"):
                step.pop(internal_key, None)
            key = (str(robot), group)
            entry = grouped.setdefault(key, {
                "order": float(order_value),
                "task": group,
                "robot": str(robot),
                "robot_locked": False,
                "steps": [],
            })
            entry["order"] = min(entry["order"], float(order_value))
            entry["robot_locked"] = entry["robot_locked"] or bool(
                step.get("robot_locked", False)
            )
            entry["steps"].append(step)
    tasks = []
    for entry in sorted(grouped.values(), key=lambda item: item["order"]):
        entry.pop("order")
        tasks.append(entry)
    return {"tasks": tasks}


def _public_compile_result(compile_result: dict, final_flat: dict) -> dict:
    """Frontend-safe final verification payload; excludes resolver geometry."""
    conflicts = list(compile_result.get("conflicts", []))
    return {
        "schedule": list(compile_result.get("schedule", [])),
        "warnings": list(compile_result.get("warnings", [
            conflict.get("message", "") for conflict in conflicts
            if conflict.get("message")
        ])),
        "conflicts": conflicts,
        "completed": compile_result.get("completed", final_flat),
        # A later sync press adopts this without re-POSTing (integration spec
        # §1); absent unless the compile retained a snapshot.
        "compile_id": compile_result.get("compile_id"),
    }


def resolve_v2_core(body: dict, *, provider=None, compile_fn=None,
                    snapshot_lookup_fn=None, topology_check_fn=None, on_event=None,
                    initial_compile_result=None) -> dict:
    """Reusable v2 resolve pipeline: snapshot reuse, fallback compile,
    ``run_resolution_v2``, nesting, and public-compile shaping.

    Returns EXACTLY the dict ``do_resolve_conflicts`` returns for the v2 path.
    ``on_event`` (optional) is passed straight through to ``run_resolution_v2``
    as pure observation; it does not change this function's behavior in any
    other way. Both the non-streaming ``do_resolve_conflicts`` (with
    ``on_event=None``) and the streaming path call this one implementation.

    ``initial_compile_result``, when the caller already has a fresh compile in
    hand (the compound tail's round-0 compile, or a previous auto-continue
    pass's committed compile), is used as-is and skips both the snapshot
    lookup and the fallback compile below -- one fewer redundant compile.
    """
    plan = body.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("plan must be the compiled completed-plan object")
    uses_default_compile = compile_fn is None
    compile_fn = compile_fn or _compile_via_skill_service
    if topology_check_fn is None and uses_default_compile:
        topology_check_fn = _validate_via_skill_service
    # S3 (compound_turn_integration_spec.md §4 items 11-13): the manual-edit
    # protected set, when the caller (the compound tail) supplies one. Absent
    # for every pre-S3 caller (do_resolve_conflicts, the legacy resolve
    # stream), so this is a pure additive default.
    protected = body.get("protected")
    initial_snapshot_reused = False
    if initial_compile_result is None:
        compile_id = body.get("compile_id")
        if isinstance(compile_id, str) and compile_id:
            lookup = snapshot_lookup_fn or _snapshot_via_skill_service
            try:
                initial_compile_result = lookup(compile_id, plan)
                initial_snapshot_reused = True
            except (KeyError, LookupError, ValueError):
                initial_compile_result = None
        if initial_compile_result is None:
            initial_compile_result = compile_fn(plan)
    flat_plan = completed_plan_or_candidate(initial_compile_result, plan)
    require_exact_after_edges(flat_plan, protected)
    robot_ids = robot_ids_from_manifest(load_manifest())
    final_flat, report, final_compile_result = run_resolution_v2(
        flat_plan,
        compile_fn,
        provider or OpenAIProvider(),
        initial_compile_result=initial_compile_result,
        initial_snapshot_reused=initial_snapshot_reused,
        return_compile_result=True,
        on_event=on_event,
        protected=protected,
        topology_check_fn=topology_check_fn,
        robot_ids=robot_ids,
    )
    # run_resolution_v2 did not perform the fallback compile itself.
    if not initial_snapshot_reused:
        report["compile_count"] += 1
    return {
        "plan": _nest_resolved_plan(final_flat),
        "report": report,
        "message": render_v2_report(report),
        "initial_snapshot_reused": initial_snapshot_reused,
        "compile": _public_compile_result(
            final_compile_result, final_flat),
    }


def do_resolve_conflicts(body: dict, *, provider=None, compile_fn=None,
                         snapshot_lookup_fn=None, topology_check_fn=None,
                         resolver_version=None) -> dict:
    """Resolve the current compiled plan without consuming chat history."""
    plan = body.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("plan must be the compiled completed-plan object")
    uses_default_compile = compile_fn is None
    compile_fn = compile_fn or _compile_via_skill_service
    if topology_check_fn is None and uses_default_compile:
        topology_check_fn = _validate_via_skill_service
    version = (resolver_version or RESOLVER_VERSION).lower()
    if version not in ("v1", "v2"):
        raise ValueError("RESOLVER_VERSION must be 'v1' or 'v2'")
    if version == "v2":
        return resolve_v2_core(
            body,
            provider=provider,
            compile_fn=compile_fn,
            snapshot_lookup_fn=snapshot_lookup_fn,
            topology_check_fn=topology_check_fn,
            on_event=None,
        )
    # The repair tools need compiler-filled standoffs. Accept nested plans for
    # API robustness, but normalize them through one compile before the loop.
    initial_compile_result = compile_fn(plan) if "tasks" in plan else None
    protected = body.get("protected")
    flat_plan = (
        completed_plan_or_candidate(initial_compile_result, plan)
        if initial_compile_result else plan
    )
    require_exact_after_edges(flat_plan, protected)
    llm = provider or OpenAIProvider()
    robot_ids = robot_ids_from_manifest(load_manifest())
    final_flat, report = run_resolution_loop(
        flat_plan,
        compile_fn,
        lambda payload: propose_repairs(
            payload,
            llm,
            robot_ids=robot_ids,
        ),
        initial_compile_result=initial_compile_result,
        topology_check_fn=topology_check_fn,
        protected=protected,
    )
    return {
        "plan": _nest_resolved_plan(final_flat),
        "report": report,
        "message": render_residual_report(report),
    }


def _latest_user_text(messages: object) -> str | None:
    """Mirror ``authoring.author``'s latest-user selection: the content of
    the last ``role == "user"`` message, or ``None`` if there is none."""
    if not isinstance(messages, list):
        return None
    for item in reversed(messages):
        if isinstance(item, dict) and item.get("role") == "user":
            return str(item.get("content", ""))
    return None


def _resolve_gate_fails(plan_state: dict) -> bool:
    return (
        plan_state.get("status") != "ready"
        or bool(plan_state.get("dirty"))
        or not plan_state.get("in_sync")
        or plan_state.get("delegable_conflict_count", 0) == 0
    )


def _emit_resolve_precondition(body: dict, write_event) -> None:
    """The full envelope for a resolve request that fails the eligibility gate.

    Shared verbatim by the legacy resolve path and the compound sync path so
    an ineligible Resolve/sync press looks identical on both.
    """
    write_event({"type": "message_started"})
    write_event(
        {
            "type": "intent_selected",
            "intent": "resolve",
            "text": "Checking the compiled plan for conflicts.",
        }
    )
    write_event(
        {
            "type": "result",
            "artifact": {
                "kind": "answer",
                "turn_id": body.get("turn_id"),
                "content": RESOLVE_PRECONDITION_MESSAGE,
                "follow_up": False,
            },
        }
    )
    write_event({"type": "message_completed", "text": RESOLVE_PRECONDITION_MESSAGE})


def dispatch_conversation_turn(body: dict, write_event, *, compile_fn=None) -> None:
    """Classify-then-dispatch for ``POST /conversation/stream`` (phase4 §5/§5.1).

    ``intent_hint == "resolve"`` (the Resolve button) skips the classifier but
    still runs the backend eligibility gate (defensive; the button is
    disabled client-side when ineligible). Free text is classified into
    author/resolve/explain via ``conversation.classify_intent``. Each
    ``stream_*`` function emits its own ``message_started``/``intent_selected``
    -- this dispatcher only emits those two events itself for the
    classified-resolve gate-failure path (no ``stream_resolve_turn`` call in
    that case).

    Everything but ``explain`` instead routes to the compound tail
    (``conversation.stream_compound_turn``) when ``COMPOUND_TURN=1``
    (integration spec item 10) -- default OFF, so that whole branch is dead
    code and the routing below is byte-for-byte the pre-compound-turn
    behavior until an operator opts in. This is the rollback path, and it
    extends to the classifier itself: the two-intent D3 variant is selected
    only under the flag, so with it unset free text still classifies into
    author/resolve/explain exactly as before.
    """
    plan_state = body.get("plan_state") or {}
    active_compile_fn = compile_fn or _compile_via_skill_service
    compound = os.environ.get("COMPOUND_TURN") == "1"
    if body.get("intent_hint") == "resolve":
        intent = "resolve"
    else:
        latest_text = _latest_user_text(body.get("messages"))
        if latest_text is None:
            write_event(
                {"type": "error", "text": "authoring requires at least one user message"}
            )
            return
        state_hint = {
            "plan_exists": bool(plan_state.get("completed_plan")),
            "delegable_conflict_count": plan_state.get("delegable_conflict_count", 0),
        }
        classification = conversation.classify_intent(
            latest_text, state_hint, OpenAIProvider(), compound=compound
        )
        intent = classification.get("intent", "author")

    if intent != "explain" and compound:
        # A "resolve" here can only have come from the button (the compound
        # classifier cannot emit it), and the button is a re-run of the tail
        # over the already-compiled plan -- no natural language to translate,
        # so the author stage is skipped via sync semantics rather than
        # burning an authoring call on "resolve the conflicts".
        tail_body = body
        if intent == "resolve":
            if _resolve_gate_fails(plan_state):
                _emit_resolve_precondition(body, write_event)
                return
            tail_body = {
                **body,
                "intent_hint": "sync",
                "plan": plan_state.get("completed_plan"),
                "compile_id": plan_state.get("compile_id"),
            }
        conversation.stream_compound_turn(
            tail_body,
            write_event,
            provider=OpenAIProvider(),
            manifest=load_manifest(),
            compile_fn=active_compile_fn,
            topology_check_fn=_validate_via_skill_service,
            snapshot_lookup_fn=_snapshot_via_skill_service,
        )
        return

    if intent == "resolve":
        if _resolve_gate_fails(plan_state):
            _emit_resolve_precondition(body, write_event)
            return
        resolve_body = {
            **body,
            "plan": plan_state.get("completed_plan"),
            "compile_id": plan_state.get("compile_id"),
        }
        conversation.stream_resolve_turn(
            resolve_body,
            write_event,
            provider=OpenAIProvider(),
            compile_fn=active_compile_fn,
            topology_check_fn=_validate_via_skill_service,
            snapshot_lookup_fn=_snapshot_via_skill_service,
        )
    elif intent == "explain":
        conversation.stream_explain_turn(body, write_event, provider=OpenAIProvider())
    else:
        conversation.stream_author_turn(
            body,
            write_event,
            provider=OpenAIProvider(),
            manifest=load_manifest(),
        )


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(size) or b"{}")

    def _open_ndjson_stream(self):
        """Start a chunked-by-newline NDJSON response and return a writer.

        Deliberately mirrors ``_send``'s CORS headers but omits Content-Length
        (unknown ahead of time) and forces the connection closed at the end of
        the stream so the client's `fetch` reader sees EOF once the handler
        returns. The returned closure flushes after every line so events are
        delivered incrementally, not buffered until the response finishes.
        """
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.close_connection = True

        def write_event(obj: dict) -> None:
            try:
                self.wfile.write(
                    json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                # The client went away mid-stream. This is ROUTINE, not an
                # error: the debounced edit tail aborts its in-flight request
                # every time a newer edit arrives (integration spec item 17),
                # so a superseded run finds a dead socket by design. Raising
                # here would kill the handler thread and -- worse -- mask the
                # real exception, because the catch-all in
                # _handle_conversation_stream reports failures by writing one
                # more event down this same socket.
                pass

        return write_event

    def _handle_conversation_stream(self) -> None:
        try:
            body = self._body()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
            return

        write_event = self._open_ndjson_stream()
        turn_id = body.get("turn_id")
        seq = 0

        def stamped_write_event(event: dict) -> None:
            nonlocal seq
            seq += 1
            write_event({**event, "turn_id": turn_id, "seq": seq})

        def compile_with_progress(plan, **kwargs):
            return _compile_via_skill_service(
                plan,
                progress_callback=lambda completed, total: stamped_write_event(
                    _compile_progress_event(completed, total)),
                **kwargs,
            )

        try:
            dispatch_conversation_turn(
                body, stamped_write_event, compile_fn=compile_with_progress)
        except Exception as exc:  # noqa: BLE001 - defensive; each stream_* fn already guards
            # Best-effort diagnostics must not prevent the protocol error
            # event. A detached Windows stderr can itself raise while printing
            # the traceback; the client would then see a clean EOF with no
            # result/error and could report only a misleading generic message.
            try:
                traceback.print_exc()
            except (BrokenPipeError, OSError, ValueError):
                pass
            stamped_write_event({"type": "error", "text": str(exc)})

    def do_OPTIONS(self) -> None:
        self._send(204, {})

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/health":
            try:
                # `compound_turn` is reported because the S2 frontend HARD
                # REQUIRES it (integration spec §9): without it the only
                # symptom is a legacy-artifact error at the end of a turn,
                # which points at the wrong thing. Two orchestrators can also
                # bind this port at once on Windows (allow_reuse_address), so
                # a mismatch here is the fastest way to spot a stale instance.
                self._send(200, {
                    "ok": True,
                    "manifest": str(MANIFEST),
                    "compound_turn": os.environ.get("COMPOUND_TURN") == "1",
                    "pid": os.getpid(),
                })
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": str(exc)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        route = self.path.split("?")[0]
        if route == "/conversation/stream":
            self._handle_conversation_stream()
            return
        handler = {
            "/ground": do_ground,
            "/author": do_author,
            "/resolve_conflicts": do_resolve_conflicts,
        }.get(route)
        if handler is None:
            self._send(404, {"error": "not found"})
            return
        try:
            self._send(200, handler(self._body()))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # OpenAI/TLS/manifest are infrastructure errors
            self._send(500, {"error": str(exc)})

    def log_message(self, *_args) -> None:
        pass


def main() -> None:
    # Fail fast on a missing/invalid manifest before announcing readiness.
    load_manifest()
    print(f"[orchestrator] listening on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
