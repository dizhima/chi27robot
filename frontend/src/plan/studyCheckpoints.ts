import { backendUrl } from "../config";
import type { AugmentedAction } from "./authorPlan";
import type { AuthoredPlan, CompileResponse } from "./planTypes";

export type SavedCheckpointSummary = {
  id: string;
  createdAt: string;
  sceneFile: string;
  taskCount: number;
};

export type SavedCheckpoint = SavedCheckpointSummary & {
  format: "mujoco-study-checkpoint-v1";
  scene: { file: string; fingerprint: string };
  snapshot: {
    plan: AuthoredPlan;
    compile: CompileResponse;
    semanticActions: AugmentedAction[];
  };
};

async function responsePayload<T>(response: Response): Promise<T> {
  const payload = (await response.json().catch(() => ({}))) as T & { error?: string };
  if (!response.ok) throw new Error(payload.error || `checkpoint request failed: ${response.status}`);
  return payload;
}

export async function listStudyCheckpoints(): Promise<SavedCheckpointSummary[]> {
  const response = await fetch(`${backendUrl}/api/study/checkpoints?t=${Date.now()}`);
  const payload = await responsePayload<{ checkpoints: SavedCheckpointSummary[] }>(response);
  return payload.checkpoints;
}

export async function saveStudyCheckpoint(snapshot: SavedCheckpoint["snapshot"]): Promise<SavedCheckpointSummary> {
  const response = await fetch(`${backendUrl}/api/study/checkpoints`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ snapshot }),
  });
  const payload = await responsePayload<{ checkpoint: SavedCheckpointSummary }>(response);
  return payload.checkpoint;
}

export async function loadStudyCheckpoint(id: string): Promise<SavedCheckpoint> {
  const response = await fetch(
    `${backendUrl}/api/study/checkpoints/${encodeURIComponent(id)}?t=${Date.now()}`,
  );
  const payload = await responsePayload<{ checkpoint: SavedCheckpoint }>(response);
  return payload.checkpoint;
}
