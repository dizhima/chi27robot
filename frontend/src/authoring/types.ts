export type AuthoringPhase =
  | "clarifying"
  | "intent_review"
  | "plan_review"
  | "plan_confirmed";

export type SceneRef = { bodyId: number; name: string };
export type RobotId = "robot_a" | "robot_b";
export type StrategyId = "by_destination" | "by_region";

export type Robot = { id: RobotId; label: string };

export type Strategy = {
  id: StrategyId;
  label: string;
  description: string;
  rationale: string;
  recommended: boolean;
};

export type ObjectGoalTask = {
  id: string;
  type: "object_goal";
  object: SceneRef;
  relation: "inside" | "on";
  target: SceneRef;
  assignee: RobotId;
};

export type ChatMessage = {
  role: "user" | "assistant";
  content: string;
};

export type SemanticTask = {
  id: string;
  action: "move";
  object: string;
  dest: string;
};

export type SceneManifestStandoff = {
  standoff_xy?: [number, number];
  face_xy?: [number, number];
};

export type SceneManifest = {
  robots?: Record<string, {
    index?: number;
    type?: string;
  }>;
  objects: Record<string, {
    body?: string | number;
    label?: string;
    show_scene_label?: boolean;
    world_pos?: [number, number, number] | number[] | null;
  }>;
  facilities: Record<
    string,
    {
      body?: string | number;
      label?: string;
      show_scene_label?: boolean;
      scene_label_xy?: [number, number];
      scene_label_z?: number;
      world_pos?: [number, number, number] | number[] | null;
      standoff?: SceneManifestStandoff | null;
      articulation?: unknown | null;
      place?: { kind?: "container" | "surface"; surface_body?: string } | null;
    }
  >;
};

export type PendingGround = {
  requestId: number;
  messages: ChatMessage[];
  currentTasks: SemanticTask[];
};

export type AuthoringState = {
  phase: AuthoringPhase;
  refinedIntent: string;
  strategies: Strategy[];
  selectedStrategyId: StrategyId | null;
  tasks: ObjectGoalTask[];
  deletedTask: { task: ObjectGoalTask; index: number } | null;
  editingTargetTaskId: string | null;
  pendingTarget: SceneRef | null;
  messages: ChatMessage[];
  manifest: SceneManifest | null;
  pendingGround: PendingGround | null;
  groundError: string | null;
  groundingReason: string | null;
  nextRequestId: number;
};

export type AuthoringAction =
  | { type: "manifest_loaded"; manifest: SceneManifest }
  | { type: "manifest_failed"; error: string }
  | { type: "send_message"; text: string }
  | {
      type: "ground_succeeded";
      requestId: number;
      tasks: SemanticTask[];
      message: string;
      reason: string | null;
    }
  | { type: "ground_failed"; requestId: number; error: string }
  | { type: "confirm_intent" }
  | { type: "update_intent"; value: string }
  | { type: "edit_intent" }
  | { type: "select_strategy"; strategyId: StrategyId }
  | { type: "reassign_task"; taskId: string; robotId: RobotId }
  | { type: "begin_target_edit"; taskId: string }
  | { type: "stage_target"; target: SceneRef }
  | { type: "apply_target" }
  | { type: "cancel_target_edit" }
  | { type: "delete_task"; taskId: string }
  | { type: "undo_delete" }
  | { type: "expire_undo" }
  | { type: "confirm_plan" };
