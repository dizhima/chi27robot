import type { AuthoredPlan } from "./planTypes";

/**
 * Built-in sample plan for wiring/verification (P0). Mirrors the skill_generators
 * self-test: robot0 takes mug_1 to the sink and opens the fridge, robot1 takes
 * mug_2 to the sink. Both contend for the sink, so /compile_plan should return a
 * shared-facility WARNING — useful for exercising the warnings path too.
 *
 * Everything here stays within currently-implemented ops: navigate / pick /
 * place (place-into is sink-only in v1) / articulation skill as op. Object,
 * facility and skill names come from the layout042_study manifest.
 */
export const SAMPLE_PLAN: AuthoredPlan = {
  tasks: [
    {
      task: "mug_1 to sink",
      robot: "robot0",
      steps: [
        { op: "navigate", target: "mug_1" },
        { op: "pick", object: "mug_1" },
        { op: "navigate", target: "sink" },
        { id: "r0_place", op: "place", object: "mug_1", dest: "sink" },
      ],
    },
    {
      task: "open fridge",
      robot: "robot0",
      steps: [
        { op: "navigate", target: "fridge" },
        { op: "OpenFridge" },
      ],
    },
    {
      task: "mug_2 to sink",
      robot: "robot1",
      steps: [
        { op: "navigate", target: "mug_2" },
        { op: "pick", object: "mug_2" },
        { id: "r1_nav_sink", op: "navigate", target: "sink" },
        { op: "place", object: "mug_2", dest: "sink" },
      ],
    },
  ],
};

/**
 * Same plan with robot1's sink approach gated behind robot0 finishing its place
 * (`after: [r0_place]`) — the conflict-resolution variant. Handy for eyeballing
 * that the warning clears and the schedule serializes.
 */
export const SAMPLE_PLAN_RESOLVED: AuthoredPlan = {
  tasks: SAMPLE_PLAN.tasks.map((task) => ({
    ...task,
    steps: task.steps.map((step) =>
      step.id === "r1_nav_sink" ? { ...step, after: ["r0_place"] } : step,
    ),
  })),
};

/**
 * Container placement (Phase A — drawers, see docs/container_placement_design.md).
 * robot0 takes mug_1, opens the right drawer, places the mug on the drawer's own
 * (currently open) interior floor, then closes it. Exercises the
 * open -> navigate(container) -> place -> close ordering and dest_point's
 * container branch (place lands on the OPEN drawer's interior, not its rest
 * pose). Verified end-to-end via the orchestrator on 2026-07-26: compiles with
 * zero warnings, place lands at the drawer's actual (partially-open) interior
 * floor position.
 */
export const SAMPLE_PLAN_DRAWER: AuthoredPlan = {
  tasks: [
    {
      task: "mug_1 to drawer_right",
      robot: "robot0",
      steps: [
        { op: "navigate", target: "mug_1" },
        { op: "pick", object: "mug_1" },
        { op: "OpenDrawer" },
        { op: "navigate", target: "drawer_right" },
        { op: "place", object: "mug_1", dest: "drawer_right" },
        { op: "CloseDrawer_stack4" },
      ],
    },
  ],
};

/**
 * Container placement Phase B — front-access upper cabinet.
 *
 * The cabinet starts open in study_init and has no OpenCabinet replay, so this
 * plan deliberately goes straight from pick to navigate/place, then exercises
 * the optional CloseCabinet replay. The place step selects gen_place_reachin
 * through the facility manifest's access:"front" declaration.
 */
export const SAMPLE_PLAN_CABINET: AuthoredPlan = {
  tasks: [
    {
      task: "condiment_bottle_1 to upper_cabinet",
      robot: "robot0",
      steps: [
        { op: "navigate", target: "condiment_bottle_1" },
        {
          op: "pick",
          object: "condiment_bottle_1",
          grasp_mode: "horizontal",
        },
        { op: "navigate", target: "upper_cabinet" },
        {
          op: "place",
          object: "condiment_bottle_1",
          dest: "upper_cabinet",
        },
        { op: "reset", retreat: 0.18, preserve_yaw: true },
        {
          op: "navigate",
          target: "upper_cabinet",
          preserve_yaw: "auto",
        },
        { op: "CloseCabinet" },
      ],
    },
  ],
};

/**
 * Refrigerator placement check. The second fridge navigation is intentionally
 * retained: it establishes the carrying pose that was visually accepted before
 * the reach-in placement begins.
 */
export const SAMPLE_PLAN_FRIDGE: AuthoredPlan = {
  tasks: [
    {
      task: "place apple_1 and apple_2 into fridge",
      robot: "robot0",
      steps: [
        { op: "navigate", target: "fridge" },
        { op: "OpenFridge" },
        { op: "navigate", target: "apple_1" },
        { op: "pick", object: "apple_1" },
        { op: "navigate", target: "fridge" },
        { op: "place", object: "apple_1", dest: "fridge" },
        { op: "reset", retreat: 0.18, preserve_yaw: true },
        { op: "navigate", target: "apple_2" },
        { op: "pick", object: "apple_2" },
        { op: "navigate", target: "fridge" },
        { op: "place", object: "apple_2", dest: "fridge" },
        { op: "reset", retreat: 0.18, preserve_yaw: true },
        { op: "navigate", target: "fridge" },
        { op: "CloseFridge" },
      ],
    },
  ],
};

/**
 * Stops at the exact carrying stance immediately before the first fridge
 * place.  This intentionally omits the failing reach-in so /debug can inspect
 * the compiler-selected base pose, open fixture state, held object and arm
 * configuration independently of placement IK.
 */
export const SAMPLE_PLAN_FRIDGE_PREPLACE: AuthoredPlan = {
  tasks: [
    {
      task: "preview apple_1 pre-place fridge standoff",
      robot: "robot0",
      steps: [
        { id: "preview_nav_open", op: "navigate", target: "fridge" },
        { id: "preview_open", op: "OpenFridge" },
        { id: "preview_reset", op: "reset", retreat: 0.0, preserve_yaw: true },
        { id: "preview_nav_apple", op: "navigate", target: "apple_1" },
        { id: "preview_pick_apple", op: "pick", object: "apple_1" },
        {
          id: "preview_nav_fridge",
          op: "navigate",
          target: "fridge",
          // Exact entry/exit world XY recorded by the 012 OpenFridge replay.
          // This bypasses the generic facility standoff at [0.447, -4.395],
          // which approaches the appliance from its side.
          standoff: [1.325951, -3.621526],
        },
      ],
    },
  ],
};

/**
 * Cross-robot refrigerator placement check for shared WorldState propagation.
 *
 * robot0 opens the fridge and places apple_2, then returns to its actual
 * study_init base XY to clear the doorway. robot1 cannot begin until that
 * clearing navigate completes; it then places apple_1 and closes the fridge.
 * The final robot0 navigate uses fridge only as the required semantic anchor —
 * its debug-only standoff override is robot0's real initial base position.
 */
export const SAMPLE_PLAN_FRIDGE_TWO_ROBOT: AuthoredPlan = {
  tasks: [
    {
      task: "robot0 opens fridge and places apple_2",
      robot: "robot0",
      steps: [
        { id: "r0_nav_open", op: "navigate", target: "fridge" },
        { id: "r0_open", op: "OpenFridge" },
        {
          id: "r0_reset_after_open",
          op: "reset",
          retreat: 0.0,
          preserve_yaw: true,
        },
        { id: "r0_nav_apple_2", op: "navigate", target: "apple_2" },
        { id: "r0_pick_apple_2", op: "pick", object: "apple_2" },
        { id: "r0_nav_fridge", op: "navigate", target: "fridge" },
        {
          id: "r0_place_apple_2",
          op: "place",
          object: "apple_2",
          dest: "fridge",
        },
        {
          id: "r0_reset_after_place",
          op: "reset",
          retreat: 0.18,
          preserve_yaw: true,
        },
        {
          id: "r0_clear_fridge",
          op: "navigate",
          target: "fridge",
          standoff: [4.25723264892, -3.48884336639],
          arrival_yaw: "home",
        },
      ],
    },
    {
      task: "robot1 places apple_1 and closes fridge",
      robot: "robot1",
      steps: [
        {
          id: "r1_nav_apple_1",
          op: "navigate",
          target: "apple_1",
          after: ["r0_clear_fridge"],
        },
        { id: "r1_pick_apple_1", op: "pick", object: "apple_1" },
        { id: "r1_nav_fridge", op: "navigate", target: "fridge" },
        {
          id: "r1_place_apple_1",
          op: "place",
          object: "apple_1",
          dest: "fridge",
        },
        {
          id: "r1_reset_after_place",
          op: "reset",
          retreat: 0.18,
          preserve_yaw: true,
        },
        { id: "r1_nav_close", op: "navigate", target: "fridge" },
        { id: "r1_close", op: "CloseFridge" },
      ],
    },
  ],
};

/**
 * M3d heterogeneous acceptance workflow for layout024_sorting_heter.
 *
 * The logical ids deliberately remain robot0/robot1: morphology comes from
 * the scene manifest. PandaOmron performs the articulation replay while
 * Stretch executes its visually accepted navigate/pick/place/reset sequence.
 * Explicit cross-robot dependencies keep the refrigerator open for the whole
 * transfer and make this useful as a one-click Debug-page regression.
 */
export const SAMPLE_PLAN_HETER_APPLE_FRIDGE: AuthoredPlan = {
  tasks: [
    {
      task: "robot0 opens fridge",
      robot: "robot0",
      steps: [
        { id: "heter_open_nav", op: "navigate", target: "fridge" },
        { id: "heter_open", op: "OpenFridge" },
        {
          id: "heter_open_reset",
          op: "reset",
          retreat: 0.0,
          preserve_yaw: true,
        },
      ],
    },
    {
      task: "robot1 moves apple_1 into fridge",
      robot: "robot1",
      robot_locked: true,
      steps: [
        {
          id: "heter_nav_apple",
          op: "navigate",
          target: "apple_1",
          after: ["heter_open_reset"],
        },
        { id: "heter_pick_apple", op: "pick", object: "apple_1" },
        { id: "heter_nav_fridge", op: "navigate", target: "fridge" },
        {
          id: "heter_place_apple",
          op: "place",
          object: "apple_1",
          dest: "fridge",
        },
        { id: "heter_reset", op: "reset", retreat: 0.18, preserve_yaw: true },
      ],
    },
    {
      task: "robot0 closes fridge",
      robot: "robot0",
      steps: [
        {
          id: "heter_close_reset",
          op: "reset",
          retreat: 0.0,
          preserve_yaw: true,
          after: ["heter_reset"],
        },
        { id: "heter_close_nav", op: "navigate", target: "fridge" },
        { id: "heter_close", op: "CloseFridge" },
        { id: "heter_close_reset_final", op: "reset", retreat: 0.18, preserve_yaw: true },
      ],
    },
  ],
};

/**
 * Focused horizontal-pick visual check. robot1 is intentionally used because
 * its ready pose is closer to condiment_bottle_1 than robot0's.
 */
export const SAMPLE_PLAN_HORIZONTAL_PICK: AuthoredPlan = {
  tasks: [
    {
      task: "robot1 horizontal pick condiment_bottle_1",
      robot: "robot1",
      steps: [
        { op: "navigate", target: "condiment_bottle_1" },
        {
          op: "pick",
          object: "condiment_bottle_1",
          grasp_mode: "horizontal",
          return_to_ready: true,
        },
      ],
    },
  ],
};
