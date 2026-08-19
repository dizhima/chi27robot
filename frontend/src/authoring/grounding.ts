import { backendUrl } from "../config";
import type {
  ObjectGoalTask,
  PendingGround,
  RobotId,
  SceneManifest,
  SemanticTask,
} from "./types";

export const orchestratorUrl =
  import.meta.env.VITE_ORCHESTRATOR_URL || "http://127.0.0.1:8900";

export function uiRobotToBackend(robot: RobotId): "robot0" | "robot1" {
  return robot === "robot_a" ? "robot0" : "robot1";
}

export function backendRobotToUi(robot: "robot0" | "robot1"): RobotId {
  return robot === "robot0" ? "robot_a" : "robot_b";
}

function bodyId(value: string | number | undefined): number {
  return typeof value === "number" ? value : 0;
}

function relationForDestination(
  destination: string,
  manifest: SceneManifest | null,
): "inside" | "on" {
  const place = manifest?.facilities[destination]?.place;
  return place?.kind === "container" ? "inside" : "on";
}

export function objectGoalTasksToSemantic(tasks: ObjectGoalTask[]): SemanticTask[] {
  return tasks.map((task) => ({
    id: task.id,
    action: "move",
    object: task.object.name,
    dest: task.target.name,
  }));
}

export function semanticTasksToObjectGoals(
  tasks: SemanticTask[],
  manifest: SceneManifest | null,
  previous: ObjectGoalTask[],
): ObjectGoalTask[] {
  const priorById = new Map(previous.map((task) => [task.id, task]));
  return tasks.map((task, index) => ({
    id: task.id,
    type: "object_goal",
    object: {
      name: task.object,
      bodyId: bodyId(manifest?.objects[task.object]?.body),
    },
    relation: relationForDestination(task.dest, manifest),
    target: {
      name: task.dest,
      bodyId: bodyId(manifest?.facilities[task.dest]?.body),
    },
    assignee:
      priorById.get(task.id)?.assignee ??
      backendRobotToUi(index % 2 === 0 ? "robot0" : "robot1"),
  }));
}

export async function loadSceneManifest(): Promise<SceneManifest> {
  const response = await fetch(`${backendUrl}/api/skills`);
  const payload = await response.json();
  if (!response.ok || !payload.ok || !payload.manifest) {
    throw new Error(payload.error || payload.reason || "Unable to load scene manifest");
  }
  return payload.manifest as SceneManifest;
}

export type GroundResponse = {
  tasks: SemanticTask[];
  message: string;
  reason: string | null;
};

export async function requestGround(pending: PendingGround): Promise<GroundResponse> {
  const response = await fetch(`${orchestratorUrl}/ground`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      messages: pending.messages,
      current_tasks: pending.currentTasks,
    }),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `Grounding failed (${response.status})`);
  }
  return payload as GroundResponse;
}
