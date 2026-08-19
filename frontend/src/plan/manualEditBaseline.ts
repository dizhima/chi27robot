import type { ConversationMessage } from "../conversation/conversationTypes";
import type { AugmentedAction } from "./authorPlan";
import type { AuthoredPlan, CompileResponse } from "./planTypes";
import type { VersionHistory } from "./versionHistory";

/** Full checkpoint captured immediately before the first manual edit. */
export type ManualEditBaseline = {
  plan: AuthoredPlan;
  compile: CompileResponse | null;
  semanticActions: AugmentedAction[];
  messages: ConversationMessage[];
  lastResolverReport: unknown | null;
  versionHistory: VersionHistory;
};

export function captureManualEditBaseline(
  baseline: ManualEditBaseline,
): ManualEditBaseline {
  return structuredClone(baseline);
}

/** Return an isolated restore payload so history snapshots remain immutable. */
export function restoreManualEditBaseline(
  baseline: ManualEditBaseline,
): ManualEditBaseline {
  return structuredClone(baseline);
}
