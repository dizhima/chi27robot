/**
 * Client for the orchestrator's path-Y authoring endpoint (POST /author):
 * natural language + the previous turn's semantic actions -> the agentic
 * authoring loop -> deterministic decompose. Returns both the semantic
 * AugmentedAction[] (echoed back next turn for refine/AB consistency) and the
 * decomposed AuthoredPlan (fed straight into the existing compile pipeline).
 *
 * Mirrors src/mujoco_skills/orchestrator/schema.py (AugmentedAction) and
 * service.py (do_author).
 */
import { orchestratorUrl } from "../config";
import type { AuthoredPlan, RobotName } from "./planTypes";

/** One semantic action as emitted by augment/propose_plan. Spatial fields are
 * NOT present here — they live only on the decomposed steps. */
export type AugmentedAction = {
  id: string;
  robot: RobotName;
  op: "move" | "open" | "close" | "go_to";
  object?: string | null;
  dest?: string | null;
  facility?: string | null;
  target?: string | null;
  via_points?: [number, number][] | null;
  serves?: string | null;
  robot_locked?: boolean;
  /** Semantic task predecessors.  Decompose maps these to first/last steps. */
  after?: string[] | null;
};

export type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  /** Resolver reports are visible in the shared Chat but are not authoring context. */
  source?: "authoring" | "resolver";
};

export type AuthorResponse = {
  actions: AugmentedAction[];
  /** null when nothing could be grounded (see `reason`). */
  plan: AuthoredPlan | null;
  message: string;
  reason: string | null;
};

export async function requestAuthor(
  messages: ChatMessage[],
  currentActions: AugmentedAction[],
  opts: { serviceUrl?: string; signal?: AbortSignal } = {},
): Promise<AuthorResponse> {
  const base = opts.serviceUrl ?? orchestratorUrl;
  let response: Response;
  try {
    response = await fetch(`${base}/author`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages, current_actions: currentActions }),
      signal: opts.signal,
    });
  } catch (err) {
    throw new Error(
      `author request failed (is the orchestrator service running at ${base}?): ${
        err instanceof Error ? err.message : String(err)
      }`,
    );
  }
  const payload = (await response.json().catch(() => ({}))) as
    | AuthorResponse
    | { error?: string };
  if (!response.ok || "error" in payload) {
    const detail =
      "error" in payload && payload.error ? payload.error : response.status;
    throw new Error(`author failed: ${detail}`);
  }
  return payload as AuthorResponse;
}

/**
 * Round-trip task ordering and robot reassignments made in the plan editor back
 * onto semantic actions before the next /author turn. The semantic projection
 * deliberately ignores compiler-generated tasks that have no matching action.
 * Spatial edits (via_points/at/standoff) live below the semantic layer and are
 * intentionally not preserved.
 */
export function applyRobotOverrides(
  actions: AugmentedAction[],
  plan: AuthoredPlan | null,
): AugmentedAction[] {
  if (!plan) return actions;
  const robotByActionId = new Map<string, { robot: RobotName; locked: boolean }>();
  for (const task of plan.tasks) {
    if (task.task) {
      robotByActionId.set(task.task, {
        robot: task.robot,
        locked: task.robot_locked === true,
      });
    }
  }
  const updated = actions.map((action) => {
    const override = robotByActionId.get(action.id);
    if (!override) return action;
    return override.robot !== action.robot || override.locked !== action.robot_locked
      ? { ...action, robot: override.robot, robot_locked: override.locked }
      : action;
  });
  const byId = new Map(updated.map((action) => [action.id, action]));
  const ordered: AugmentedAction[] = [];
  const seen = new Set<string>();
  for (const task of plan.tasks) {
    if (!task.task || seen.has(task.task)) continue;
    const action = byId.get(task.task);
    if (!action) continue;
    ordered.push(action);
    seen.add(task.task);
  }
  for (const action of updated) {
    if (!seen.has(action.id)) ordered.push(action);
  }
  return ordered;
}
