import type { AuthoringState, ObjectGoalTask, Robot, Strategy } from "./types";

export const robots: Robot[] = [
  { id: "robot_a", label: "Robot A" },
  { id: "robot_b", label: "Robot B" },
];

export const strategies: Strategy[] = [
  {
    id: "by_destination",
    label: "By destination",
    description: "Robot A handles mugs; Robot B handles fruit.",
    rationale: "Keeps work for each destination with one robot.",
    recommended: true,
  },
  {
    id: "by_region",
    label: "By island region",
    description: "Each robot clears the side nearest to it.",
    rationale: "Reduces initial travel from the island.",
    recommended: false,
  },
];

export const destinationTasks: ObjectGoalTask[] = [
  {
    id: "task-mug-1",
    type: "object_goal",
    object: { bodyId: 101, name: "mug_1" },
    relation: "inside",
    target: { bodyId: 20, name: "sink" },
    assignee: "robot_a",
  },
  {
    id: "task-mug-2",
    type: "object_goal",
    object: { bodyId: 102, name: "mug_2" },
    relation: "inside",
    target: { bodyId: 20, name: "sink" },
    assignee: "robot_a",
  },
  {
    id: "task-fruit-a",
    type: "object_goal",
    object: { bodyId: 201, name: "fruit_a" },
    relation: "inside",
    target: { bodyId: 30, name: "fridge" },
    assignee: "robot_b",
  },
  {
    id: "task-fruit-b",
    type: "object_goal",
    object: { bodyId: 202, name: "fruit_b" },
    relation: "inside",
    target: { bodyId: 30, name: "fridge" },
    assignee: "robot_b",
  },
];

export function createMockAuthoringState(): AuthoringState {
  return {
    phase: "clarifying",
    refinedIntent: "",
    strategies,
    selectedStrategyId: null,
    tasks: [],
    deletedTask: null,
    editingTargetTaskId: null,
    pendingTarget: null,
    messages: [],
    manifest: null,
    pendingGround: null,
    groundError: null,
    groundingReason: null,
    nextRequestId: 1,
  };
}
