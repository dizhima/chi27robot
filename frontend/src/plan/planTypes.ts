/**
 * Step-level authoring schema (what the LLM emits and the user edits) plus the
 * shape of the /compile_plan response. Kept UI-free so any surface — the debug
 * verification harness or the redesigned scene page — can share one contract.
 *
 * Mirrors tools/skill_service.py (POST /compile_plan) and the schema in
 * tools/llm_tools.json. Spatial fields (at/via_points/standoff) are NOT authored
 * by the LLM; the backend fills them and echoes them back on `completed`.
 */

export type RobotName = "robot0" | "robot1";

/**
 * One authored step. `op` is navigate|pick|place|reset|wait or an articulation
 * skill name (e.g. "OpenFridge"). Extra keys are allowed because the backend writes
 * spatial defaults (standoff/via_points/route/at) onto the completed copy.
 */
export type AuthoredStep = {
  id?: string;
  op: string;
  target?: string; // navigate: object or facility name
  object?: string; // pick/place: object name
  dest?: string; // place: destination facility name
  duration?: number; // wait: seconds
  retreat?: number; // reset: backward distance in metres
  preserve_yaw?: boolean | "auto"; // reset/navigate: avoid unnecessary turning
  align_final_yaw?: boolean; // navigate: false keeps the final travel-facing yaw
  arrival_yaw?: "home"; // navigate: adopt the robot's initial yaw before the final leg
  final_yaw?: "home"; // navigate: follow the normal route, then adopt initial yaw at the endpoint
  return_to_ready?: boolean; // horizontal pick; defaults to true
  at?: [number, number]; // place: explicit drop point (overrides auto slot)
  at_anchor?: [number, number]; // placement distribution anchor from a pinned reference; backend-filled, passthrough
  via_points?: [number, number][]; // navigate: authored intermediate constraints
  route?: [number, number][]; // navigate: backend-derived full route, read-only
  standoff?: [number, number]; // backend default or user-adjusted navigate endpoint
  after?: string[]; // step ids (any robot) this step must start after
  [key: string]: unknown;
};

/** One robot's ordered group of steps. */
export type AuthoredTask = {
  task?: string; // human-readable label
  robot: RobotName;
  /** User explicitly selected this robot; automated coordination cannot hand it off. */
  robot_locked?: boolean;
  steps: AuthoredStep[];
};

/** The authoring plan sent to /compile_plan (nested tasks form). */
export type AuthoredPlan = {
  tasks: AuthoredTask[];
};

/** One compiled, scheduled track item as returned by /compile_plan. */
export type ScheduleEntry = {
  /** Stable id of the authored step this item came from (for edit mapping). */
  id: string;
  /** Step ids this item waits on (echoes the authored `after`). */
  after: string[];
  robot: string;
  label: string;
  /** Absolute start time (seconds) already resolved by the backend scheduler. */
  start: number;
  duration: number;
  op?: string | null;
  facility: string | null;
  object: string | null;
  group: string | null;
  robot_locked?: boolean;
  /** Compiler-generated coordination metadata used by the task projection. */
  source?: string | null;
  repair_kind?: string | null;
  parent_group?: string | null;
  detour_overridden?: boolean;
  /** Public path to the generated track JSON, e.g. /trajectories/.../x.track.json */
  track_url: string;
};

/**
 * One structured cross-robot/placement conflict from the backend detector.
 * `steps` are the exact authored-step ids in contention (used to tint precisely
 * the involved Gantt bars, rather than substring-matching the message).
 */
export type Conflict = {
  kind: "placement" | "facility" | "object" | "path";
  steps: string[];
  robots: string[];
  /** Overlap window [start, end] in seconds; null for time-independent (placement). */
  window: [number, number] | null;
  detail: Record<string, unknown>;
  message: string;
};

/** Full /compile_plan response. */
export type CompileResponse = {
  /** Opaque handle for reusing this exact compile as Resolver V2's round-0 observation. */
  compile_id?: string;
  compiler_generation?: string;
  schedule: ScheduleEntry[];
  warnings: string[];
  /** Structured form of `warnings`; drives exact per-step bar tinting. */
  conflicts: Conflict[];
  completed: Record<string, AuthoredStep[]>;
};
