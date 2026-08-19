/**
 * Frontend mirror of the backend schedule() fixpoint: derive each step's start
 * as max(previous step on same robot's end, all `after` deps' ends). Lets the
 * Gantt re-layout instantly after a draft edit (drag/reassign) without a
 * backend round-trip; the real compile confirms on demand.
 *
 * Per-step duration/label/facility are NOT recomputed — they don't change on an
 * after/robot edit — so we reuse the last compiled values (metaById). New steps
 * estimate duration from previously compiled steps of the same op, keeping a
 * mixed compiled/new preview readable until the next real compile.
 */
import type { AuthoredPlan, AuthoredStep } from "./planTypes";
import { prettyStepLabel, type GanttBar } from "./ganttModel";

export type PreviewMeta = {
  duration: number;
  label: string;
  facility?: string | null;
  group?: string | null;
};

// Nominal per-step width used before a real compile has produced durations, so
// a structural preview lays out as even-width bars (relative timing is unknown
// until compile). Any positive constant works — the Gantt is percentage-scaled.
const NOMINAL_DURATION = 1;
const MIN_MIXED_PREVIEW_DURATION = 3;
const MAX_MIXED_PREVIEW_DURATION = 12;

type FlatStep = {
  id: string;
  op: string;
  robot: string;
  after: string[];
  group?: string | null;
  authoredDuration?: number;
  /** Fallback label from the authored step, used until compile supplies one. */
  label: string;
};

/** Compact label matching compiled Step Plan bars. */
function stepLabel(step: AuthoredStep): string {
  return prettyStepLabel({
    op: step.op,
    facility: typeof step.facility === "string" ? step.facility : null,
    repairKind: typeof step.compiler_v2_repair === "string"
      ? step.compiler_v2_repair : null,
  });
}

function flatten(plan: AuthoredPlan): FlatStep[] {
  const steps: FlatStep[] = [];
  for (const task of plan.tasks) {
    for (const step of task.steps) {
      if (!step.id) continue; // ids are assigned on load; skip anything unkeyed
      steps.push({
        id: step.id,
        op: step.op,
        robot: task.robot,
        after: step.after ?? [],
        group: typeof step.group === "string" ? step.group : task.task ?? null,
        authoredDuration:
          step.op === "wait" && typeof step.duration === "number" && step.duration > 0
            ? step.duration
            : undefined,
        label: stepLabel(step),
      });
    }
  }
  return steps;
}

function median(values: number[]): number | undefined {
  if (values.length === 0) return undefined;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

/**
 * Derive start times for a draft plan and return Gantt bars (warned=false;
 * conflicts only surface after a real compile). Steps with no compiled meta fall
 * back to a nominal width + an authored-step label, so a never-compiled plan
 * still renders a readable structural preview.
 */
export function previewBars(plan: AuthoredPlan, metaById: Map<string, PreviewMeta>): GanttBar[] {
  const steps = flatten(plan);

  // A fixed one-second fallback works when every bar is new because percentage
  // scaling makes them evenly sized. It fails badly after a compile: real tasks
  // may span 100+ seconds while newly added tasks collapse to a few pixels.
  // Learn estimates from the last compile instead, preferring the same op.
  const samplesByOp = new Map<string, number[]>();
  for (const step of steps) {
    const compiled = metaById.get(step.id)?.duration;
    if (compiled === undefined || compiled <= 0) continue;
    const samples = samplesByOp.get(step.op) ?? [];
    samples.push(compiled);
    samplesByOp.set(step.op, samples);
  }
  const allCompiledDurations = [...metaById.values()]
    .map((meta) => meta.duration)
    .filter((duration) => duration > 0);
  const globalMedian = median(allCompiledDurations);
  const mixedFallback =
    globalMedian === undefined
      ? NOMINAL_DURATION
      : Math.min(
          MAX_MIXED_PREVIEW_DURATION,
          Math.max(MIN_MIXED_PREVIEW_DURATION, globalMedian),
        );
  const estimatedDuration = (step: FlatStep) =>
    step.authoredDuration ?? median(samplesByOp.get(step.op) ?? []) ?? mixedFallback;
  const durationById = new Map(
    steps.map((step) => [
      step.id,
      metaById.get(step.id)?.duration ?? estimatedDuration(step),
    ]),
  );
  const dur = (id: string) => durationById.get(id) ?? mixedFallback;

  const byRobot = new Map<string, FlatStep[]>();
  for (const s of steps) {
    if (!byRobot.has(s.robot)) byRobot.set(s.robot, []);
    byRobot.get(s.robot)!.push(s); // encounter order = execution order
  }

  const start = new Map<string, number>(steps.map((s) => [s.id, 0]));
  // Relax to a fixpoint; steps.length + 3 passes is ample (raises nothing on
  // cycles — a bad `after` cycle just fails to converge and is caught on compile).
  for (let pass = 0; pass < steps.length + 3; pass++) {
    let changed = false;
    for (const list of byRobot.values()) {
      let prevEnd = 0;
      for (const s of list) {
        let t = prevEnd;
        for (const a of s.after) {
          t = Math.max(t, (start.get(a) ?? 0) + dur(a));
        }
        if (t !== start.get(s.id)) {
          start.set(s.id, t);
          changed = true;
        }
        prevEnd = t + dur(s.id);
      }
    }
    if (!changed) break;
  }

  return steps.map((s) => {
    const meta = metaById.get(s.id);
    return {
      key: s.id,
      robot: s.robot,
      label: meta?.label ?? s.label,
      op: s.op,
      start: start.get(s.id) ?? 0,
      duration: dur(s.id),
      group: meta?.group ?? s.group,
      facility: meta?.facility ?? null,
      warned: false,
      estimated: !meta,
    };
  });
}
