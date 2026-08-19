"""Resolve scene-scoped compiler artifacts from the active viewer scene."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


DEFAULT_SCENE_RELATIVE = Path("assets/robocasa/layout042_study.xml")


@dataclass(frozen=True)
class SceneRuntime:
    public: Path
    scene_xml: Path
    study: Path
    study_name: str
    tracks: Path
    standoffs: Path
    manifest: Path


def resolve_scene_runtime(
    public: Path,
    configured_scene: str | os.PathLike[str] | None = None,
) -> SceneRuntime:
    """Derive the XML, manifest, standoffs, and tracks for one active scene."""
    public = Path(public).resolve()
    raw_scene = Path(configured_scene) if configured_scene else DEFAULT_SCENE_RELATIVE
    scene_path = raw_scene if raw_scene.is_absolute() else public / raw_scene
    scene_path = scene_path.resolve()
    suffix = scene_path.suffix.lower()
    if suffix not in {".xml", ".mjb"}:
        raise ValueError("MUJOCO_REACT_SCENE_PATH must point to an .xml or .mjb file")
    scene_xml = scene_path.with_suffix(".xml") if suffix == ".mjb" else scene_path
    study_name = scene_xml.stem
    study = public / "trajectories" / study_name
    return SceneRuntime(
        public=public,
        scene_xml=scene_xml,
        study=study,
        study_name=study_name,
        tracks=study / "tracks",
        standoffs=study / "standoffs.json",
        manifest=study / "skills_manifest.json",
    )


def scene_runtime_from_env(
    *,
    environ: Mapping[str, str] | None = None,
    default_public: Path | None = None,
) -> SceneRuntime:
    env = os.environ if environ is None else environ
    public = Path(
        env.get(
            "MUJOCO_REACT_PUBLIC_DIR",
            str(default_public or Path(__file__).resolve().parents[3] / "frontend/public"),
        )
    )
    return resolve_scene_runtime(public, env.get("MUJOCO_REACT_SCENE_PATH"))
