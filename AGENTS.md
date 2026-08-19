# Agent Instructions

These instructions apply to every coding agent working in this repository,
including Codex, Claude, and any delegated or automated agent.

## Test Process Cleanup

- Every process started by an agent for testing, verification, debugging, or
  local preview must be stopped before the agent finishes its task.
- This includes development servers, test watchers, Node.js processes, Python
  services, `uv` processes, browser-test servers, and all child processes they
  spawn.
- Prefer bounded foreground commands. If a background process is necessary,
  record its PID and command when it is started so the exact process tree can
  be cleaned up afterward.
- Always perform cleanup, including when a test fails, a command times out, or
  the task is interrupted. Treat cleanup like a `finally` block.
- After cleanup, verify that every port opened for the test is no longer
  listening. For this project, explicitly check ports `8787`, `8899`, and
  `8900` whenever the backend stack was started.
- Never kill unrelated Node.js or Python processes. Resolve the PID, command
  line, parent/child relationship, and repository path before terminating a
  process.
- Do not stop a server that was already running before the agent began testing
  unless the user explicitly asks for it. Distinguish user-owned processes from
  agent-started processes during the initial inspection.
- A process may remain running only when the user explicitly asks for a
  persistent server. In that case, state the PID and listening port in the final
  response.
- Before reporting completion, state that agent-started test processes were
  stopped and relevant ports were checked. Do not leave cleanup for the user.

## Study Scene Export Workflow (RoboCasa kitchens)

> Full handoff (all gotchas, verification matrix, per-scene inventory, and the
> pending layout042 object-swap task): **docs/scene_export_pipeline.md**.
> The section below is the short version.

Scenes live in `frontend/public/assets/robocasa/` as `layoutXXX_study.xml` +
compiled `.mjb`. Source configs live in the robocasa clone at
`C:\Users\madizhi\Documents\robocasa_ws\robocasa\robocasa\models\assets\scenes\custom_layouts\`
(`layoutXXX_study.yaml` + `layoutXXX_study_objects.yaml`).

Full pipeline for a new scene (run all robocasa commands from
`C:\Users\madizhi\Documents\robocasa_ws\robocasa` with `uv run`):

1. **Pick a layout**: `uv run python scripts/analyze_layout_candidates.py`
   (this repo) ranks train layouts 11-60 by yaml complexity + atomic-dataset
   articulation-demo coverage. Fridge/drawer/cabinet each need >=1 demo; a
   missing open/close direction can be filled by time-reversing its pair, a
   missing twin-fixture pair by translation retarget.
2. **Strip decorations**: `uv run python scripts/make_study_layout.py <id ...>`
   (this repo) comments out front/enclosing walls chosen for removal,
   wall/counter/floor accessories, windows, and stools, writing
   `custom_layouts/layoutXXX_study.yaml`. Further wall removals are manual
   edits (comment out the wall AND its `_backing` entry; check nothing
   references removed fixtures via `align_to`/`attach_to`/`ref`).
3. **Object config** (DISCUSS combos with the user first — study-design
   decision): `uv run python tools/generate_objects_yaml.py --layout-yaml
   .../layoutXXX_study.yaml --spec "cat=instance:2,cat2,..." --seed N --output
   .../layoutXXX_study_objects.yaml`. Study structure: 3 sink objects (cups,
   two identical + one different), 3 fridge objects (fruit/veg, 2+1), 3
   cabinet/drawer objects (canned food / condiment / cereal). Pinned instances
   must come from the `objaverse`/`lightwheel` registries (aigen paths crash
   the exporter's reverse lookup). Placements carry
   `sample_region_kwargs: {full_depth_region: true}` — without it, islands
   with an interior sink sample objects onto the sink-side strip regions,
   clustering them out of robot reach.
4. **Export**: `uv run python -m robocasa.scripts.export_kitchen_scene
   --layout-yaml <study.yaml> --objects-yaml <objects.yaml> --style <id>
   --extra-pandaomrons 1 --flat-wall --web-assets
   --auto-place-primary-pandaomron --output
   <frontend/public/assets/robocasa/layoutXXX_study.xml>`.
   The exporter fails nondeterministically sometimes — retry the identical
   command up to 3 times before debugging. Formal scene styles: 012=18,
   024 sorting=45 (matches OpenFridge ep88 / Refrigerator040; legacy
   layout024_study remains 46), 038=58, 042=51.
5. **Downscale textures (ALWAYS after every export batch, BEFORE compiling
   .mjb)**: `uv run --with pillow python scripts/downscale_scene_textures.py`
   (this repo). Exports re-copy full-size textures into the web assets; without
   this step scene .mjb files exceed ~400MB and mujoco-react fails to load
   them. Target is <=512px, giving ~4x texture memory reduction.
6. **Compile .mjb + verify**: load the XML with `mujoco==3.10.0` (the
   frontend's version), `mujoco.mj_saveModel` next to the XML, and sanity-check
   free-joint object world positions / render a top-down preview. Do NOT
   recompile `layout042_study.mjb` naively — it has the baked `study_init`
   keyframe from `bake_study_init.py`.

Old/unused scene exports are archived in `archive/robocasa_scenes/`, not
deleted.
