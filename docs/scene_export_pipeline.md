# Study Scene Export & Skill Pipeline — Full Handoff

Everything known (as of 2026-08-12) about producing a RoboCasa study scene for
this project: from layout selection to a compiling schema-v2 skills manifest.
Written so a fresh agent can build a new scene or reproduce the completed
layout042_sorting case study in §9 without any other context. Complements
`AGENTS.md` ("Study Scene Export Workflow", the short version) and
`docs/scene_manifest_generation_handoff.md` (the manifest-v2 design rationale
— already implemented).

## 1. Two repositories

- **This repo** (`C:\Users\madizhi\Documents\mujoco-skill-playground`): system
  code, pipeline scripts, frontend/backend, generated artifacts.
- **RoboCasa workspace** (`C:\Users\madizhi\Documents\robocasa_ws\robocasa`):
  upstream clone with local modifications (uncommitted; a fork is planned).
  Run every `robocasa.*` command from THIS directory with `uv run`.
  Local modifications so far: `scripts/export_kitchen_scene.py`
  (`_make_walls_flat` shared-texture guard), `tools/generate_objects_yaml.py`
  (registry-aware instance resolution + `full_depth_region`), custom layouts
  under `robocasa/models/assets/scenes/custom_layouts/`.

## 2. Data ownership and reproducibility layers

| Layer | Owns | Rebuild / preservation rule |
|---|---|---|
| RoboCasa `layout*.yaml` + `*_objects.yaml` | reproducible source layout, object registry instances, normalized placement regions, exporter seed inputs | exporter input only; it does **not** preserve later manual world-qpos edits |
| `frontend/public/assets/robocasa/layoutXXX_*.xml` / `.mjb` | frozen formal scene: bodies/joints/geoms, robot mounts, object free-joint qpos, `study_init` keyframe | after manual XML pose edits, treat the XML as formal truth and do not casually re-export over it |
| `layoutXXX_*.scene_table.json` (same dir, hand-authored) | semantic IDs/labels, object/facility body bindings, articulation grouping, `place` config incl. per-fixture `motion` overrides + `support_geom`, pins | contains scene-specific semantics, never per-frame arrays or values derivable from the model |
| `trajectories/layoutXXX_*/robot{0,1}/*.json` + `tracks/` | raw full-qpos replays and canonical name-addressed tracks, including reverse/retarget/release synthesis | raw dumps are scene-state-bound; canonical tracks are rebuilt from them and post-processed |
| `trajectories/layoutXXX_*/skills_manifest.json` (generated, schema v2) | complete runtime contract: XML hash/model signature, robots+mounts, objects/facilities, standoffs, placement config, skills, navigation, state model, validation | regenerate after XML, scene table, standoff, or canonical-track changes |

Golden rule: **`skill_generators.py` contains zero concrete fixture/body names
or scene mounts.** A new scene = XML/mjb + scene_table + tracks. If you find
yourself editing skill_generators for a scene, stop — put it in the scene
table and regenerate the manifest.

For manually curated formal scenes, also record the layout id, style, object
spec, seed, exporter command, source-YAML hashes, and final overridden object
qpos in a build note or sidecar. A final XML that cannot be reproduced from
the source YAML without undocumented hand edits is a fragile artifact.

## 3. Pipeline scripts (all parameterized; 042 paths are only defaults)

In this repo:

| Script | Purpose |
|---|---|
| `scripts/analyze_layout_candidates.py` | rank train layouts 11–60 by yaml complexity + atomic-demo coverage |
| `scripts/make_study_layout.py <ids>` | strip decorations from official layout yaml → `custom_layouts/layoutXXX_study.yaml` |
| `scripts/downscale_scene_textures.py` | resize web-asset textures to ≤512px (MANDATORY after every export, BEFORE compiling mjb) |
| `src/mujoco_skills/pipeline/extract_skill_tracks.py` | raw dumps → name-addressed tracks; `--reverse robot:SKILL:NEW` (time-reverse open↔close), `--retarget robot:SKILL:SRC_J:DST_J:NEW` (twin-fixture translation); writes `meta.recording` (mount, scene_id, model_signature) + `entry/exit_base_pose` |
| `src/mujoco_skills/pipeline/append_openfridge_release.py` | collision-check and append a target-scene Cartesian handle-release tail; parameterized by `--scene` + `--tracks-dir` |
| `src/mujoco_skills/pipeline/migrate_track_mounts.py` | one-time stamp of recording metadata onto legacy tracks (explicit `--mount` values; never guesses) |
| `src/mujoco_skills/pipeline/bake_study_init.py` | bake `study_init` keyframe (ready arms from `--ready-from`, default robot0/OpenFridge; `--init-open robot/SKILL` sets a fixture open from a Close demo's frame 0) + recompiles the sibling .mjb |
| `src/mujoco_skills/pipeline/calibrate_standoffs.py` | `--scene <xml>`; auto-discovers targets (free-joint objects + sink) and island prefix → `standoffs.json` + floorplan png |
| `src/mujoco_skills/pipeline/build_skills_manifest.py` | schema-v2 manifest generator with JSON-path validation and atomic write |
| `src/mujoco_skills/pipeline/build_navigation_grid.py` | navgrid (`.navgrid.json/.npz` next to the xml) |

In robocasa_ws: `tools/generate_objects_yaml.py` (object placement yaml from
`--spec`), `robocasa.scripts.export_kitchen_scene` (scene export),
`robocasa.scripts.dataset_scripts.replay_atomic_on_scene` (replay demos).

## 4. Full pipeline for a NEW scene

```bash
# 0) pick a layout (complexity + demo coverage; fridge/drawer/cabinet each need >=1 demo;
#    a missing open/close direction is synthesized by --reverse, a missing twin pair by --retarget)
uv run python scripts/analyze_layout_candidates.py

# 1) strip decorations (front/enclosing walls, accessories, windows, stools)
uv run python scripts/make_study_layout.py 12
#    extra wall removals = manual: comment out the wall AND its *_backing entry;
#    check nothing references removed fixtures via align_to/attach_to/ref.

# 2) objects yaml — DISCUSS the object combos with the user first (study-design decision).
#    Current study structure: 3 sink cups (2 identical + 1 different), 3-4 fridge fruit/veg
#    (2+1 or 2+2), 3 cabinet/drawer pantry items (cereal/canned_food/condiment_bottle).
cd C:\Users\madizhi\Documents\robocasa_ws\robocasa
uv run python tools/generate_objects_yaml.py \
  --layout-yaml robocasa/models/assets/scenes/custom_layouts/layout012_study.yaml \
  --spec "mug=mug_3:2,cup=cup_2,bowl=bowl_1,apple=apple_2:2,banana=banana_1:2,cereal=cereal_0,canned_food=canned_food_0,condiment_bottle=condiment_0" \
  --seed 8 --output robocasa/models/assets/scenes/custom_layouts/layout012_study_objects.yaml

# 3) export (retry identical command up to 3x — the exporter fails nondeterministically)
uv run python -m robocasa.scripts.export_kitchen_scene \
  --layout-yaml .../layout012_study.yaml --objects-yaml .../layout012_study_objects.yaml \
  --style 18 --extra-pandaomrons 1 --flat-wall --web-assets --auto-place-primary-pandaomron \
  --output <playground>/frontend/public/assets/robocasa/layout012_study.xml

# 4) ALWAYS: downscale textures (exports re-copy full-size ones; >400MB mjb OOMs mujoco-react)
uv run --with pillow python scripts/downscale_scene_textures.py

# 5) reachability gate BEFORE running replays: every object must have a feasible standoff.
#    If any is INFEASIBLE, change --seed in step 2 and re-export (loop 2-5).
uv run --with mujoco==3.10.0 --with matplotlib python -m mujoco_skills.pipeline.calibrate_standoffs \
  --scene frontend/public/assets/robocasa/layout012_study.xml

# 6) replays (per robot 0 and 1, per skill). episode-index = position in the sorted
#    episode_ids list of (task, layout) in atomic_layout_summary.json.
uv run python -m robocasa.scripts.dataset_scripts.replay_atomic_on_scene \
  --scene-xml <scene.xml> --layout 12 --task OpenFridge --episode-index 1 \
  --robot-index 0 --no-viewer \
  --trajectory-json <playground>/frontend/public/trajectories/layout012_study/robot0/layout012_study_OpenFridge.json

# 7) extract tracks (+ synthesis). Writes meta.recording/entry/exit automatically.
uv run --with mujoco==3.10.0 python -m mujoco_skills.pipeline.extract_skill_tracks \
  --scene <scene.xml> --input-dir <trajectories/layout012_study> \
  --output-dir <trajectories/layout012_study/tracks> \
  --reverse robot0:OpenDrawer:CloseDrawer --reverse robot1:OpenDrawer:CloseDrawer

# 7a) if OpenFridge ends with the hand intersecting the style-variant door,
#     append a scene-solved release tail (dry-run first, then --apply). This
#     must be repeated after every re-extraction because extraction overwrites
#     canonical tracks.
uv run --with mujoco==3.10.0 python -m mujoco_skills.pipeline.append_openfridge_release \
  --scene <scene.xml> --tracks-dir <tracks> --apply

# 8) scene table: hand-author layoutXXX_study.scene_table.json (copy 012's as template;
#    schema notes in 042's _comment). Enumerate body names from the compiled model.

# 9) bake study_init (+ mjb recompile). 042 additionally needs --init-open robot0/CloseCabinet.
uv run --with mujoco==3.10.0 python -m mujoco_skills.pipeline.bake_study_init \
  --scene <scene.xml> --tracks-dir <tracks>

# 10) standoffs again (final state), navgrid, manifest
uv run --with mujoco==3.10.0 --with matplotlib python -m mujoco_skills.pipeline.calibrate_standoffs --scene <scene.xml>
uv run --with mujoco==3.10.0 python -m mujoco_skills.pipeline.build_skills_manifest \
  --scene <scene.xml> --tracks-dir <tracks> --standoffs <standoffs.json> --output <skills_manifest.json>

# 11) verification compiles (headless, no services) — see §7.
```

## 5. Gotchas (every one of these has bitten us)

**Object-YAML placement semantics (normalized, then sampled again)**
- `placement.pos` is a 2-D center in the selected normalized counter region;
  it is **not** MuJoCo world XY. `placement.size` is the normalized sampling
  window around that center; it is **not** the object's physical size and does
  not make `pos` an exact pose. RoboCasa samples again inside that window.
- `size: [0, 0]` is a degenerate/invalid sampling region, not a way to pin the
  object. The current generator uses `[0.25, 0.25]`. Use
  `rotation: [0.0, 0.0]` to disable random yaw.
- The current two-lane sampler uses normalized long-axis bounds
  `[-0.85, 0.85]`, short-axis edge-distance bounds `[0.60, 0.85]`, and
  `min_distance = 1.70 / (ceil(n_objects / 2) + compensation - 1)`, where
  compensation is 1 or 2. This is normalized center distance, **not meters**;
  object radii and the exporter's second sampling still matter. Measure final
  center/clearance distances from the exported MuJoCo model.
- For an island with an interior sink, the generator emits the qualified sink
  reference with `sample_region_kwargs: {ref: <sink>, loc: left_right}`. This
  asks RoboCasa to exclude the style-specific instantiated sink footprint.
  `full_depth_region: true` is only the fallback when no sink reference is
  available.
- Preview only the result of the real RoboCasa exporter loaded in MuJoCo. A
  hand-written 2-D preview does not implement RoboCasa regions, sink geometry,
  object footprints, or its second sampling pass and is not evidence that a
  seed is valid. If the real exporter cannot initialize, stop and diagnose it
  instead of substituting an approximate preview.

**Export / objects**
- `export_kitchen_scene` fails nondeterministically → retry same command ≤3x
  before debugging.
- Pinned object instances must resolve in the `objaverse`/`lightwheel`
  registries; `aigen` paths crash the exporter's reverse lookup
  (generate_objects_yaml now enforces this — error lists valid instances).
- Island counters with an interior sink split their top into strip regions;
  without `sample_region_kwargs: {full_depth_region: true}` objects cluster
  beside the sink out of robot reach (generator adds it by default now).
- Object placement is seed-sampled on the island's short-axis edge bands;
  iterate seeds until calibrate_standoffs reports 0 INFEASIBLE (a target can
  miss the 0.40–0.90m reach band by 1cm).
- Before blaming spacing for repeated initialization failures, validate every
  layout reference. Removed fixtures must not remain in `fixture`, `align_to`,
  `attach_to`, `ref`, or `interior_obj`; decorations such as a plant can keep
  referencing a removed counter and fail every retry deterministically.
- Texture downscale after EVERY export, before every mjb compile.
- `.mjb` must be compiled with mujoco==3.10.0 (frontend version).

**Tracks / replays**
- Raw dumps are index-addressed full-qpos arrays: **any change to nq (adding/
  removing objects) invalidates ALL raw dumps and requires re-running every
  replay**. Raw frame 0 also contains every current object pose, so moving only
  one apple/banana while keeping nq unchanged still invalidates all raw dumps.
  Extracted tracks are name-addressed and nq-robust, but keeping stale tracks
  leaves stale recording metadata — re-extract after re-replay.
- Open/Close pairs from independent demos usually DON'T meet (e.g. Close demo
  starts half-open → visible snap + hand misses handle). Preferred fix:
  `CloseX := --reverse(OpenX)` (exact continuity). 042 also uses `--retarget`
  for twin drawers (stack_4 ↔ stack_2; docs/skill_track_retarget.md).
- If OpenFridge finishes with the gripper intersecting a style-variant door,
  `reset_ready` falls back to slow local RRT. Run
  `append_openfridge_release --scene ... --tracks-dir ... --apply` after track
  extraction. It solves a collision-checked Cartesian retreat in the target
  scene; do not copy absolute joint frames between differently sampled tracks.
- Do not infer a release tail from frame count alone: old study tracks may be
  ~15 FPS while newly replayed tracks are ~30 FPS. Compare duration, phase,
  `meta.release_handle`, terminal contacts, and the following reset mode.
- Compile-profile item suffixes are global step indices. In a 12-step
  open→move→close session, `reset_ready_2` follows **OpenFridge**; the reset
  after CloseFridge is the final indexed reset. The `slowest:` list is sorted
  by cost and does not imply adjacency. Diagnose against the ordered item list.
- With objects resting in the container the compiler may synthesize
  `CloseFridge_with_<objects>` from the time-reversed OpenFridge track for
  exact endpoint continuity. Therefore an OpenFridge release tail can also
  appear at the opposite end of that generated compound close; validate the
  whole open→place→close plan, not only the standalone canonical Close file.
- Replays re-simulate on the target scene, so demo style_id doesn't need to
  match the export style.
- Every track must carry `meta.recording.robot_mount`; the compiler
  (`_track_record_mount`) hard-fails otherwise. New extractions write it;
  ONLY genuinely legacy tracks may be stamped via migrate_track_mounts with
  explicit values.

**Keyframe / init**
- A fixture with a Close demo but no Open demo starts OPEN via
  `bake_study_init --init-open robot0/CloseX` (042's upper cabinet).
- bake_study_init recompiles the mjb; never recompile a baked mjb by hand
  from an xml missing the keyframe.

**Container placement tuning (per scene, in scene_table `place.motion`)**
- `support_geom` must often be pinned by hand: shelf slabs are frequently
  anonymous geoms (012 fridge: g124 upper / g125 lower; the discovery
  heuristic needs "bottom"/"shelf" in the geom name). Slabs may also be
  DUPLICATED under the fixture main body (012: g9==g124, g10==g125).
- Tuning methodology: sweep candidate motion dicts (patch a copy of the
  manifest json, run compile_plan) over **both robots × every object instance
  headed to that container**, pick the params where all combos compile. Grasp
  offsets differ per robot×object, so single-combo success proves nothing.
- Current validated values — 012 fridge (lower shelf g125, 29cm compartment):
  `release_clearance 0.12`, `front_target_offset 0.28`,
  `front_target_max_fraction 0.85`, `slot_center_frac -0.5`
  (see the 012 scene_table for the live values — they were re-tuned after the
  initial 0.20 sweep); 012 upper_cabinet: `release_clearance 0.08`.
  042 fridge keeps its migrated french-door tuning (front_distance 0.60,
  slot_fracs [-0.25, 0.12], ik_eef_tolerance 0.05, ...).
- Compiler robustness already handled generically (don't re-fix per scene):
  epsilon joint-limit noise at replay end is clamped; contact pairs already
  present at the start pose are baseline-exempt (hand resting on the handle
  it just pulled).
- Container pins (`place_at_pin` into a container) are projected into the live
  support geom's local frame and clamped inside its current open-state bounds;
  multiple objects sharing a pin distribute laterally around it.
- Pin annotations persist on plan actions across turns; the authoring
  executor treats carried-over pins as pre-validated (only NEW annotations
  need a same-turn scene_ref).

**Manifest / validation**
- build_skills_manifest validates: all named bodies/joints/geoms exist (JSON-
  path errors), every track has recording.robot_mount, replay entry within
  `navigation.replay_entry_max_facility_distance` (default 2.5m) of its
  facility, motion override keys against a whitelist. It loads the KEYFRAME
  state (not qpos0) for world positions.
- `tracks/_generated/` is compile output and never enters the manifest.
- After regenerating manifests/tracks: **restart the backend** (`npm run dev`
  auto-spawns skill_service; the warm process caches rigs/old code).

## 6. Derived-artifact invalidation matrix

Never repair a changed formal scene by updating only the manifest. Use the
smallest applicable row below; when several changes overlap, take the union.

| Change | Required rebuild / check |
|---|---|
| object free-joint world pose in formal XML | bake `study_init` + recompile MJB; rerun **all** raw robot0/robot1 replays; re-extract/synthesize canonical tracks; recalibrate standoffs; rebuild manifest |
| add/remove object (`nq` changes) | same as above, plus update scene-table objects and any tests/inventories that enumerate them |
| robot mount or navigable geometry | all replays/tracks, standoffs, navgrid, bake/MJB, manifest |
| fixture body/joint/geom or semantic rename | scene table, replays/tracks as applicable, standoffs/navgrid, bake/MJB, manifest; validate every named binding |
| scene-table placement/motion/support tuning only | rebuild manifest, then rerun the complete container compile matrix |
| canonical track post-processing only | rebuild manifest; rerun skill/reset regressions; restart services to clear rig/track/memo caches |
| XML content of any kind | verify/rebuild manifest XML SHA256 and verify XML/MJB `study_init` agreement |

Before replacing formal artifacts, copy the current XML/MJB, standoffs,
manifest, raw replays, and canonical tracks into a timestamped directory under
`archive/robocasa_scenes/`. `tracks/_generated/` is disposable compile output
and normally should not be archived with canonical source tracks.

After rebuilding, assert all of the following:

1. every raw replay frame 0 contains the current XML qpos for every free-joint
   object (checking only the edited object is insufficient);
2. XML and MJB have the same nq/nkey and identical `study_init` qpos;
3. manifest scene SHA256 equals the current XML SHA256;
4. each synthesized CloseDrawer is the exact reverse of its intended OpenDrawer;
5. both robots' canonical tracks carry the current recording/model signature;
6. manifest excludes `tracks/_generated/` and any `*_with_*` compile artifact.

## 7. Verification and debug recipes (headless, no services)

```python
# uv run --with mujoco==3.10.0 python - <<EOF   (from playground root, sys.path += ["src"])
from mujoco_skills.skills import skill_generators as sg
r = sg.compile_plan(SCENE_XML, {"robot0": [
    {"id":"s0","op":"navigate","target":"banana_1"},
    {"id":"s1","op":"pick","object":"banana_1"},
    {"id":"s2","op":"navigate","target":"fridge"},
    {"id":"s3","op":"OpenFridge"},
    {"id":"s4","op":"place","object":"banana_1","dest":"fridge"},
]}, STANDOFFS, TRACKS_DIR, MANIFEST)   # result keys: items/conflicts/warnings/completed/rest_points
```
Matrix to run per scene: {both robots} × {every fridge-bound object} → fridge;
one object → each of cabinet/drawer/sink; cross-scene order (compile scene A,
then B, then A in ONE process) must not change results. Full test suite:
`uv run --with pytest --with mujoco==3.10.0 python -m pytest tests/ -q`
(≈280 tests; the heavy ones skip if scene assets are missing).
Per AGENTS.md: stop any processes you started; verify ports 8787/8899/8900.

Use four verification layers rather than treating “XML compiles” as success:

1. **Static consistency:** XML/MJB/keyframe/hash/body/joint/geom checks from
   §6, plus a short passive simulation for free-body support.
2. **Track consistency:** both robots, frame-0 qpos, recording signature,
   reverse/retarget provenance, OpenFridge terminal contact count and reset
   mode. The acceptance test for a release tail is zero fridge contacts and
   `gen_reset(..., retreat=0.18)` reporting `arm_reset_mode=linear`.
3. **Geometric feasibility:** every object and facility standoff is navigable;
   every placement point has collision-checked IK in the target scene.
4. **Behavior compilation:** both robots × every object/destination, complete
   open→move(s)→close sessions, multiple resting objects, and A→B→A cache
   isolation. Inspect ordered per-step compile profiles; container-session
   resets should normally be linear.

**Standoff versus placement debugging**
- Navigation/standoff failure happens before manipulation: inspect standoff XY,
  face target, navgrid projection, robot base pose, and reach-to-target.
- If navigation succeeds but place IK fails, inspect the placement point,
  `support_geom`, shelf/interior geometry, and scene-table `place.motion`.
- `/debug` pins should show kind, semantic object/facility name, body name/id,
  and world XYZ. Use a minimal compile-plan harness plan that ends at the
  destination approach to visualize the base pose immediately before place.
  Render placement markers as visual-only sites/geoms (`contype=0`,
  `conaffinity=0`).
- A manually pinned point is evidence about placement geometry, not a standoff;
  keep these coordinate domains separate.

**RoboCasa sanity-checker interpretation**
- The generic `mujoco_scene_check.py` is useful for initial penetrations and
  unsupported free-body drift, but RoboCasa fixture/handle names can trigger
  false end-effector classifications, and a single highest-work-surface test
  can flag valid low handles/grippers. Investigate real contact/drift findings,
  but do not judge a RoboCasa scene by its aggregate error count.
- The final manipulation gate is the target-scene compile/replay/contact matrix,
  not a generic naming heuristic.

## 8. Current scene inventory (2026-08-18)

| Scene | Status |
|---|---|
| **layout042_sorting** (style 51, seed 306, manually adjusted) | COMPLETE: 8 island objects (mug×2, apple×2, lemon×2, canned food, condiment; bowl/cereal/cup removed), nq=139 and one baked `study_init`. The two lemons reuse the former orange/banana world poses and the verified `objaverse/lemon/lemon_0` asset from layout024_study. Formal XML preserves manual apple_1/apple_2/lemon_1/lemon_2/condiment poses. Visual/user-facing drawer names were corrected: drawer_right is stack_4 and drawer_left is stack_2; each CloseDrawer is the strict reverse of its matching native/retargeted OpenDrawer. Both robots' 30 FPS OpenFridge tracks have a target-scene-solved +X 0.16m/0.8s release tail (693→717 frames), eliminating handle contact before reset. Manifest v2, standoffs, navgrid, floorplan, MJB and canonical tracks were synchronized after the lemon swap. Archives include `layout042_sorting_pre_seed306_20260811/`, `layout042_sorting_pre_banana_rebake_20260812/`, `layout042_sorting_pre_apple2_rebake_20260812/`, `layout042_sorting_pre_orange_swap_20260815/`, `layout042_sorting_pre_remove_cup_20260817/`, and `layout042_sorting_pre_lemon_swap_20260817/`; pre-release OpenFridge tracks are also retained under `tracks/_backups/`. |
| **layout042_study** (default dev scene, style 51) | OLD 6-object set (mug×2, apple×2, condiment, bowl). Tracks are legacy: recorded on a scene revision that no longer exists, stamped via migrate_track_mounts (recording.model_signature=null + migration note). Manifest v2 ✓. `study_init` has cabinet-open baked (no OpenCabinet demo — replay only offers CloseCabinet). Has navgrid + scene_table + task_plan context for the frontend. |
| **layout012_study** (sorting scene, style 18) | COMPLETE reference implementation: 11 objects (see spec in §4 step 2), 6 skills (CloseDrawer = reverse of OpenDrawer), native recording metadata, v2 manifest, tuned container motion. Copy this scene's files as templates. |
| **layout012_preparing** (breakfast scene, style 18) | COMPLETE: bowls/spoons on counter, cereal in `cab_2_main_group_level1`, and milk/oranges in the fridge; eight object-specific breakfast landing targets on the island. The formal XML and `study_init` preserve the hand-adjusted cereal/milk/orange poses; MJB, all 12 raw replays, canonical tracks, previews, and manifest were synchronized after the 2026-08-15 pose edit. Cold-object standoffs intentionally reuse OpenFridge and cereal reuses OpenCabinet. Pre-sync artifacts are archived under `layout012_preparing_pre_object_pose_sync_20260815/`. |
| **layout024_sorting** (style 45, manually adjusted) | COMPLETE: style 45 deliberately matches OpenFridge ep88's `Refrigerator040`; style 46 / `Refrigerator059` was rejected because its `freezer_door` and `fridge_door` physical sides are swapped relative to ep88, making the arm pull one side while the other door opens. Eight objects are retained (bowl×2 at the counter pins, cup_4×2, orange fork_1×2, avocado_1×2); bowls use 0.6 scale. The two avocados replace boxed food and jam while preserving those slots' XY positions and using the original avocado asset's stable support height/orientation. `fork_1` replaced gray `fork_3`, which visually merged with the white island countertop; both fork positions are preserved, but only the first fork body is rotated 180° around world Z to reverse tine direction. Sorting semantics are bowl -> initially-open `cabinet`, cup/fork -> `drawer`, and avocado -> `fridge`; Refrigerator040 uses upper fridge shelf `fridge_right_group_g10`, whose physical support geom belongs to the semantic interior body's ancestor fixture. Fixed world-space default placement slots are hand-pinned in the scene table: cabinet has four slots across its lower and level1 shelves, fridge has two, and sink has two. `cab_1_main_group` starts open; native robot0 CloseCabinet ep65 is mount-retargeted to robot1 with its world-space base/arm path preserved, so explicitly locked robot1 close commands are supported. OpenDrawer ep69 is paired with its strict reverse; OpenFridge ep88 is paired with its strict reverse. Formal XML/MJB (`nq=170`, baked `study_init`), raw replays, canonical tracks, 12-target standoffs and schema-v2 manifest (8 objects, 6 facilities, 5 canonical skills) are synchronized. Representative bowl/cabinet, cup/sink, and avocado/fridge plans compile to the hand-pinned first slots with zero conflicts or warnings. Archives: `layout024_sorting_pre_style45_20260817/`, `layout024_sorting_pre_fork1_20260817/`, `layout024_sorting_pre_fork_flip_20260818/`, and `layout024_sorting_pre_avocado_restore_20260818/`. |
| **layout024_study** (legacy style 46, seed 7) / **layout038_study** (style 58) | Baseline exports with 9-object sets and stripped outer walls. `layout024_study` is no longer the formal 024 sorting scene; use `layout024_sorting` style 45. Full replay availability remains documented in **§10**. |

`layout024_sorting` update (2026-08-19), superseding the avocado inventory in
the table above: `avocado_1` / `avocado_2` were replaced by two instances of
038 preparing's verified `objaverse/spoon/spoon_11` asset, named `spoon_1` /
`spoon_2`. They preserve the former avocado XY slots and use body Z `0.9307`,
which leaves them flat with no initial island contact. The island drawer now
starts open in both `study_init` (slide joint `-0.4623974073`) and manifest
state. An initially-open container no longer requires a redundant OpenDrawer
dependency; the two-spoon-to-drawer plan compiles without OpenDrawer and ends
with one CloseDrawer, zero conflicts, and zero warnings. Formal XML/MJB, source
objects YAML, raw replays, canonical tracks, standoffs, navgrid, and manifest
were synchronized. The previous artifacts are archived under
`layout024_sorting_pre_spoon_drawer_open_20260819/`.

`layout024_sorting` drawer robot coverage update (2026-08-19): the native
robot0 `OpenDrawer` track and its strict-reverse `CloseDrawer` track are
mount-retargeted to robot1 while preserving their world-space base and arm
paths. The manifest therefore registers both drawer operations for robot0 and
robot1.

`layout024_sorting` fork initial-height correction (2026-08-18): both `fork_1`
instances now use body Z `0.9306938`. Their visual mesh minimum is
`0.9205000`, leaving 0.5 mm clearance above the island surface at Z `0.92`;
the XML and compiled MJB `study_init` agree and report no initial fork/island
contacts. The prior XML/MJB/build metadata are archived under
`layout024_sorting_pre_fork_height_20260818/`.

`layout024_sorting` bowls retain the default top-down grip. A horizontal-grip
experiment could pick them from their 0.75 m counter standoffs, but the carried
orientation made the subsequent cabinet reach-in fail IK/collision planning;
the horizontal override and its `[0, 0, 0.015]` offset were therefore removed.

`layout024_sorting` cups retain the default top-down grip. A horizontal-grip
diagnostic can pick either cup, but cabinet reach-in fails during the
horizontal insertion (`eef` IK residual 0.0637 m, followed by no
collision-free fallback solution). The cabinet-wide `release_clearance` is
0.02: the previous 0.08 value raised a lower-shelf cup target to about Z 1.5724
and made robot0 link7 contact the level1 shelf. With 0.02, both default-grip
cups compile end to end into their object-specific lower pins, including
CloseCabinet, with zero conflicts or warnings.

The lower cup pins are deep enough that the default-grip wrist can collide with
the level1 shelf late in the straight horizontal insertion. The cabinet enables
`nearby_rrt_fallback`: it retains the collision-free Cartesian prefix and tries
nearby IK seeds before the global random fallback. On the fixed `cup_1` pin this
reduced the fallback from 1,858 RRT iterations / 7,322 collision checks / 31.74 s
to 4 iterations / 216 checks / 1.74 s without moving the pin; `cup_2` uses 3
iterations and remains upright. Keep this override cabinet-specific so other
scenes preserve their established deterministic fallback trajectories.

Cabinet destinations are object-specific, not generic ordered slots:
`bowl_1` / `bowl_2` are bound to the two level1 upper-shelf pins and `cup_1` /
`cup_2` to the two main-body lower-shelf pins. This binding uses
`object_slot_points`, so separate “move bowls” and “move cups” requests cannot
both restart at slot zero.

Reach-in placement now applies the same release-time upright correction as
surface placement: during the final gripper-open segment, roll/pitch is removed
relative to the object's initial resting orientation while the motion-selected
yaw is retained. The generated reach-in track reports `object_up_alignment`;
both the two-cup lower-shelf case and two-bowl upper-shelf case report exactly
`1.0`, with zero conflicts or warnings.

Study design (docs/paper draft/user_study.tex): the two active scenarios are
Kitchen Island Sorting and Breakfast Preparation; their formal scenes are
`layout042_sorting` and `layout012_preparing`, respectively.

## 9. Completed case study: layout042_sorting

This replaces the obsolete “swap layout042's objects” plan. The final
scene deliberately did **not** copy all 11 objects from 012: after real-export
seed review, bowl and cereal were removed and seed 306 became the base for a
manually curated scene. Cup was subsequently removed, leaving an 8-object
formal scene. The case is useful as the reference workflow for an export
followed by human-curated world poses.

### Final source and formal state

- Source layout: layout 42 study geometry, style 51.
- Object generator seed: 306; initial source spec is represented by the final
  `layout042_sorting_objects.yaml` (mug×2, apple×2, lemon×2, canned food,
  condiment bottle). Both lemons use `objaverse/lemon/lemon_0`.
- Generator config: `[0.25, 0.25]` sampling windows, fixed yaw, qualified sink
  `left_right` regions, adaptive normalized spacing.
- Formal XML was then manually curated: apple_1, apple_2, lemon_1, lemon_2,
  and condiment positions were adjusted. The formal XML/free-joint qpos is the
  runtime truth; do not overwrite it with a new export unless intentionally
  beginning a new candidate.
- Final model: nq=139, `study_init` baked with upper cabinet open because layout
  42 has CloseCabinet but no OpenCabinet dataset replay.

### Replay and skill synthesis

For both robot0 and robot1, replay layout-42 episode index 0 for:

| Task | Source episode | Raw→canonical frames |
|---|---|---:|
| OpenFridge | episode_000055 | 462→693 before release synthesis |
| CloseFridge | episode_000001 | 270→405 |
| CloseCabinet | episode_000041 | 250→375 |
| OpenDrawer | episode_000007 | 211→316 |
| CloseDrawer | episode_000055 | 165→247 (raw retained for provenance; formal closes are synthesized below) |

Drawer semantics after checking world positions, not stack labels:

- `drawer_right` = stack_4: native `OpenDrawer`; `CloseDrawer_stack4` is its
  strict reverse.
- `drawer_left` = stack_2: `OpenDrawer_stack2` translated from stack_4;
  `CloseDrawer` is its strict reverse.

After every extraction, run `append_openfridge_release` for this scene. It adds
a +X 0.16m / 0.8s collision-checked Cartesian tail to both OpenFridge tracks
(693→717 frames at 30 FPS), removes terminal gripper/fridge contact, and makes
the next reset linear. Dry-run first; `--apply` creates a timestamped backup.

### Rebuild order after a manual object-pose edit

1. archive the current formal/trajectory artifacts;
2. rerun all 10 raw replays (five tasks × two robots), even though nq is
   unchanged;
3. extract the 10 native tracks, retarget the two stack_2 opens, and synthesize
   the four strict-reverse drawer closes (14 canonical files total);
4. append both OpenFridge release tails;
5. bake `study_init` with `--init-open robot0/CloseCabinet` and compile MJB with
   MuJoCo 3.10.0;
6. recalibrate standoffs and rebuild navgrid/manifest;
7. run §6 consistency assertions and §7 behavior matrix;
8. restart 8787/8899/8900 services so no old rig, track, or compile memo remains.

The Apple 2 rebake regression specifically compiles a full
OpenFridge→apple_2 place path, including its collision-checked RRT fallback at
the difficult lower waypoint. The OpenFridge regression separately asserts
that both robots finish contact-free and admit a linear reset. These are better
gates than a screenshot or standalone canonical replay alone.

## 10. Candidate replay skills: layout024 / 038 / 034 / 050

Ground truth extracted from `datasets/v1.0/pretrain/atomic/atomic_layout_summary.json`
+ each episode's `ep_meta.json` (`fixture_refs`). The 024/038 target fixtures
were verified to exist WITH their joints in the current decoration-stripped
exports (`layout024_sorting.xml` style 45 / `layout038_study.xml` style 58) on
2026-08-17. Layouts 034/050 were added as promising candidates on 2026-08-20:
their dataset coverage and refrigerator state trajectories were audited, but
they do **not** yet have decoration-stripped exports, fixture-survival checks,
reachability calibration, or target-scene replays. Replays do re-simulate on
the target export, but style compatibility still requires a geometry/semantics
audit: refrigerator variants can change handle geometry or assign the same
door names to different physical sides. The formal 024 sorting scene therefore
uses ep88's own style 45. `idx` is the `--episode-index` for
`replay_atomic_on_scene` (position in the sorted episode_ids of that
task+layout).

### How to query replay availability for ANY layout (the method behind these tables)

All data lives under the atomic dataset root
`C:\Users\madizhi\Documents\robocasa_ws\robocasa\datasets\v1.0\pretrain\atomic\`:

1. **Coarse pass — which tasks exist in a layout.** Run the interactive helper
   that sits next to the summary JSON:
   `uv run python <atomic_root>\query_layout_tasks.py`, choose `layout`, enter
   e.g. `24`. It prints every task name (reads `by_layout[<id>]["tasks"]`).
   Articulation tasks are the `Open*/Close*` ones (Fridge/Cabinet/Drawer/
   Microwave/Dishwasher…).

2. **Fine pass — episode indices + target fixtures.** The summary JSON
   (`atomic_layout_summary.json`, same dir) has three useful sections:
   - `by_task[<Task>]["layouts"][<layout>]["episode_ids"]` — the episode list.
     **`--episode-index` = position in this list after `sorted()`** (that is
     exactly how `replay_atomic_on_scene` resolves it).
   - `episodes[<episode_id>]["paths"]["ep_meta"]` — relative path to that
     episode's `ep_meta.json` (note: episodes live under
     `<Task>/<date>/lerobot/extras/episode_XXXXXX/`, not directly under the
     task dir). `ep_meta.json` contains `fixture_refs` → which concrete fixture
     the demo manipulates, plus `style_id` and the init base pose.
   - `episodes[<episode_id>]["style_id"]` — normally a compatibility hint rather
     than a hard requirement because replay re-simulates on YOUR export. Treat
     it as a hard audit item for articulated fixtures whose variants can swap
     physical part semantics (notably side-by-side refrigerator doors).

   Copy-paste snippet (prints idx / episode / target fixture for one task+layout):

   ```python
   import json, pathlib
   root = pathlib.Path(r"C:\Users\madizhi\Documents\robocasa_ws\robocasa\datasets\v1.0\pretrain\atomic")
   d = json.load(open(root / "atomic_layout_summary.json"))
   task, layout = "OpenCabinet", "24"
   for idx, ep in enumerate(sorted(d["by_task"][task]["layouts"][layout]["episode_ids"])):
       meta = json.load(open(root / d["episodes"][ep]["paths"]["ep_meta"]))
       print(idx, ep, meta.get("fixture_refs"))
   ```

3. **Verify against your export.** A demo is only usable if its
   `fixture_refs` fixture still exists **with its joints** in the
   decoration-stripped study XML — grep the exported `layoutXXX_study.xml` for
   the fixture name and its `*doorhinge`/`*slidejoint` joints. Decoration
   stripping can delete fixtures; this is the step that catches it.

4. **Fill gaps with synthesis** (§ track synthesis): missing Close :=
   `--reverse` of Open (and vice versa); a twin fixture elsewhere can donate via
   `--retarget` (tool self-checks admissibility; differently-facing hinge
   cabinets are rejected by design).

### layout034 / layout050 — promising double-door candidates (2026-08-20)

Both layouts have an island and a sink, pass the coarse coverage gate
(fridge/drawer/cabinet each have at least one atomic demo), and rank near the
top of `analyze_layout_candidates.py` by conversion complexity. They remain
**candidates**, not validated study scenes.

| Layout | complexity score | fridge O/C | drawer O/C | cabinet O/C |
|---|---:|---:|---:|---:|
| 034 | 71 | 1/2 | 2/2 | 2/1 |
| 050 | 76 | 2/7 | 4/4 | 2/2 |

Their refrigerator fixture is `fridgefrenchdoor_left_group_1`. Direct
inspection of every source `states.npz` against its episode `model.xml.gz`
confirmed that every listed Open/Close demo moves **both** upper refrigerator
door joints by about 1.45–1.57 rad; the freezer doors/drawers remain closed.

| Layout | Task | idx | episode(s) | source style(s) | observed door motion |
|---|---|---|---|---|---|
| 034 | OpenFridge | 0 | ep58 | 48 | left + right open |
| 034 | CloseFridge | 0–1 | ep9 / ep46 | 36 / 47 | left + right close |
| 050 | OpenFridge | 0–1 | ep1 / ep66 | 20 / 25 | left + right open |
| 050 | CloseFridge | 0–6 | ep17/36/39/72/79/81/104 | 13/28/45/16/14/45/50 | left + right close |

Recommended continuity policy: choose one OpenFridge episode after reviewing
its matching style/handle geometry, then synthesize CloseFridge as its strict
reverse instead of pairing independently recorded endpoints. Before promoting
either layout, strip/export it and perform the fixture, articulation,
reachability, and target-scene replay gates in §4.

### layout024 — rich coverage, zero synthesis required for coverage

“Matched pair” below means that native Open and Close demos target the same
fixture. It does **not** guarantee endpoint continuity across independently
recorded styles. The 2026-08-17 endpoint audit found that the recommended
cabinet and drawer pairs should still use one native direction plus its exact
reverse when building canonical tracks.

| Task | idx | episode | target fixture | joints |
|---|---|---|---|---|
| OpenFridge | 0–5 | ep26/31/45/47/88/100 | `fridge_right_group` (all six) | fridge_door + freezer_door |
| CloseFridge | 0–3 | ep15/38/68/102 | `fridge_right_group` (all four) | 〃 |
| OpenCabinet | 1 | ep51 | **`cab_1_main_group`** (single door) | `cab_1_main_group_doorhinge` |
| CloseCabinet | 0 | ep65 | **`cab_1_main_group`** ← matched pair! | 〃 |
| OpenCabinet | 3, 4 | ep88, ep97 | `cab_2_main_group` (double door, no Close demo) | left/rightdoorhinge |
| OpenCabinet | 0 / 2 | ep42 / ep65 | `cab_4_main_group` / `cab_5_main_group` (no Close) | — |
| OpenDrawer | 1 | ep69 | **`stack_1_island_group_1_3`** (island drawer) | slidejoint |
| CloseDrawer | 0 | ep15 | **`stack_1_island_group_1_3`** ← matched pair! | 〃 |
| OpenDrawer | 0 | ep31 | `stack_3_main_group_6` (main wall, no Close) | 〃 |
| CloseDrawer | 1, 2 | ep28, ep34 | `stack_7_main_group_3` (main wall, no Open) | 〃 |
| Open/CloseMicrowave | 0–1 each | — | `microwave_right_group` (native pair, bonus) | — |
| Open/CloseDishwasher | 0–1 / 0–3 | — | `dishwasher_main_group` (native pair, bonus) | — |

Recommended picks:
- **fridge**: use Open idx=4 (ep88, style 45 / `Refrigerator040`) and synthesize
  Close as its strict reverse. The formal export also uses style 45 so the
  robot hand path, handle geometry, and left/right door semantics agree. Do
  not replay ep88 on style 46 / `Refrigerator059`: that model swaps the physical
  sides associated with `freezer_door` and `fridge_door`.
- **cabinet #1**: `cab_1_main_group` Open idx=1 ends at -1.626248 rad (small
  dynamic overshoot past the -1.57 rad limit); Close idx=0 runs -1.552848 →
  0.000168 rad. The `layout024_sorting` study design starts this cabinet open
  at the native Close frame-0 value and exposes **only native Close idx=0**;
  it does not expose an OpenCabinet skill.
- **cabinet #2** (only if the scenario needs a second cabinet, e.g. breakfast
  cereal vs bowl): `cab_2_main_group` Open idx=3, Close := `--reverse` of it.
- **drawer** (native coverage in both directions, on the island):
  `stack_1_island_group_1_3` has a -0.60 → 0 m joint range. Open idx=1
  (ep69) runs -0.000523 → -0.462397 m, so its actual opening travel is about
  **46.2 cm**; this is the fixture's intended functional open endpoint rather
  than the raw 60 cm joint stop. Native Close idx=0 starts only at -0.284261 m
  and ends at 0.000161 m, so it does not meet Open's endpoint. Use Open idx=1
  and synthesize Close as its exact reverse. Main-wall alternatives exist
  (stack_3 open-only /
  stack_7 close-only; pairable via reverse, or via retarget if the stacks are
  translation-twins — the tool self-checks admissibility).

For formal `layout024_sorting` (style 45), use native OpenDrawer idx=1 and
define CloseDrawer as its strict time reversal. Native CloseCabinet idx=0 is
replayed directly, and its frame-0 cabinet value is baked into `study_init` so
the scene starts open. The selected fridge Open idx=4 (ep88) opens both
doors (freezer 0 → 1.568632 rad; fridge 0 → 1.570817 rad), whereas native Close
idx=0 (ep15) closes only the fridge door from 1.462133 → -0.000012 rad. They are
not endpoint-continuous. The selected `layout024_sorting` policy therefore uses
native OpenFridge idx=4 and defines CloseFridge as its strict time reversal;
both doors close from the exact Open endpoint with no state snap.

### layout038 — reverse-heavy; NO OpenDrawer demo exists

| Task | idx | episode | target fixture | joints |
|---|---|---|---|---|
| OpenFridge | 0–4 | ep21/43/64/90/93 | `fridgesidebyside_left_group_1` (all five) | fridge_door + freezer_door |
| CloseFridge | 0 | ep28 | `fridgesidebyside_left_group_1` ← matched pair | 〃 |
| OpenCabinet | 0 | ep63 | `hingecabinet_4_left_group_1` (ONLY Open demo in the layout) | left/rightdoorhinge |
| CloseCabinet | 0, 2 | ep3, ep87 | `hingecabinet_3_left_group_1` (no Open) | 〃 |
| CloseCabinet | 1 | ep74 | `hingecabinet_2_left_group_1` (no Open) | 〃 |
| CloseDrawer | 3 | ep74 | `stack_3_island_group_1_2` (island drawer) | slidejoint |
| CloseDrawer | 0, 1 | ep13, ep47 | `stack_1_main_group_1_2` | 〃 |
| CloseDrawer | 2 | ep62 | `stack_4_main_group_1_2` | 〃 |
| Open/CloseMicrowave | 0 each | ep57/ep17 | `microwave_main_group_1` (native pair, bonus) | — |
| Open/CloseDishwasher | 0–1 each | — | `dishwasher_1_main_group_1` (native pair, bonus) | — |

Required synthesis for 038:
- **drawer**: `OpenDrawer := --reverse(CloseDrawer)` — no Open demo exists
  anywhere in the layout. Island drawer `stack_3_island_group_1_2` (Close
  idx=3, ep74) is the natural pick.
- **cabinet A** (`hingecabinet_4_left_group_1`): Open native (idx=0),
  Close := reverse.
- **cabinet B** (`hingecabinet_3_left_group_1`, if a second cabinet is
  needed): Close native (idx=0 or 2), Open := reverse.
- hingecabinets 2/3/4 all sit in `left_group_1` (same wall) — a
  translation `--retarget` between them is plausible if more coverage is
  wanted; the tool's quat/axis/range checks decide admissibility (hinge-door
  retargets FAIL for differently-facing cabinets by design).
- **fridge**: native pair, no synthesis (Open idx=0 + Close idx=0).

### Scenario fit

| Need | 024 | 038 |
|---|---|---|
| Sorting (fridge pair + sink + insertion-candidate containers) | all native | fridge native; drawer/cabinet pairs via reverse |
| Breakfast preparing (fridge + 2 storage containers + drawer) | fridge + cab_1 + cab_2(open+rev) + island drawer, all demos exist | fridge + hingecab_4(open+rev) + hingecab_3(close+rev) + island drawer(rev) |

Both scenarios are feasible in both layouts. 024 is the comfortable one
(native pairs everywhere, six fridge-open demos to choose from); 038 exercises
the reverse-synthesis path for cabinet AND drawer, so budget extra time for
its continuity checks (§7) and possibly an OpenFridge-style release tail.
