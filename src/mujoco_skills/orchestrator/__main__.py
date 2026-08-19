"""One-shot CLI for the agentic authoring -> decompose -> compile pipeline.

    python -m mujoco_skills.orchestrator "move mug_1 to the sink"

Flow: semantic authoring loop -> confirm -> deterministic decompose ->
compile_plan.

Requires OPENAI_API_KEY set and skill_service running on :8899
(python -m mujoco_skills.service.skill_service).
"""

from __future__ import annotations

import argparse
import json
import sys

import truststore
from dotenv import load_dotenv

# Windows consoles default to cp1252, which cannot encode Chinese / emoji in the
# model's replies (UnicodeEncodeError on print). Force UTF-8 on our streams.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# Use the OS trust store (Windows cert store) so corporate TLS-inspection root
# CAs are trusted — certifi's bundle alone fails behind a MITM proxy. Must run
# before any HTTPS client is created.
truststore.inject_into_ssl()

from mujoco_skills.orchestrator import authoring, decompose, skill_client
from mujoco_skills.orchestrator.providers.openai_provider import OpenAIProvider
from mujoco_skills.orchestrator.schema import AugmentedAction


def _print_event(kind: str, payload: object) -> None:
    if kind == "tool_call":
        print(f"  -> tool: {payload.name}({json.dumps(payload.arguments)[:400]})", file=sys.stderr)
    elif kind == "tool_result":
        name, result = payload
        head = json.dumps(result)[:400]
        print(f"  <- {name}: {head}", file=sys.stderr)
    elif kind == "max_iters":
        print(f"  ! hit max_iters={payload}; stopping", file=sys.stderr)
    elif kind == "no_tasks_reason":
        print(f"  (stage 1) could not ground any tasks: {payload}", file=sys.stderr)


def _confirm_plan(actions: list[AugmentedAction]) -> list[AugmentedAction] | None:
    """Inline confirm/edit/cancel. Returns the (possibly trimmed) action list,
    or None if the user cancelled."""
    _print_actions(actions)
    while True:
        resp = input("[Enter]=confirm / e=edit (delete by number) / q=cancel: ").strip().lower()
        if resp == "":
            return actions
        if resp == "q":
            return None
        if resp == "e":
            raw = input("numbers to remove (space/comma separated): ").strip()
            remove = {int(x) for x in raw.replace(",", " ").split() if x.strip().lstrip("-").isdigit()}
            actions = [action for i, action in enumerate(actions, 1) if i not in remove]
            if not actions:
                print("no actions left; cancelling.")
                return None
            _print_actions(actions)
            continue
        print("unrecognized input.")


def _print_actions(actions: list[AugmentedAction]) -> None:
    print("Augmented actions:")
    for action in actions:
        detail = (
            f"{action.object} -> {action.dest}"
            if action.op == "move"
            else action.facility or action.target or ""
        )
        serves = f" serves={action.serves}" if action.serves else ""
        print(f"  {action.id}. {action.robot} {action.op} {detail}{serves}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mujoco_skills.orchestrator")
    ap.add_argument("request", help="natural-language authoring request")
    ap.add_argument("--model", default=None, help="override OPENAI_MODEL")
    ap.add_argument("--quiet", action="store_true", help="suppress tool trace on stderr")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt (auto-confirm the semantic plan)")
    args = ap.parse_args(argv)

    # Load .env from the repo root (searched upward from cwd) so OPENAI_API_KEY
    # need not be exported manually. Real env vars still take precedence.
    load_dotenv()

    if not skill_client.health():
        print(
            f"skill_service not reachable at {skill_client.BASE_URL}. Start it:\n"
            f"  uv run --with mujoco==3.10.0 python -m mujoco_skills.service.skill_service",
            file=sys.stderr,
        )
        return 2

    provider = OpenAIProvider(model=args.model) if args.model else OpenAIProvider()
    on_event = None if args.quiet else _print_event

    # The manifest is context for semantic authoring, but is not exposed as a
    # retrieval tool. The loop's only tools are augment and propose_plan.
    manifest = skill_client.execute("get_manifest", {})
    if "error" in manifest:
        print(f"failed to load manifest: {manifest['error']}", file=sys.stderr)
        return 2

    authored = authoring.author(
        messages=[{"role": "user", "content": args.request}],
        current_plan=[],
        provider=provider,
        manifest=manifest,
        on_event=on_event,
    )
    actions = authored.actions
    if not actions:
        print(f"No groundable plan: {authored.reason or authored.message}")
        return 0

    print(f"Authoring: {authored.message}")
    if args.yes:
        _print_actions(actions)
        confirmed = actions
    else:
        confirmed = _confirm_plan(actions)
        if confirmed is None:
            print("cancelled.")
            return 0

    plan = decompose.decompose(confirmed, manifest)
    result = skill_client.execute("compile_plan", {"plan": plan})
    if "error" in result:
        print(f"compile failed: {result['error']}", file=sys.stderr)
        return 2
    print("Schedule:")
    print(json.dumps(result.get("schedule", []), ensure_ascii=False, indent=2))
    warnings = result.get("warnings", [])
    print(f"Warnings ({len(warnings)}):")
    print(json.dumps(warnings, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
