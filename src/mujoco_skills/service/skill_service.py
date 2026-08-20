r"""Warm HTTP service exposing the skill/plan layer to the frontend and LLM.

Loads the scene model + standoffs + ready pose + manifest ONCE (mujoco load is
slow), then answers compile/standoff requests in milliseconds. These endpoints
are the tools the LLM co-authoring loop calls (see service/llm_tools.json):

  POST /compile_plan   body: {"plan": <authoring plan>}
       -> {"schedule": [{robot,label,start,duration,facility,object,group,
                          op,motion,is_last_in_robot,track_url}],
           "warnings": [...], "conflicts": [...], "completed": {robot:[steps...]},
           "rest_points": {robot: [x,y]}}
       (also writes immutable plan-hash-namespaced generated tracks under
        tracks/_generated/ so cached responses keep valid track_url targets)
  POST /standoff       body: {"target_xy": [x,y]}   (optional "working_q")
       -> standoff_for_point result {feasible, standoff_xy, face_xy, reason}
  GET  /manifest       -> the skills manifest (object/facility/skill vocabulary)

Run:
    uv run --with mujoco==3.10.0 python -m mujoco_skills.service.skill_service   # port 8899
    (set SKILL_SERVICE_PORT / MUJOCO_REACT_PUBLIC_DIR /
     MUJOCO_REACT_SCENE_PATH to override)
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

# The backend starts this module directly with ``uv run``. Load the repository
# .env before importing skill_generators, whose compile-log mode is configured
# from the environment at import time.
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from mujoco_skills.skills import skill_generators as sg
from mujoco_skills.pipeline.build_navigation_grid import navigation_grid_paths
from mujoco_skills.service.scene_runtime import scene_runtime_from_env

RUNTIME = scene_runtime_from_env(
    default_public=Path(__file__).resolve().parents[3] / "frontend/public"
)
PUBLIC = RUNTIME.public
SCENE = str(RUNTIME.scene_xml)
STUDY = RUNTIME.study
STUDY_NAME = RUNTIME.study_name
TRACKS = RUNTIME.tracks
STANDOFFS = RUNTIME.standoffs
MANIFEST = RUNTIME.manifest
PORT = int(os.environ.get("SKILL_SERVICE_PORT", 8899))
HOST = os.environ.get("SKILL_SERVICE_HOST", "127.0.0.1")
SNAPSHOT_TTL_SECONDS = float(os.environ.get("COMPILE_SNAPSHOT_TTL", "300"))
SNAPSHOT_MAX_ENTRIES = int(os.environ.get("COMPILE_SNAPSHOT_MAX", "32"))
GENERATED_TRACK_NAMESPACE_LENGTH = 32


def active_compiler_version():
    version = os.environ.get("COMPILER_VERSION", "v1").strip().lower()
    if version not in ("v1", "v2"):
        raise ValueError(f"unsupported COMPILER_VERSION {version!r}")
    return version


def _compile_log(message):
    print(f"[compile-request] {message}", file=sys.stderr, flush=True)


def _compile_memo_log_suffix(result):
    """Render Compiler V2's request-local memo counters for the done log."""
    compiler_v2 = result.get("compiler_v2") if isinstance(result, dict) else None
    memo = compiler_v2.get("memo") if isinstance(compiler_v2, dict) else None
    if not isinstance(memo, dict):
        return ""
    try:
        hits = int(memo.get("hits", 0))
        misses = int(memo.get("misses", 0))
        uncacheable = int(memo.get("uncacheable", 0))
    except (TypeError, ValueError):
        return ""
    considered = hits + misses
    rate = (hits / considered * 100.0) if considered else 0.0
    mode = str(memo.get("mode", "unknown"))
    return (
        f" memo={mode} hits={hits}/{considered} ({rate:.0f}%) "
        f"uncacheable={uncacheable}"
    )


def canonical_plan_key(plan):
    """Stable semantic-exact key across nested and flat plan representations.

    ``flatten_tasks`` materialises author/robot order before JSON key sorting,
    so insertion order that affects shared-container compilation remains part
    of the key while resolver-flat and frontend-nested forms can share cache.
    """
    normalized = sg.flatten_tasks(plan)
    for steps in normalized.values():
        for step in steps:
            step.setdefault("robot_locked", False)
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compiler_generation():
    """Identity of the scene/compiler inputs used by compile snapshots.

    Includes TRACKS (D7): base track files feed compiled output (Open/Close
    replays, the navigate lookahead that previews a following skill's entry
    pose) exactly as much as the scene/standoffs/manifest do, but were not
    previously part of this identity, so editing one wouldn't have busted a
    stale snapshot. ``sg.tracks_fingerprint`` is the same helper the per-step
    compile memo in skill_generators.py uses for its own generation value —
    shared rather than duplicated so the two identities can't drift apart.
    """
    navgrid = navigation_grid_paths(SCENE)
    inputs = (
        Path(SCENE), STANDOFFS, MANIFEST,
        navgrid["npz"], navgrid["json"], Path(sg.__file__),
    )
    material = []
    for path in inputs:
        try:
            stat = path.stat()
            material.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            material.append((str(path.resolve()), None, None))
    material.append(sg.tracks_fingerprint(TRACKS))
    material.append(("compiler_version", active_compiler_version()))
    return hashlib.sha256(
        json.dumps(material, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class CompileSnapshotRegistry:
    """Bounded TTL/LRU storage for Resolver's initial observation only.

    This deliberately is not consulted by ``CompileCoordinator.run``: every
    compile request and every repair verification remains a fresh compile.
    """

    def __init__(self, *, ttl_seconds=300.0, max_entries=32, clock=None):
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._clock = clock or time.monotonic
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    def put(self, compile_id, plan_hash, generation, result, *,
            completed_plan_hash=None):
        now = self._clock()
        record = {
            "compile_id": compile_id,
            "plan_hash": plan_hash,
            # Resolver receives the compiler-completed flat plan, which can
            # legitimately have a different canonical key from the authored
            # nested request (compiler-filled fields and cross-robot order).
            "completed_plan_hash": completed_plan_hash or plan_hash,
            "generation": generation,
            "created_at": now,
            "result": copy.deepcopy(result),
        }
        with self._lock:
            self._prune(now)
            self._entries[compile_id] = record
            self._entries.move_to_end(compile_id)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def get(self, compile_id, *, plan_hash=None, completed_plan_hash=None,
            generation):
        now = self._clock()
        with self._lock:
            self._prune(now)
            record = self._entries.get(compile_id)
            if record is None:
                return None
            expected_hash = (
                record["completed_plan_hash"]
                if completed_plan_hash is not None
                else record["plan_hash"]
            )
            supplied_hash = (
                completed_plan_hash
                if completed_plan_hash is not None
                else plan_hash
            )
            if (expected_hash != supplied_hash
                    or record["generation"] != generation):
                return None
            self._entries.move_to_end(compile_id)
            return copy.deepcopy(record)

    def _prune(self, now):
        expired = [
            key for key, value in self._entries.items()
            if now - value["created_at"] > self.ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)


def _warm():
    # prime the rig cache + config so the first request is fast too
    island = json.loads(STANDOFFS.read_text("utf-8"))["island_bbox"]
    ready = sg.load_ready(TRACKS)
    for r in (0, 1):
        sg.get_rig(SCENE, r, ready, island)
    print(f"[skill_service] warm: scene={Path(SCENE).name} rigs=robot0,robot1")


def _compile_uncached(plan, plan_key, *, progress_callback=None):
    # Reject invalid candidate dependencies before entering the MuJoCo compiler.
    # This is intentionally the same topology builder compile_plan uses as its
    # in-process defence, so manual Gantt edits and resolver candidates cannot
    # be checked against a weaker graph than the eventual compile.
    version = active_compiler_version()
    compile_plan_input = plan
    preparation_report = None
    if version == "v2":
        from mujoco_skills.orchestrator.compiler_v2_plan import (
            prepare_compiler_v2_input,
        )
        compile_plan_input, preparation_report = prepare_compiler_v2_input(plan)
    sg.validate_plan_topology(compile_plan_input, MANIFEST)
    if version == "v2":
        compile_kwargs = {"scheduler_mode": "v2"}
        if progress_callback is not None:
            compile_kwargs["progress_callback"] = progress_callback
        result = sg.compile_plan(
            SCENE, compile_plan_input, STANDOFFS, TRACKS, MANIFEST,
            **compile_kwargs,
        )
    else:
        result = sg.compile_plan(
            SCENE, compile_plan_input, STANDOFFS, TRACKS, MANIFEST)
    # Generated labels repeat across plans. Keep immutable plan-keyed files so
    # an exact-cache response can never point at tracks overwritten by a later
    # compile with the same labels.
    # A full 64-character SHA-256 namespace plus a descriptive physics-aware
    # articulation filename can exceed Win32's legacy 260-character path
    # limit. Use a 128-bit namespace and let generated_track_filename compact
    # an unusually long label without changing the user-visible schedule name.
    namespace_key = (
        hashlib.sha256(f"{plan_key}|v2".encode("utf-8")).hexdigest()
        if version == "v2" else plan_key
    )
    track_namespace = namespace_key[:GENERATED_TRACK_NAMESPACE_LENGTH]
    sg.write_generated_tracks(
        result, TRACKS, namespace=track_namespace)
    last_order = {}
    for it in result["items"]:
        last_order[it["robot"]] = max(
            last_order.get(it["robot"], it["order"]), it["order"])
    schedule = []
    for it in result["items"]:
        completed_step = it.get("completed_step") or {}
        op = completed_step.get("op")
        schedule.append({
            # id + after let the UI map a Gantt bar back to its authored step
            # (for editing) and show current dependencies. op/motion/
            # is_last_in_robot feed the conflict-resolution scheduler's
            # per-round payload (design doc §4) without it re-deriving them.
            "id": it["id"], "after": it.get("after", []),
            "robot": it["robot"], "label": it["label"], "start": it["start"],
            "duration": it["duration"], "facility": it["facility"],
            "object": it["object"], "group": it["group"],
            "op": op,
            "motion": "moving" if op in sg.MOVING_OPS else "dwelling",
            "is_last_in_robot": it["order"] == last_order[it["robot"]],
            "robot_locked": bool(
                completed_step.get("robot_locked", False)),
            # Compiler-generated detours remain separate execution steps, but
            # the task view groups them into the semantic task they temporarily
            # leave and resume. Their geometry stays independently editable.
            "source": completed_step.get("source"),
            "repair_kind": completed_step.get("compiler_v2_repair"),
            "parent_group": completed_step.get("compiler_v2_parent_group"),
            "detour_overridden": bool(
                completed_step.get("compiler_v2_detour_overridden", False)),
            "track_url": (
                f"/trajectories/{STUDY_NAME}/tracks/{sg.GENERATED_TRACK_DIR}/"
                f"{track_namespace}/{it['robot']}/"
                f"{sg.generated_track_filename(it['label'])}"
                if sg.is_generated_track_item(it)
                else f"/trajectories/{STUDY_NAME}/tracks/{it['robot']}/{it['label']}.track.json"
            ),
        })
    response = {"schedule": schedule, "warnings": result["warnings"],
            "conflicts": result.get("conflicts", []),
            "completed": result["completed"],
            "rest_points": result.get("rest_points", {}),
            # Deterministic resolver tools consume these fields directly.
            # conflict_payload intentionally omits both from the LLM message.
            "scene_xml": str(Path(SCENE).resolve()),
            "base_timelines": sg.serialize_base_occupancy_timelines(
                result["items"]),
            }
    if result.get("compiler_v2") is not None:
        response["compiler_v2"] = {
            **result["compiler_v2"],
            "preparation": preparation_report,
        }
    return response


class CompileCoordinator:
    """Serialize mutable MuJoCo access and instrument every compilation.

    This coordinator never reads a result cache. The service stores its output
    separately only as a short-lived Resolver round-0 snapshot; every ordinary
    compile request and repair verification still invokes the compiler afresh.
    """

    def __init__(self, compiler, *, execution_lock=None):
        self._compiler = compiler
        self._execution_lock = execution_lock or threading.Lock()
        self._request_ids = itertools.count(1)

    def run(self, plan, *, progress_callback=None):
        requested_at = time.perf_counter()
        request_id = f"c{next(self._request_ids):06d}"
        plan_key = canonical_plan_key(plan)
        short_key = plan_key[:12]

        try:
            with self._execution_lock:
                compile_started = time.perf_counter()
                queue_wait = compile_started - requested_at
                cpu_started = time.process_time()
                _compile_log(
                    f"request={request_id} plan={short_key} status=start "
                    f"queue={queue_wait:.3f}s active=1")
                with sg.compile_profile_context(request_id, plan_key):
                    if progress_callback is None:
                        result = self._compiler(plan, plan_key)
                    else:
                        result = self._compiler(
                            plan, plan_key,
                            progress_callback=progress_callback)
                compile_wall = time.perf_counter() - compile_started
                cpu_time = time.process_time() - cpu_started

            _compile_log(
                f"request={request_id} plan={short_key} status=done "
                f"compile_wall={compile_wall:.3f}s cpu={cpu_time:.3f}s "
                f"total_wall={time.perf_counter() - requested_at:.3f}s"
                f"{_compile_memo_log_suffix(result)}")
            return result
        except BaseException as exc:
            # Keep the terminal log single-line while preserving actionable
            # compiler details such as the exact dependency-cycle path.
            error_detail = " ".join(str(exc).split()) or "<no message>"
            _compile_log(
                f"request={request_id} plan={short_key} status=error "
                f"total_wall={time.perf_counter() - requested_at:.3f}s "
                f"error={type(exc).__name__} "
                f"detail={json.dumps(error_detail, ensure_ascii=False)}")
            raise


# compile and standoff both mutate cached SceneRig.data/qpos. They must share
# one lock even though health/manifest endpoints remain concurrent.
_MUJOCO_EXECUTION_LOCK = threading.Lock()
_COMPILE_COORDINATOR = CompileCoordinator(
    _compile_uncached,
    execution_lock=_MUJOCO_EXECUTION_LOCK,
)
_COMPILE_SNAPSHOTS = CompileSnapshotRegistry(
    ttl_seconds=SNAPSHOT_TTL_SECONDS,
    max_entries=SNAPSHOT_MAX_ENTRIES,
)
_SNAPSHOT_IDS = itertools.count(1)


def do_compile(plan, *, retain_snapshot=True, progress_callback=None):
    result = (
        _COMPILE_COORDINATOR.run(plan)
        if progress_callback is None
        else _COMPILE_COORDINATOR.run(
            plan, progress_callback=progress_callback)
    )
    if not retain_snapshot:
        return {
            **result,
            "compiler_generation": compiler_generation(),
        }
    compile_id = f"s{next(_SNAPSHOT_IDS):06d}"
    plan_hash = canonical_plan_key(plan)
    completed_plan_hash = canonical_plan_key(result["completed"])
    generation = compiler_generation()
    _COMPILE_SNAPSHOTS.put(
        compile_id,
        plan_hash,
        generation,
        result,
        completed_plan_hash=completed_plan_hash,
    )
    return {
        **result,
        "compile_id": compile_id,
        "compiler_generation": generation,
    }


def do_validate_plan(plan):
    """Topology-only plan validation; never enters CompileCoordinator/MuJoCo."""
    if not isinstance(plan, dict):
        return {
            "ok": False,
            "error": {
                "code": "invalid_plan",
                "message": "plan must be an object",
                "cycle": None,
                "facility": None,
            },
        }
    try:
        sg.validate_plan_topology(plan, MANIFEST)
    except sg.TopologyError as exc:
        return {"ok": False, "error": exc.as_dict()}
    except ValueError as exc:
        return {
            "ok": False,
            "error": {
                "code": "invalid_plan_topology",
                "message": str(exc),
                "cycle": None,
                "facility": None,
            },
        }
    return {"ok": True}


def do_compile_snapshot(body):
    compile_id = body.get("compile_id")
    plan = body.get("plan")
    if not isinstance(compile_id, str) or not isinstance(plan, dict):
        raise ValueError("compile_id and plan are required")
    record = _COMPILE_SNAPSHOTS.get(
        compile_id,
        completed_plan_hash=canonical_plan_key(plan),
        generation=compiler_generation(),
    )
    if record is None:
        raise KeyError("compile snapshot missing, expired, or stale")
    return {
        **record["result"],
        "compile_id": record["compile_id"],
        "compiler_generation": record["generation"],
    }


def do_standoff(body):
    with _MUJOCO_EXECUTION_LOCK:
        rig = sg.get_rig(SCENE, 0)
        working_q = body.get("working_q")
        import numpy as np
        wq = np.array(working_q) if working_q is not None else None
        return sg.standoff_for_point(rig, body["target_xy"], working_q=wq)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _open_ndjson_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.close_connection = True

        def write_event(event):
            try:
                self.wfile.write(
                    json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                pass

        return write_event

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/manifest":
                self._send(200, json.loads(MANIFEST.read_text("utf-8")))
            elif path == "/health":
                self._send(200, {"ok": True, "scene": Path(SCENE).name})
            else:
                self._send(404, {"error": "not found"})
        except sg.IncrementalCompileConflict as e:
            self._send(422, {"error": str(e), "detail": e.as_dict()})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self._body()
            if path == "/compile_plan_stream":
                write_event = self._open_ndjson_stream()
                try:
                    result = do_compile(
                        body["plan"],
                        retain_snapshot=bool(body.get("retain_snapshot", True)),
                        progress_callback=lambda completed, total: write_event({
                            "type": "progress",
                            "completed": completed,
                            "total": total,
                        }),
                    )
                except sg.IncrementalCompileConflict as exc:
                    write_event({
                        "type": "error", "status": 422,
                        "error": str(exc), "detail": exc.as_dict(),
                    })
                except Exception as exc:  # noqa: BLE001
                    write_event({
                        "type": "error", "status": 400,
                        "error": str(exc),
                    })
                else:
                    write_event({"type": "result", "result": result})
            elif path == "/compile_plan":
                self._send(200, do_compile(
                    body["plan"],
                    retain_snapshot=bool(body.get("retain_snapshot", True)),
                ))
            elif path == "/validate_plan":
                self._send(200, do_validate_plan(body.get("plan")))
            elif path == "/compile_snapshot":
                self._send(200, do_compile_snapshot(body))
            elif path == "/standoff":
                self._send(200, do_standoff(body))
            else:
                self._send(404, {"error": "not found"})
        except sg.IncrementalCompileConflict as e:
            self._send(422, {"error": str(e), "detail": e.as_dict()})
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": str(e)})

    def log_message(self, *_):
        pass  # quiet


if __name__ == "__main__":
    _warm()
    print(f"[skill_service] listening on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
