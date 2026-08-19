# Multi-object container resting physics

## Why this follow-up exists

Container placement currently supports physical settling and articulation replay
for one object resting in one container. It looks physically correct for a
single mug in a drawer (or one object followed by an Open/Close replay), but it
does not preserve a set of objects placed in the same container.

The desired behavior is generic: if several compatible pickable objects are
placed in the same drawer, cabinet, or fridge, a later Open/Close replay must
simulate all of them together as MuJoCo free bodies. No object-specific mug or
apple logic should be needed.

## Current limitation

`compile_robot` in `src/mujoco_skills/skills/skill_generators.py` keeps:

```python
resting: dict[str, tuple]
# facility -> (obj_name, (position, quaternion))
```

After each container place it assigns:

```python
resting[facility] = (obj, settled_pose)
```

Therefore a second placed object in the same facility overwrites the first
object's resting-physics state.

`_replay_with_resting_object` also accepts and records exactly one object free
joint. The browser playback is deterministic: it replays object poses generated
by this backend physics simulation, rather than running browser-side physics.

## Target state model

Use a per-facility collection of resting objects:

```python
RestPose = tuple[np.ndarray, np.ndarray]  # position, quaternion
resting: dict[str, dict[str, RestPose]]
# facility -> {object_name -> (position, quaternion)}
```

Consequences:

- A place adds or updates only its own object entry.
- Picking an object removes only that object from every facility map.
- An empty facility map is removed.
- Reset, later place operations, and articulation replays must thread every
  stored object's latest pose into the scratch qpos.

## Required implementation changes

### 1. Multi-object settling after place

Replace the one-object settle path with a helper conceptually like
`_settle_resting_objects_physically`.

Inputs:

- compiled qpos at release;
- all prior resting object poses in that facility;
- the newly released object's estimated pose.

Behavior:

1. Write every object's free-joint pose into `data.qpos`.
2. Leave all those objects dynamic; do not kinematically freeze old objects.
3. Run the short settle window with `mj_step`.
4. Return updated poses for **all** resting objects.
5. Extend the just-generated place track with channels for every object whose
   pose moves during this settle interval, so frontend playback remains
   continuous.

This lets the new object collide with and settle against objects already in the
container.

### 2. Multi-object articulation replay

Generalize `_replay_with_resting_object` to a plural helper, for example:

```python
_replay_with_resting_objects(rig, q, raw_track, resting_objects)
```

Important invariants:

- `raw_track` is the canonical pre-recorded articulation track and may script
  only scalar robot/fixture channels.
- Every resting object stays a dynamic free body during `mj_step`.
- At each recorded time, update the scripted robot/fixture channels, step the
  physics, and record every object's `[x, y, z, qw, qx, qy, qz]` pose.
- The returned augmented track has one free-joint channel per resting object.
- Return a new pose map to update `resting[facility]`.

The contact solver then naturally handles object-object, object-drawer, and
object-door contact in one simulation.

### 3. Compiler bookkeeping

Update `compile_robot` branches for:

- `place`: merge the result of multi-object settling into the facility map;
- `pick`: remove only the picked object from resting state;
- `reset`: write all resting pose maps into scratch qpos before collision
  checks;
- articulation: invoke the plural replay whenever the target facility has at
  least one resting object;
- future Open/Close operations: carry forward the returned map, not one pose.

The existing `dest_point` slot allocator and placement-distance conflict check
remain useful. Physical settling is the final check that objects fit without
unstable overlap.

### 4. Keep generated tracks out of the LLM manifest

This is a separate prerequisite discovered during cabinet testing.

Augmented tracks such as `CloseCabinet_with_condiment_bottle_1` contain a
7-DoF object channel. They are compile-internal artifacts, not canonical skills.
If exposed in `skills_manifest.json`, an LLM can select one as a raw Close
skill; a second augmentation then fails the scalar-channel assertion.

`build_skills_manifest.py` should expose only canonical articulation skill names
declared by the scene table's facility articulation configuration. Generated
tracks—including `*_with_*`, `navigate_*`, `pick_*`, `place_*`, `reset_*`, and
`reposition_*`—must not be manifest skills. The compiler may still generate and
serve them for a schedule produced in the current compile.

## Compatibility requirements

The change must not regress:

- Phase A: one-object drawer place -> CloseDrawer physical replay;
- Phase B: one-object upper-cabinet place -> CloseCabinet;
- Phase B: two-apple fridge place -> CloseFridge;
- reset's collision-checked backward retreat;
- normal pick/place object attachment tracks;
- canonical Open/Close replay without any resting object.

## Test matrix

Run both direct `compile_plan` and the real two-stage orchestrator path.

1. One mug -> drawer -> reset -> navigate -> CloseDrawer.
2. Two compatible objects -> same drawer -> CloseDrawer; inspect both free-joint
   channels in the augmented close track.
3. Open drawer after close; confirm both poses continue from the close result.
4. Pick one object back out; confirm only it leaves the facility map.
5. Two apples -> fridge -> reset/navigate -> CloseFridge.
6. Condiment bottle -> upper cabinet using horizontal pick -> CloseCabinet.
7. Rebuild manifest after the above tests; verify no `*_with_*` entries appear
   under `skills`.
8. `/debug` visual review for no snap, tunnelling, or object pop at place/open/
   close boundaries.

## Non-goals for the first implementation

- Do not introduce browser-side live physics; backend-generated deterministic
  tracks remain the playback contract.
- Do not support arbitrary packing optimization. Existing slots choose intended
  release regions; MuJoCo determines the final local settle.
- Do not change Stage 1, loop.py, provider behavior, or the two-stage flow.
