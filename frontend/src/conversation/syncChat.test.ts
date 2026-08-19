import { describe, expect, it } from "vitest";
import {
  APPLY_EDIT_CHAT_TEXT,
  RECHECK_PLAN_CHAT_TEXT,
  applySyncProgress,
  createSyncChatMessages,
} from "./syncChat";

describe("Sync chat lifecycle", () => {
  it("adds the apply-edit user bubble and a working assistant bubble", () => {
    const result = createSyncChatMessages([], { userId: "u1", assistantId: "a1" }, true);

    expect(result.requestMessages).toEqual([
      { id: "u1", role: "user", content: APPLY_EDIT_CHAT_TEXT },
    ]);
    expect(result.visibleMessages[1]).toMatchObject({
      id: "a1",
      role: "assistant",
      status: "working",
      content: "Applying your edits…",
    });
  });

  it("uses a re-check label when Sync has no pending edits", () => {
    const result = createSyncChatMessages([], { userId: "u1", assistantId: "a1" }, false);
    expect(result.requestMessages).toEqual([
      { id: "u1", role: "user", content: RECHECK_PLAN_CHAT_TEXT },
    ]);
    expect(result.visibleMessages[1].content).toBe("Re-checking conflicts…");
  });

  it("streams progress into the same assistant bubble", () => {
    const started = createSyncChatMessages([], { userId: "u1", assistantId: "a1" }, true);
    const updated = applySyncProgress(started.visibleMessages, "a1", {
      type: "progress",
      seq: 7,
      stage: "compiling",
      text: "Compiling the plan.",
    });

    expect(updated[1]).toMatchObject({
      id: "a1",
      content: "Compiling the plan.",
      activities: [{ seq: 7, stage: "compiling", text: "Compiling the plan." }],
    });
  });
});
