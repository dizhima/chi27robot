import { useSelectionHighlight } from "mujoco-react";
import type { MujocoLoader, SceneConfig } from "mujoco-react";
import loadMujocoThreaded from "@mujoco/mujoco/mt";

export { default as mtMujocoWasmUrl } from "@mujoco/mujoco/mt/mujoco.wasm?url";

// Endpoint config lives in config.ts (wasm-free) so pure logic can import it
// without pulling the MuJoCo loader; re-exported here for existing consumers.
export { backendUrl, skillServiceUrl } from "./config";

export const defaultScenePath = "assets/robocasa/layout042_study.xml";

export const defaultSceneConfig: SceneConfig = {
  src: "/",
  sceneFile: defaultScenePath,
};

/**
 * Named keyframe holding a scene's initial session state (e.g. the study
 * scene starts with a cabinet left open). Applied on load only when the
 * loaded scene actually declares it, so other scenes are unaffected.
 */
export const initialStateKeyframe = "study_init";

const requestedMujocoInitialMemoryMb = Number(
  import.meta.env.VITE_MUJOCO_INITIAL_MEMORY_MB || 2048,
);
const mujocoInitialMemoryMb = Math.min(
  2048,
  Math.max(16, requestedMujocoInitialMemoryMb),
);
const mujocoInitialMemory = mujocoInitialMemoryMb * 1024 * 1024;

export const threadedMujocoLoader: MujocoLoader = (options) =>
  loadMujocoThreaded({
    ...options,
    INITIAL_MEMORY: mujocoInitialMemory,
  } as Parameters<typeof loadMujocoThreaded>[0]);

export type SceneArtifactStatus = {
  path: string;
  exists: boolean;
};

export type SceneSession = {
  ok: boolean;
  scenePath: string;
  sceneDir: string;
  src: string;
  sceneFile: string;
  sceneExists: boolean;
  skillRuntime?: {
    sceneXml: string;
    studyName: string;
    manifest: string;
    standoffs: string;
    tracks: string;
    available: boolean;
    missing: Array<{ kind: string; path: string }>;
  };
  manifest: SceneArtifactStatus;
  taskPlan: SceneArtifactStatus;
  compile?: {
    scenePath: string;
    compiled: boolean;
    cached: boolean;
  };
  error?: string;
};

/** Applies emissive highlight to the currently selected MuJoCo body. */
export function SelectionHighlight({ bodyId }: { bodyId: number | null }) {
  useSelectionHighlight(bodyId, { color: "#22c55e", emissiveIntensity: 0.55 });
  return null;
}
