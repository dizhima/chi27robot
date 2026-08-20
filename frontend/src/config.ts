/**
 * Runtime endpoint config, kept free of the MuJoCo wasm imports so pure logic
 * (e.g. plan/compilePlan.ts) can depend on it and stay unit-testable under
 * vitest/node without mocking the wasm loader.
 */

/** Browser page hostname, or loopback when evaluated under vitest/node. */
function pageHostname(): string {
  if (typeof window !== "undefined" && window.location.hostname) {
    const hostname = window.location.hostname;
    return hostname === "localhost" ? "127.0.0.1" : hostname;
  }
  return "127.0.0.1";
}

function serviceBaseUrl(envValue: string | undefined, port: number): string {
  if (envValue) return envValue.replace(/\/$/, "");
  return `http://${pageHostname()}:${port}`;
}

/** Node bridge (scene open/compile, trajectories, skills manifest). */
export const backendUrl = serviceBaseUrl(import.meta.env.VITE_CODEX_BACKEND_URL, 8787);

/**
 * Warm skill/plan service (tools/skill_service.py). Answers POST /compile_plan.
 * Runs on its own port because loading the MuJoCo model + rigs is slow and must
 * happen once; CORS is open there, so the browser can call it directly.
 */
export const skillServiceUrl = serviceBaseUrl(import.meta.env.VITE_SKILL_SERVICE_URL, 8899);

/**
 * Orchestrator service (src/mujoco_skills/orchestrator/service.py). Answers the
 * LLM authoring endpoints POST /author and POST /ground. Separate from
 * skill_service because it needs OpenAI + corporate TLS but not the MuJoCo wasm.
 */
export const orchestratorUrl = serviceBaseUrl(import.meta.env.VITE_ORCHESTRATOR_URL, 8900);
