/** Stateless client for the bounded post-compile conflict resolver. */
import { orchestratorUrl } from "../config";
import type { AuthoredPlan, AuthoredStep, CompileResponse } from "./planTypes";

export type ResolutionReport = {
  rounds_used: number;
  round_cap: number;
  converged: boolean;
  applied: unknown[];
  rejected: unknown[];
  deferred: unknown[];
  unresolved: unknown[];
  escalated_to_human: unknown[];
};

export type ResolveConflictsResponse = {
  plan: AuthoredPlan;
  report: ResolutionReport;
  message: string;
  initial_snapshot_reused?: boolean;
  /** Present for V2: the Resolver's last accepted verification compile. */
  compile?: CompileResponse;
};

export async function requestConflictResolution(
  completedPlan: Record<string, AuthoredStep[]>,
  compileId: string | null,
  opts: { serviceUrl?: string; signal?: AbortSignal } = {},
): Promise<ResolveConflictsResponse> {
  const base = opts.serviceUrl ?? orchestratorUrl;
  let response: Response;
  try {
    response = await fetch(`${base}/resolve_conflicts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan: completedPlan, compile_id: compileId }),
      signal: opts.signal,
    });
  } catch (err) {
    throw new Error(
      `resolve_conflicts request failed (is the orchestrator running at ${base}?): ${
        err instanceof Error ? err.message : String(err)
      }`,
    );
  }
  const payload = (await response.json().catch(() => ({}))) as
    | ResolveConflictsResponse
    | { error?: string };
  if (!response.ok || "error" in payload) {
    const detail = "error" in payload && payload.error ? payload.error : response.status;
    throw new Error(`resolve_conflicts failed: ${detail}`);
  }
  return payload as ResolveConflictsResponse;
}
