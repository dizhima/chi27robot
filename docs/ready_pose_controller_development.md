# Ready Pose Joint-Hold Controller Development Spec

## 1. Problem and evidence

The default scene loads the `study_init` keyframe correctly, but both Panda arms leave that pose as soon as physics advances. The arm actuators are direct torque motors and the keyframe supplies `qpos` without a matching `ctrl` vector, so gravity and coupling torques move the joints.

A native MuJoCo 3.10 check of `frontend/public/assets/robocasa/layout042_study.xml` showed both robots starting at the expected ready pose and drifting by as much as `0.8433 rad` after 0.5 seconds with zero control input. This is a runtime-control issue, not a missing ready pose in the scene.

## 2. Goal

Add a small `mujoco-react` controller that:

- captures each robot arm's current joint positions after the scene's initial state is applied;
- holds those positions while the application is in free/explore mode;
- discovers robots from the existing XML naming convention instead of depending on one scene path;
- relinquishes control whenever a plan is present so it cannot fight `SchedulePlayer`;
- safely re-captures the current pose after reset, scene replacement, or return from plan mode.

The implementation must work for any scene whose robot arm joints and torque actuators use the same names as the current scenes.

## 3. Non-goals

- Do not modify MJCF/XML files to add position actuators.
- Do not hold mobile-base joints, grippers, props, drawers, or other scene objects.
- Do not change plan compilation, schedule playback, or label visualization.
- Do not hardcode `study_init`, a scene URL, robot count, model IDs, qpos addresses, or ready-pose angles in the controller.
- Do not introduce a general motion controller or inverse-kinematics system.

## 4. Required `mujoco-react` integration

The controller must be a React component mounted inside the existing `<MujocoCanvas>` in `frontend/src/ScenePage.tsx`.

Use public `mujoco-react` APIs available in version 10.7.0:

- `useMujoco()` to access the active simulation API and readiness/model lifecycle;
- `useBeforePhysicsStep(({ model, data }) => ...)` to write torque immediately before every `mj_step`;
- `api.getActuatedJoints()` to obtain names and `qposAdr`, `dofAdr`, `ctrlAdr`, and `ctrlRange` mappings.

Writing `data.ctrl[ctrlAdr]` in the before-step callback is acceptable and avoids React renders in the physics loop. Do not use `useFrame` for physics control.

## 5. File design

Create the following focused modules:

### `frontend/src/readyPoseControl.ts`

Pure, unit-testable logic:

- naming-pattern matching and arm actuator discovery;
- deterministic sorting and validation;
- gain lookup;
- PD plus bias torque calculation;
- finite-value checks and control-range clamping.

### `frontend/src/ReadyPoseController.tsx`

A renderless component that:

- discovers and caches the controlled joints for the current simulation model;
- captures targets from current `data.qpos`;
- runs the controller in `useBeforePhysicsStep`;
- clears only its own control slots when disabled or unmounted;
- warns once per model for incomplete or ambiguous robot mappings and skips unsafe entries.

### `frontend/src/readyPoseControl.test.ts`

Unit coverage for discovery, sorting, validation, torque calculation, clamping, and invalid numeric inputs.

Modify `frontend/src/ScenePage.tsx` only for controller mounting and capture lifecycle wiring.

## 6. Robot discovery contract

Default naming rules:

```text
joint:    ^robot(\d+)_joint([1-7])$
actuator: ^robot(\d+)_torq_j([1-7])$
```

For every controlled entry, the robot number and joint number extracted from the joint and actuator names must match. Sort entries numerically by robot number and then joint number; do not rely on XML declaration order.

An arm is eligible only when all seven unique joint/actuator pairs are present. If a robot has a duplicate, missing pair, invalid address, or unusable control range, skip that robot as a unit and emit one concise warning. One malformed robot must not disable other valid robots.

This convention means `robot0`, `robot1`, and additional identically named robots are discovered automatically in another scene. A future optional explicit configuration may override the regular expressions, but no override UI is required now.

Portability assumptions:

- arm actuators remain direct torque motors with the current naming convention;
- their gear/transmission semantics remain compatible with direct joint torque (current scenes use the default unit gear);
- each arm joint is a scalar hinge joint;
- control limits are exposed through the actuator mapping.

If these assumptions change, the controller must fail closed by skipping the incompatible arm rather than writing guessed controls.

## 7. Target-capture lifecycle

`ReadyPoseController` should accept at least:

```ts
type ReadyPoseControllerProps = {
  enabled: boolean;
  captureRevision: number;
};
```

Capture current qpos values only after discovery succeeds. A capture is required when:

1. a new MuJoCo model/API becomes ready;
2. `captureRevision` changes after the caller applies reset/keyframe state;
3. `enabled` transitions from `false` to `true` after plan mode.

Do not recapture on ordinary React renders or every physics step. Otherwise the controller would continually redefine the target to the drifting pose and provide no hold.

In `ScenePage`, maintain a monotonic revision. Every code path that calls `applyInitialSceneState(api)` must increment the revision after the reset/keyframe application. The ready callback must apply initial state first and then request capture. Returning from a loaded plan to no-plan mode must capture the current pose through the `false -> true` rule.

The first before-step callback after a pending capture must snapshot qpos and immediately calculate torque in that same callback. `mujoco-react` can execute many `mj_step` calls after one before-step callback, so returning immediately after capture would leave an entire rendered frame uncontrolled rather than skipping only one physics step.

Scene confirmation can temporarily replace the active MuJoCo model without changing the public API object's identity or returning React readiness to `false`. Track the actual model object passed to `useBeforePhysicsStep`; when its identity changes, invalidate actuator addresses, rediscover the arm mapping, and capture a fresh target. Do not treat a transient empty model as the final mapping for a later model instance.

## 8. Control law

For each eligible joint:

```text
error = targetQpos - data.qpos[qposAdr]
tau = data.qfrc_bias[dofAdr] + kp[jointIndex] * error - kd[jointIndex] * data.qvel[dofAdr]
ctrl = clamp(tau, ctrlRangeMin, ctrlRangeMax)
```

`qfrc_bias` supplies gravity/Coriolis compensation; the PD terms correct pose and velocity error. Do not add `qfrc_applied` because the model already exposes torque actuators.

Conservative initial Panda gains, indexed from joint 1 through 7:

```ts
kp = [80, 80, 60, 60, 30, 20, 15]
kd = [12, 12, 10, 8, 5, 4, 3]
```

Keep these defaults in one exported constant so they can be tuned. Always clamp to each actuator's `ctrlRange`. If any input or calculated torque is non-finite, write zero to that controller-owned slot for the step and avoid propagating NaN.

## 9. Plan and ownership rules

Mount the component inside `MujocoCanvas` with:

```tsx
<ReadyPoseController
  enabled={!hasPlan}
  captureRevision={readyPoseRevision}
/>
```

The existing canvas is paused whenever `hasPlan` is true, and `SchedulePlayer` writes planned qpos. The hold controller must therefore be disabled for the entire lifetime of a draft/loaded plan, not only during active playback.

On disable:

- stop writing torque immediately;
- zero only the discovered arm actuator slots so stale hold commands cannot survive;
- mark the target stale so the next enable captures the then-current pose.

Never zero all controls because other controllers may own other actuators.

## 10. Safety and failure behavior

- Never throw from the physics-step callback for a scene naming mismatch.
- Do not write outside `data.ctrl`, `data.qpos`, or `data.qvel` bounds.
- Require unique valid addresses and a valid two-value range before controlling an entry.
- Warnings must identify the skipped robot/name and reason, but must be de-duplicated to avoid console spam.
- Component unmount/model replacement must clear cached mappings and targets.
- Avoid per-step object/array allocation where practical.

## 11. Verification

### Unit tests

Cover at minimum:

- two robots discovered even when actuator info is shuffled;
- numeric ordering (`robot2` before `robot10`);
- unrelated base/gripper actuators ignored;
- incomplete, duplicate, mismatched, or invalid-address arms skipped;
- PD+bias result with zero and nonzero error/velocity;
- torque clamped at both limits;
- NaN/Infinity produces safe zero.

### Frontend verification

- Run the existing frontend test command with the new focused test.
- Run `npm run build` in `frontend`.
- Confirm TypeScript does not depend on private `mujoco-react` internals.

### Runtime/headless verification

Using the default scene and its initial keyframe:

- confirm both `robot0` and `robot1` expose seven eligible arm joints;
- simulate at least 0.5 seconds with the same control law;
- maximum absolute arm-joint error should remain below `0.02 rad` and all controls must stay finite and within their ranges.

If the browser runtime is exercised, also check that loading a plan disables the controller, clearing the plan re-captures without a visible jump, and switching to a second same-named scene rediscovers the model instead of retaining stale addresses.

## 12. Acceptance criteria

The work is complete when:

1. both arms remain visually at their captured ready pose in the default no-plan view;
2. the pure discovery logic supports any count of robots following the naming contract;
3. plan preview/playback behavior is unchanged and never competes with hold torques;
4. reset and scene replacement result in fresh targets;
5. malformed or absent robot mappings degrade safely;
6. focused tests and frontend build pass;
7. any agent-started test processes are stopped, and ports `8787`, `8899`, and `8900` are checked if the backend stack was started.
