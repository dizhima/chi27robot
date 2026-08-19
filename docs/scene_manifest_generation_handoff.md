# Scene Manifest Generation Handoff

## Owner and objective

This handoff is for the agent responsible for RoboCasa study-scene generation and
manifest generation.

The objective is to make each generated `skills_manifest.json` the complete,
scene-specific execution contract consumed by the planner, skill compiler, and
frontend. After this work, `skill_generators.py` must not contain fixture body
names, scene-specific robot mount poses, scene-specific standoffs, or
fixture-specific placement tuning.

This is a data-ownership and generator task. Do not fix it by adding
`if layout012` / `if layout042` branches or by replacing one global constant
with another scene-specific constant table in Python.

Background design: [manifest_redesign.md](manifest_redesign.md). That document
established the noun/verb split and the generated-manifest flow. This handoff
extends it with the runtime information that was still missing: recorded robot
mounts, complete container placement metadata, track compatibility, navigation
configuration, pins, and validation.

## Current incident

Scene:

- XML: `frontend/public/assets/robocasa/layout012_study.xml`
- generated manifest:
  `frontend/public/trajectories/layout012_study/skills_manifest.json`
- fixed replay directory:
  `frontend/public/trajectories/layout012_study/tracks/`

The following commands currently fail during compile:

```text
move the banana to the cabinet
move banana to fridge (pin p1)
```

Observed errors:

```text
point [9.495498605452871, -5.028818105919106] is 4.659m from navigable space
point [6.719327214067684, -2.2300509386435934] is 1.784m from navigable space
```

The 012 manifest and raw 012 tracks contain correct facility/object data. The
bad points are produced later when the compiler retargets the replay with a
global historical robot mount:

- `src/mujoco_skills/skills/skill_generators.py::_SKILL_RECORD_MOUNT`
- `src/mujoco_skills/skills/skill_generators.py::_retarget_skill_base`

The raw 012 replay entry poses are valid:

- `OpenCabinet`: approximately `[4.126, -0.846]`
- `OpenFridge`: approximately `[1.326, -3.622]`

Temporarily disabling the global mount retarget allows navigation, open, pick,
and destination navigation to compile. It then exposes a second problem:
container placement still reads legacy 042 body names from
`PLACEMENT_REGIONS` instead of the active manifest.

Wrong legacy names:

```text
hingecabinet_2_right_group_1_level1_main
fridgefrenchdoor_main_group_1_Body001_Clear
```

Correct 012 names already present in the generated manifest:

```text
cab_2_main_group_level1_main
fridge_1_left_group_Body001_Clear
```

The navigation limit of `0.600m` is not the root cause and must not be enlarged
to hide this failure.

## Required data ownership

Use three artifact layers with explicit responsibilities.

### 1. Scene XML / MJB

The MuJoCo model owns physical structure:

- body, joint, geom, site, actuator names and topology;
- robot mount body transforms;
- qpos layout and joint ranges;
- keyframes and initial physical state;
- collision and visual geometry.

### 2. `<scene>.scene_table.json`

This is the small scene-authoring input beside the XML. It should contain only
information that cannot be reliably inferred from MuJoCo structure or track
payloads:

- semantic IDs, labels, and aliases;
- object/facility body bindings;
- optional per-object `pick` defaults (`grasp_mode`, full world-axis XYZ
  `grasp_offset` from the body origin, and horizontal `return_to_ready`) when grasp geometry is
  scene-specific; absent `grasp_offset` preserves the legacy generated target;
- facility semantic type;
- which surface/container is placeable;
- semantic articulation grouping;
- skill-to-facility binding where inference would be ambiguous;
- placement and motion overrides that are intentionally scene-specific;
- optional fixed world-frame placement `slot_points` (`[x, y]` or
  `[x, y, z]`) when a debug/pin workflow has validated exact landing targets;
- optional object-specific world-frame `object_slot_points` mapping when each
  named object must keep the same authored target regardless of plan order;
- named pins and their fixture-local locations;
- explicit initial semantic state when the chosen keyframe matters;
- manual override provenance/reason.

Do not hand-copy world positions, robot mount transforms, joint ranges, or
track entry poses into the scene table when the generator can calculate them.
Fixed placement slots are the exception: they are intentional authored task
targets. Runtime `at`/pin overrides take priority; otherwise slots are consumed
in their declared order, remain stable as object count grows, and define a hard
facility capacity.

### 3. Generated `skills_manifest.json`

This is the complete runtime contract. It should contain both authored semantic
data and all validated/derived execution data. Runtime code should not need to
open `scene_table.json`, scrape XML for semantic relationships, or load a
parallel `standoffs.json`.

"Put everything possible in the manifest" means complete generated output, not
maximum hand-maintained duplication.

## Required generated manifest sections

The exact JSON encoding can change during implementation, but the following
information is required.

### `schema_version`

- Increment for breaking changes.
- Add a JSON schema and validate every generated manifest.

### `scene`

Required fields:

```jsonc
{
  "id": "layout012_study",
  "xml": "assets/robocasa/layout012_study.xml",
  "mjb": "assets/robocasa/layout012_study.mjb",
  "xml_sha256": "...",
  "model_signature": "...",
  "mujoco_version": "3.10.0",
  "nq": 181,
  "nv": 0,
  "init_keyframe": "study_init",
  "coordinate_frame": "world",
  "units": {"length": "meter", "angle": "radian"}
}
```

`model_signature` must cover named model structure, not only `nq`. At minimum
include body/joint/geom/site/actuator names, joint types/ranges/qpos addresses,
and robot mount transforms.

### `robots`

For every robot generate:

- semantic robot ID and numeric index;
- robot type and capabilities;
- `base_body`, end-effector site/body, arm joints, finger joints, base joints,
  and relevant actuators;
- current mount world position/quaternion/yaw from the compiled model;
- gripper open/closed semantics and limits;
- navigation footprint/collision margin;
- optional ready track/keyframe binding.

No generator/runtime code should construct model names by assuming a particular
`robot{N}` naming scheme without validating the names recorded here.

### `objects`

For every semantic object generate/preserve:

- ID, label, aliases, semantic class;
- body and free-joint binding;
- pickability;
- initial home facility and initial world pose;
- geometry summary needed for grasp/placement clearance;
- grasp mode, offsets, preferred orientation, and approach clearance;
- object navigation standoff/face pose;
- optional allowed/forbidden destination classes.

Initial world pose is a snapshot only. Runtime code must use current MuJoCo
state after an object has moved.

### `facilities`

For every facility generate/preserve:

- ID, label, aliases, semantic type, primary body, initial world pose;
- navigation standoff, facing point, position/yaw tolerances;
- complete articulation group, initial semantic state, state thresholds;
- complete placement definition;
- open/close skill references;
- facility-local pins.

Surface placement must specify:

- `kind: surface`;
- support body and preferably support geom;
- region frame, center/offset, half extents, edge margin, and z offset;
- top-down placement/grasp tuning.

Container placement must specify:

- `kind: container`;
- access mode (`front` or `top`);
- `interior_body`;
- `support_geom`, or an explicit validated rule for discovering it;
- state source and required semantic state;
- complete placement motion parameters currently living in
  `PLACEMENT_REGIONS`, including release clearance, front target offset,
  preinsert distance, slot spread/fractions, IK tolerance, and RRT flags;
- object-size/edge safety margins.

There must be no concrete RoboCasa body or geom name left in
`skill_generators.py` after the consumer-side migration.

### `skills`

Use a registry keyed by stable skill ID. For each fixed replay include:

- semantic action and target facility;
- implementation type (`fixed_replay`, `reverse_replay`, or retargeted replay);
- per-robot track path;
- compatible robot IDs;
- preconditions, effects, and affected joints;
- reverse skill relationship;
- entry and exit base/facing poses;
- duration/frame count/provenance;
- recording scene ID and model signature;
- coordinate-frame semantics;
- recorded robot mount for every robot-specific track;
- any fixture-to-fixture retarget provenance and transform.

Example recording metadata:

```jsonc
{
  "recording": {
    "scene_id": "layout012_study",
    "model_signature": "...",
    "robot": "robot0",
    "coordinate_frame": "robot_mount_local",
    "robot_mount": {
      "position": [2.584, -6.084, 0.0],
      "quaternion": [0.7071, 0.0, 0.0, 0.7071],
      "yaw": 1.57079632679
    }
  }
}
```

The recorded mount must be calculated from the exact source model used when
the track was extracted. Do not infer it later from a global table.

Track JSON should retain only time-series payload plus minimal identity fields:

- schema version;
- skill ID;
- manifest/model signature;
- time, phase, and channels;
- optional payload checksum.

Large per-frame arrays belong in track files, not the manifest.

### `ops`

Keep `navigate`, `pick`, and `place` as parameterized operations. Record their
argument/capability constraints; do not enumerate every object-destination
combination.

### `navigation`

Generate/store scene-level planner configuration:

- grid resolution;
- obstacle inflation/robot footprint policy;
- bounds margin;
- nearest-free maximum distance;
- replay-entry-to-standoff validation tolerance;
- optional navigation exclusions/allowed regions if they are semantic scene
  information.

The runtime may retain hard safety ceilings, but scene tuning must not be hidden
inside skill generation code.

### `pins`

Each pin must specify:

- stable ID and label;
- owning facility;
- reference body/geom frame;
- local position/orientation;
- clearance and allowed object constraints.

Container pins are projected from authored world XY into the live support
geom's local frame and clamped to its current open-state region at runtime.
Multiple objects sharing one pin distribute laterally around the projected
point; a single in-bounds pin retains its authored XY.

### `state_model`

Describe semantic facility states and transitions so augment/decompose does not
infer behavior from strings such as `OpenFridge`:

- valid states;
- initial state;
- skill transition `from` and `to` states;
- observable joint thresholds used to validate state.

### `validation` and `generated`

Record validation policy and build provenance:

- require all named bodies/joints/geoms/sites/actuators;
- require track/model signature match;
- require reachable facility/object standoffs;
- require replay entry near the associated facility standoff;
- generator name/version/time;
- source XML hash;
- list of manual overrides and reasons.

## Generator implementation requirements

Primary implementation entry point:

```text
src/mujoco_skills/pipeline/build_skills_manifest.py
```

Required work:

1. Extend `load_scene_table` to preserve the new semantic/override sections
   instead of returning only `facilities` and `objects`.
2. Load the selected keyframe before deriving positions and semantic initial
   states. Do not assume `qpos0` and `study_init` are equivalent.
3. Inspect the compiled model to generate robot definitions and mount poses.
4. Fold `standoffs.json` content into the generated manifest. It may remain as
   a transitional output, but runtime consumers must move to the manifest and
   the duplicate file should ultimately be retired.
5. Generate complete surface/container placement configuration. Scene-table
   values override inferred values; inferred values must carry provenance.
6. Scan only canonical replay tracks, excluding `_generated/` compile output.
7. Emit per-robot recorded mount and model compatibility metadata for every
   fixed track.
8. Validate all model references and fail generation with the full JSON path,
   for example:

   ```text
   facilities.fridge.placement.interior_body: unknown body ...
   ```

9. Validate replay entry poses against their associated facility standoffs.
   Reject a manifest that would have generated the current 012 out-of-kitchen
   targets.
10. Write atomically: validate the complete in-memory manifest before replacing
    the existing output.

If recorded mount data cannot be reconstructed reliably from the current track
alone, update the extraction pipeline so it writes the source mount at track
creation time. The relevant existing retargeting background is
[skill_track_retarget.md](skill_track_retarget.md).

## Consumer-side contract expected after generation

The manifest-generation agent does not need to complete the whole compiler
refactor unless explicitly assigned, but its output must support it.

Expected follow-up consumer changes:

- remove `_SKILL_RECORD_MOUNT`;
- make `_retarget_skill_base` read per-track recording metadata;
- remove scene-specific contents of `PLACEMENT_REGIONS`;
- make `placement_regions_from_manifest` use manifest container fields, not
  only surface geometry;
- make `compile_plan` read standoffs/navigation/pins from the manifest;
- include scene ID, model signature, manifest hash, and track fingerprints in
  rig/retarget/compile cache identities;
- clear or naturally miss all scene-bound caches when the active scene changes.

The generator should not preserve an old schema merely because current runtime
code still expects it. Provide a deliberate compatibility/migration layer and
tests instead.

## Migration scope

Generate and validate schema-v2 manifests for at least:

- `layout012_study`
- `layout042_study`

Do not overwrite or naively recompile `layout042_study.mjb`; it contains the
baked `study_init` keyframe. This task should not require changing either scene
XML or MJB unless validation proves the model itself is wrong.

For old tracks missing recorded mount metadata, use one explicit migration:

1. load the exact source scene named by track metadata;
2. resolve the track robot's base mount body;
3. save its world position/quaternion/yaw into the manifest/track metadata;
4. validate the track entry pose against its facility;
5. fail rather than guess if the source scene is unavailable or ambiguous.

Do not silently fall back to `_SKILL_RECORD_MOUNT`.

## Tests required

### Unit tests

- schema rejects missing or unknown model references;
- scene-table overrides beat inferred values;
- recorded mount is extracted separately for 012 and 042;
- model signature changes when named topology or robot mount changes;
- generated tracks under `_generated/` never enter the skill registry;
- container placement preserves manifest `interior_body`, `support_geom`, and
  motion parameters;
- pin coordinates are fixture-local;
- stale XML/manifest/track signatures fail clearly.

Update the existing container compatibility assertion in
`tests/test_dest_point_anchor.py`: it currently requires container metadata to
remain sourced from legacy `PLACEMENT_REGIONS`, which is the behavior being
removed.

### Integration tests

Build manifests and compile, without starting persistent services, at least:

```text
layout012: move banana_1 to upper_cabinet
layout012: move banana_1 to fridge
layout012: move a compatible object to drawer
layout042: representative cabinet/fridge/drawer moves
```

Assertions:

- the inserted Open skill's replay entry is within tolerance of the facility;
- no navigation target is created outside navigable space due to mount
  retargeting;
- all referenced bodies/joints/geoms exist in the selected model;
- placement generation reaches the correct scene-specific container;
- compiling 012 then 042 then 012 in one process does not reuse stale rig,
  track, navigation-grid, or placement configuration.

`pin p1` should either compile to a validated fixture-local destination or fail
with a precise unsupported-feature error. Silent fallback is not acceptable.

## Acceptance criteria

The handoff is complete when all of the following are true:

1. A new study scene can be added by supplying XML/MJB, a scene table, and its
   fixed tracks without editing `skill_generators.py`.
2. Generated manifests are schema-validated and contain all scene-specific
   runtime configuration listed above.
3. No concrete 012/042 fixture body, joint, geom, or robot mount value is needed
   in generic skill generator code.
4. 012 and 042 fixed replays resolve their own recorded mounts and facility
   geometry.
5. The two current banana commands get past navigation, open, pick, destination
   navigation, and container target generation without legacy-name failures.
6. Cross-scene compile order does not affect results.
7. Invalid or stale scene/track combinations fail during manifest generation or
   initial load with an actionable compatibility error, not a late A*/RRT
   failure.
8. No test or preview process remains running; if backend verification starts
   services, ports 8787, 8899, and 8900 are verified closed afterward.

## Out of scope unless separately assigned

- Re-recording physically incompatible fixtures.
- Making a replay portable across fixtures with different joint topology,
  orientation, handle geometry, or door count.
- Weakening navigation/collision limits to accommodate incorrect metadata.
- Rebuilding the scene XML purely to work around compiler data ownership.
- Storing full trajectory frame arrays inside the manifest.

Fixed replay remains valid only for the scene/model it was recorded against, or
for a retarget explicitly proven compatible by geometry, joint-axis/range, and
base-drive transform checks.
