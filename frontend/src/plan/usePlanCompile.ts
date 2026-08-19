/**
 * React bridge from a plan to its compiled Gantt data. Whenever `plan` changes
 * it debounces, cancels any in-flight compile, and re-runs compileAndLoad (POST
 * /compile_plan + load tracks). This is the ONE channel plan → schedule, used
 * for the first plan and every later edit alike; it is UI-agnostic (returns
 * data, renders nothing).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { ScheduledItem } from "../SchedulePlayer";
import { compileAndLoad, loadScheduleTracks } from "./compilePlan";
import type {
  AuthoredPlan,
  AuthoredStep,
  CompileResponse,
  Conflict,
  ScheduleEntry,
} from "./planTypes";

export type PlanCompileStatus = "idle" | "compiling" | "ready" | "error";

export type PlanCompileResult = {
  /** Loaded tracks for SchedulePlayer (kinematic playback). */
  items: ScheduledItem[];
  /** Raw schedule entries (carry step id/after/facility) — the Gantt's source. */
  schedule: ScheduleEntry[];
  warnings: string[];
  /** Structured conflicts; `steps` drive exact per-step bar tinting. */
  conflicts: Conflict[];
  completed: Record<string, AuthoredStep[]>;
  status: PlanCompileStatus;
  error: string | null;
  /** Bumped on each successful compile — key SchedulePlayer off it to remount / reset the playhead. */
  epoch: number;
  /** The exact plan object these results were compiled from (null before first success). */
  compiledPlan: AuthoredPlan | null;
  /** Valid only for compiledPlan; plan changes clear it until the next compile lands. */
  compileId: string | null;
};

export type PlanCompileController = PlanCompileResult & {
  /** Adopt a Resolver-verified compile without POSTing /compile_plan again. */
  adoptCompileResult: (plan: AuthoredPlan, response: CompileResponse) => Promise<void>;
};

const EMPTY: PlanCompileResult = {
  items: [],
  schedule: [],
  warnings: [],
  conflicts: [],
  completed: {},
  status: "idle",
  error: null,
  epoch: 0,
  compiledPlan: null,
  compileId: null,
};

export function usePlanCompile(
  plan: AuthoredPlan | null,
  opts: { debounceMs?: number; serviceUrl?: string } = {},
): PlanCompileController {
  const { debounceMs = 250, serviceUrl } = opts;
  const [state, setState] = useState<PlanCompileResult>(EMPTY);
  const epochRef = useRef(0);
  const adoptedPlanRef = useRef<AuthoredPlan | null>(null);

  const adoptCompileResult = useCallback(async (
    adoptedPlan: AuthoredPlan,
    response: CompileResponse,
  ) => {
    adoptedPlanRef.current = adoptedPlan;
    setState((prev) => ({ ...prev, status: "compiling", error: null }));
    try {
      const items = await loadScheduleTracks(response.schedule);
      epochRef.current += 1;
      setState({
        items,
        schedule: response.schedule,
        warnings: response.warnings,
        conflicts: response.conflicts ?? [],
        completed: response.completed,
        status: "ready",
        error: null,
        epoch: epochRef.current,
        compiledPlan: adoptedPlan,
        // Resolver verification compiles deliberately are not snapshot-cached.
        compileId: null,
      });
    } catch (err) {
      adoptedPlanRef.current = null;
      setState((prev) => ({
        ...prev,
        status: "error",
        error: err instanceof Error ? err.message : String(err),
      }));
      throw err;
    }
  }, []);

  useEffect(() => {
    if (!plan) {
      setState((prev) =>
        prev.status === "idle" && prev.items.length === 0
          ? prev
          : { ...EMPTY, epoch: epochRef.current },
      );
      return;
    }

    if (adoptedPlanRef.current === plan) {
      adoptedPlanRef.current = null;
      return;
    }

    const controller = new AbortController();
    let cancelled = false;

    // A changed plan must never retain a snapshot handle from the preceding
    // compile, even during the debounce/compiling interval.
    setState((prev) => prev.compileId === null ? prev : { ...prev, compileId: null });

    const timer = setTimeout(() => {
      setState((prev) => ({ ...prev, status: "compiling", error: null }));
      compileAndLoad(plan, { serviceUrl, signal: controller.signal })
        .then(({ items, warnings, response }) => {
          if (cancelled) return;
          epochRef.current += 1;
          setState({
            items,
            schedule: response.schedule,
            warnings,
            conflicts: response.conflicts ?? [],
            completed: response.completed,
            status: "ready",
            error: null,
            epoch: epochRef.current,
            compiledPlan: plan,
            compileId: response.compile_id ?? null,
          });
        })
        .catch((err) => {
          if (cancelled || controller.signal.aborted) return;
          setState((prev) => ({
            ...prev,
            status: "error",
            error: err instanceof Error ? err.message : String(err),
          }));
        });
    }, debounceMs);

    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [plan, debounceMs, serviceUrl]);

  return { ...state, adoptCompileResult };
}
