import { destinationTasks } from "./mockData";
import {
  objectGoalTasksToSemantic,
  semanticTasksToObjectGoals,
} from "./grounding";
import type { AuthoringAction, AuthoringState, RobotId } from "./types";

const regionAssignments: Record<string, RobotId> = {
  "task-mug-1": "robot_a",
  "task-fruit-b": "robot_a",
  "task-mug-2": "robot_b",
  "task-fruit-a": "robot_b",
};

function destinationAssignee(objectName: string): RobotId {
  return objectName.startsWith("mug") ? "robot_a" : "robot_b";
}

export function authoringReducer(
  state: AuthoringState,
  action: AuthoringAction,
): AuthoringState {
  switch (action.type) {
    case "manifest_loaded":
      return { ...state, manifest: action.manifest, groundError: null };
    case "manifest_failed":
      return { ...state, groundError: action.error };
    case "send_message": {
      const content = action.text.trim();
      if (!content || state.pendingGround) return state;
      const messages = [...state.messages, { role: "user" as const, content }];
      return {
        ...state,
        messages,
        pendingGround: {
          requestId: state.nextRequestId,
          messages,
          currentTasks: objectGoalTasksToSemantic(state.tasks),
        },
        nextRequestId: state.nextRequestId + 1,
        groundError: null,
        groundingReason: null,
      };
    }
    case "ground_succeeded":
      if (state.pendingGround?.requestId !== action.requestId) return state;
      return {
        ...state,
        tasks: semanticTasksToObjectGoals(action.tasks, state.manifest, state.tasks),
        messages: [
          ...state.messages,
          {
            role: "assistant",
            content: action.message || action.reason || "Semantic tasks updated.",
          },
        ],
        pendingGround: null,
        groundError: null,
        groundingReason: action.reason,
        deletedTask: null,
      };
    case "ground_failed":
      if (state.pendingGround?.requestId !== action.requestId) return state;
      return { ...state, pendingGround: null, groundError: action.error };
    case "update_intent":
      return { ...state, refinedIntent: action.value };
    case "confirm_intent":
      return {
        ...state,
        phase: "plan_review",
        selectedStrategyId: "by_destination",
        tasks: destinationTasks.map((task) => ({ ...task })),
      };
    case "edit_intent":
      return {
        ...state,
        phase: "intent_review",
        selectedStrategyId: null,
        tasks: [],
        deletedTask: null,
      };
    case "select_strategy":
      return {
        ...state,
        selectedStrategyId: action.strategyId,
        tasks: state.tasks.map((task) => ({
          ...task,
          assignee:
            action.strategyId === "by_destination"
              ? destinationAssignee(task.object.name)
              : regionAssignments[task.id],
        })),
      };
    case "reassign_task":
      return {
        ...state,
        tasks: state.tasks.map((task) =>
          task.id === action.taskId ? { ...task, assignee: action.robotId } : task,
        ),
      };
    case "begin_target_edit":
      return { ...state, editingTargetTaskId: action.taskId, pendingTarget: null };
    case "stage_target":
      return state.editingTargetTaskId
        ? { ...state, pendingTarget: action.target }
        : state;
    case "apply_target":
      if (!state.editingTargetTaskId || !state.pendingTarget) return state;
      const place = state.manifest?.facilities[state.pendingTarget.name]?.place;
      return {
        ...state,
        tasks: state.tasks.map((task) =>
          task.id === state.editingTargetTaskId
            ? {
                ...task,
                target: state.pendingTarget!,
                relation: place?.kind === "container" ? "inside" : "on",
              }
            : task,
        ),
        editingTargetTaskId: null,
        pendingTarget: null,
      };
    case "cancel_target_edit":
      return { ...state, editingTargetTaskId: null, pendingTarget: null };
    case "delete_task": {
      const index = state.tasks.findIndex((task) => task.id === action.taskId);
      if (index < 0) return state;
      return {
        ...state,
        tasks: state.tasks.filter((task) => task.id !== action.taskId),
        deletedTask: { task: state.tasks[index], index },
      };
    }
    case "undo_delete": {
      if (!state.deletedTask) return state;
      const tasks = [...state.tasks];
      tasks.splice(state.deletedTask.index, 0, state.deletedTask.task);
      return { ...state, tasks, deletedTask: null };
    }
    case "expire_undo":
      return { ...state, deletedTask: null };
    case "confirm_plan":
      return { ...state, phase: "plan_confirmed" };
  }
}
