import { describe, expect, it } from "vitest";
import type { ConversationMessage } from "../conversation/conversationTypes";
import type { AuthoredPlan, CompileResponse } from "./planTypes";
import {
  EMPTY_VERSION_HISTORY,
  appendPlanVersion,
  checkoutPlanVersion,
  snapshotForVersion,
  versionDisplayLabel,
  type PlanVersionSnapshot,
} from "./versionHistory";

const plan = (label: string): AuthoredPlan => ({
  tasks: [{ task: label, robot: "robot0", steps: [{ id: `${label}-step`, op: "wait", duration: 1 }] }],
});

const compile = (label: string): CompileResponse => ({
  schedule: [],
  warnings: [],
  conflicts: [],
  completed: { [label]: [] },
});

const snapshot = (label: string, messages: ConversationMessage[] = []): PlanVersionSnapshot => ({
  plan: plan(label),
  compile: compile(label),
  semanticActions: [{ id: label, op: "move", robot: "robot0", object: label, dest: "sink" }],
  edits: [],
  messages,
  lastResolverReport: { label },
});

const append = (history: typeof EMPTY_VERSION_HISTORY, id: string, state = snapshot(id)) =>
  appendPlanVersion(history, {
    id,
    createdAt: 1,
    source: "chat",
    title: id,
    snapshot: state,
  });

describe("version history", () => {
  it("keeps the abandoned future when a new commit branches from an old version", () => {
    let history = append(EMPTY_VERSION_HISTORY, "v1");
    history = append(history, "v2");
    history = append(history, "v3");
    history = checkoutPlanVersion(history, "v1");
    history = append(history, "v4");

    expect(history.versions.map((version) => version.id)).toEqual(["v1", "v2", "v3", "v4"]);
    expect(history.versions.find((version) => version.id === "v4")?.parentId).toBe("v1");
    expect(history.currentId).toBe("v4");
    expect(versionDisplayLabel(history, history.versions[3])).toBe("V4");
  });

  it("restores the selected plan and only that version's conversation context", () => {
    const oldMessages: ConversationMessage[] = [
      { id: "u1", role: "user", content: "make version one" },
      { id: "a1", role: "assistant", content: "done", status: "done", source: "authoring" },
    ];
    let history = append(EMPTY_VERSION_HISTORY, "v1", snapshot("v1", oldMessages));
    history = append(history, "v2", snapshot("v2", [...oldMessages, { id: "u2", role: "user", content: "future edit" }]));

    const restored = snapshotForVersion(history, "v1");
    expect(restored?.plan).toEqual(plan("v1"));
    expect(restored?.semanticActions[0].id).toBe("v1");
    expect(restored?.messages.map((message) => message.id)).toEqual(["u1", "a1"]);
  });

  it("clones stored and restored snapshots", () => {
    const original = snapshot("v1");
    const history = append(EMPTY_VERSION_HISTORY, "v1", original);
    original.plan.tasks[0].task = "mutated outside";

    const firstRestore = snapshotForVersion(history, "v1")!;
    firstRestore.plan.tasks[0].task = "mutated restore";
    const secondRestore = snapshotForVersion(history, "v1")!;

    expect(secondRestore.plan.tasks[0].task).toBe("v1");
  });
});
