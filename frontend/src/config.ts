/**
 * Runtime endpoint config, kept free of the MuJoCo wasm imports so pure logic
 * (e.g. plan/compilePlan.ts) can depend on it and stay unit-testable under
 * vitest/node without mocking the wasm loader.
 */

/** Node bridge (scene open/compile, trajectories, skills manifest). */
export const backendUrl =
  import.meta.env.VITE_CODEX_BACKEND_URL || "http://127.0.0.1:8787";

/**
 * Warm skill/plan service (tools/skill_service.py). Answers POST /compile_plan.
 * Runs on its own port because loading the MuJoCo model + rigs is slow and must
 * happen once; CORS is open there, so the browser can call it directly.
 */
export const skillServiceUrl =
  import.meta.env.VITE_SKILL_SERVICE_URL || "http://127.0.0.1:8899";

/**
 * Orchestrator service (src/mujoco_skills/orchestrator/service.py). Answers the
 * LLM authoring endpoints POST /author and POST /ground. Separate from
 * skill_service because it needs OpenAI + corporate TLS but not the MuJoCo wasm.
 */
export const orchestratorUrl =
  import.meta.env.VITE_ORCHESTRATOR_URL || "http://127.0.0.1:8900";
