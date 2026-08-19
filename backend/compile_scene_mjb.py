"""Compile a MuJoCo XML scene to .mjb for browser WASM loading."""

from __future__ import annotations

import sys
from pathlib import Path

import mujoco

WASM_MUJOCO_VERSION = "3.10.0"


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: compile_scene_mjb.py <scene.xml> [scene.mjb]", file=sys.stderr)
        return 2

    xml_path = Path(sys.argv[1]).resolve()
    mjb_path = (
        Path(sys.argv[2]).resolve()
        if len(sys.argv) > 2
        else xml_path.with_suffix(".mjb")
    )

    if mujoco.__version__ != WASM_MUJOCO_VERSION:
        print(
            f"Expected mujoco=={WASM_MUJOCO_VERSION} (browser WASM version), "
            f"got {mujoco.__version__}",
            file=sys.stderr,
        )
        return 3

    if not xml_path.is_file():
        print(f"XML not found: {xml_path}", file=sys.stderr)
        return 4

    print(f"Compiling {xml_path} -> {mjb_path} (mujoco {mujoco.__version__})")
    model = mujoco.MjModel.from_xml_path(str(xml_path))
    mjb_path.parent.mkdir(parents=True, exist_ok=True)
    mujoco.mj_saveModel(model, str(mjb_path))
    print(f"OK ngeom={model.ngeom} nmesh={model.nmesh} bytes={mjb_path.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
