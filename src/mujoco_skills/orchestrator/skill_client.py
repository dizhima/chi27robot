"""Executes tool calls against the running skill_service (default :8899).

The orchestrator does NOT start skill_service — it assumes the warm service is
already up (start it with `python -m mujoco_skills.service.skill_service`). Tool
errors are returned as {"error": ...} so they can be fed back to the model to
recover; only a broken connection to the service raises.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ.get("SKILL_SERVICE_URL",
                          f"http://127.0.0.1:{os.environ.get('SKILL_SERVICE_PORT', 8899)}")


def health() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=5) as r:
            return json.loads(r.read()).get("ok", False)
    except (urllib.error.URLError, OSError):
        return False


def execute(name: str, arguments: dict) -> dict:
    """Run one tool call; return a JSON-able result (or {"error": ...})."""
    if name == "get_manifest":
        return _get("/manifest")
    if name == "standoff_for_point":
        return _post("/standoff", arguments)
    if name == "compile_plan":
        # skill_service expects {"plan": <plan>}; the tool arg already nests it.
        return _post("/compile_plan", arguments)
    return {"error": f"unknown tool: {name}"}


def _get(path: str) -> dict:
    try:
        with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # A tool-level error (e.g. 4xx/5xx with {"error":...}) — feed back, don't raise.
        return _http_error(e)


def _post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return _http_error(e)


def _http_error(e: urllib.error.HTTPError) -> dict:
    try:
        return json.loads(e.read())  # skill_service returns {"error": ...}
    except Exception:  # noqa: BLE001
        return {"error": f"HTTP {e.code} from skill_service"}
