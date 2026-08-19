# Skill-track coordinate-translation retarget

**Problem this solves.** RoboCasa's atomic-task dataset is uneven: for a given
layout you may have `CloseDrawer` recorded on one drawer but `OpenDrawer` only
on a *different* drawer (and never the pair on the same one). Rather than
tele-operate a missing demo, you can sometimes **synthesise** the missing skill
by taking an existing demo on fixture A and replaying it on the geometrically
identical fixture B, shifting the whole motion (robot base + manipulation) by
the rigid offset between A and B.

This is a cheap, physically faithful alternative to two other fallbacks:

* **Reverse playback** (play the Close demo backwards to fake an Open): only
  valid for kinematic playback, and for hinge doors the reversed arm motion
  often looks unnatural. Prefer translation retarget when a twin fixture exists.
* **Initial-state trick** (start the scene with the door already open, so only
  the Close demo is needed): always available, but changes the task narrative.

## When it is valid (the tool enforces all of these)

Retarget from `src_joint` (fixture A) to `dst_joint` (fixture B) is admissible
only when A and B differ by a **pure translation the robot base can drive**:

1. **Same orientation** — the two fixture bodies have the same world
   quaternion (within `QUAT_TOL`). If they face different directions, a pure
   translation would misalign the arm; this is why the trick does **not**
   generalise to hinge cabinets on differently-oriented walls.
2. **Same joint axis** — the two joints' world axes match (within `AXIS_TOL`).
3. **Same range** — identical `jnt_range`, so the manipulation values transfer
   verbatim.
4. **Reachable by translation** — the world offset `Δ = anchor(B) − anchor(A)`
   lies in the executing robot's horizontal driving plane, i.e. it is fully
   spanned by the base `mobile_forward` and `mobile_side` axes (vertical
   residual within `RESIDUAL_TOL`).

If any check fails the tool raises with the reason instead of emitting a
distorted track.

## How the shift is applied

* **Base translation.** `Δ` is projected onto the executing robot's own base
  axes (read from the compiled model, *not* hard-coded): `fwd_off = Δ · fwd_axis`,
  `side_off = Δ · side_axis`. Each base translation channel gets that constant
  added to every frame. This matters because the two robots in `layout042_study`
  are rotated 180° relative to each other, so the same world offset lands on
  `side` for robot0 but on `forward` for robot1 — the projection handles that
  automatically.
* **Manipulation & arm channels.** Left untouched. Because the base moved by the
  same `Δ` as the fixture, the arm's pose *relative to the fixture* is preserved,
  so grasp and contact stay valid.
* **Fixture channel.** Renamed from `src_joint` to `dst_joint`; its values
  (the door/drawer opening curve) are copied unchanged.

## Why it is exact here

For the two `layout042_study` drawers (`stack_4_right_group_1_2` →
`stack_2_right_group_1_3`) the fixtures are identical prefabs differing by
`Δ = (0, +1.560, 0)` m, same quat, same axis `(1,0,0)`, same range `[-0.6, 0]`.
Kinematic check: after retarget the gripper-to-drawer distance profile matches
the original demo to floating-point precision (`max|diff| ≈ 1e-15`), and the
drawer slide reproduces `0 → -0.426`.

## Reusing on other layouts

1. Identify a fixture that has the demo you need (A) and the target fixture that
   lacks it (B); both must be the same fixture *type/prefab*.
2. Find their joint names (the tool prints `fixtures=[...]` per extracted skill;
   or inspect the scene with MuJoCo and look at `jnt_qposadr`).
3. Run `extract_skill_tracks.py` with
   `--retarget robot{i}:SKILL:SRC_JOINT:DST_JOINT:NEW_SKILL`.
4. If it errors on orientation/axis/range/residual, the fixtures are **not**
   twins — fall back to the initial-state trick or tele-operate that one demo.

See `tools/extract_skill_tracks.py`.
