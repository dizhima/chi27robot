import type { ConversationMessage } from "../conversation/conversationTypes";
import type { AugmentedAction } from "./authorPlan";
import type { AuthoredPlan, CompileResponse } from "./planTypes";
import type { PlanEditDelta } from "./planEdits";

export type PlanVersionSource = "chat" | "sync";

export type PlanVersionSnapshot = {
  plan: AuthoredPlan;
  compile: CompileResponse;
  semanticActions: AugmentedAction[];
  edits: PlanEditDelta[];
  messages: ConversationMessage[];
  lastResolverReport: unknown | null;
};

export type PlanVersion = {
  id: string;
  parentId: string | null;
  createdAt: number;
  source: PlanVersionSource;
  title: string;
  snapshot: PlanVersionSnapshot;
};

export type VersionHistory = {
  versions: PlanVersion[];
  currentId: string | null;
};

export const EMPTY_VERSION_HISTORY: VersionHistory = {
  versions: [],
  currentId: null,
};

function cloneSnapshot(snapshot: PlanVersionSnapshot): PlanVersionSnapshot {
  return structuredClone(snapshot);
}

/**
 * Append a committed state below the version that is currently checked out.
 * Existing descendants are retained, so committing after checking out an old
 * version creates a branch instead of deleting the abandoned future.
 */
export function appendPlanVersion(
  history: VersionHistory,
  version: Omit<PlanVersion, "parentId" | "snapshot"> & { snapshot: PlanVersionSnapshot },
): VersionHistory {
  const node: PlanVersion = {
    ...version,
    parentId: history.currentId,
    snapshot: cloneSnapshot(version.snapshot),
  };
  return {
    versions: [...history.versions, node],
    currentId: node.id,
  };
}

/** Return an isolated snapshot so restored state cannot mutate history. */
export function snapshotForVersion(history: VersionHistory, id: string): PlanVersionSnapshot | null {
  const version = history.versions.find((candidate) => candidate.id === id);
  return version ? cloneSnapshot(version.snapshot) : null;
}

export function checkoutPlanVersion(history: VersionHistory, id: string): VersionHistory {
  return history.versions.some((version) => version.id === id)
    ? { ...history, currentId: id }
    : history;
}

export function versionDisplayLabel(history: VersionHistory, version: PlanVersion): string {
  const index = history.versions.findIndex((candidate) => candidate.id === version.id);
  return `V${index + 1}`;
}
