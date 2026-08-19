import { describe, expect, it } from "vitest";
import { turnResultBubbleContent, turnResultDetails } from "./turnResultPresentation";
import type { TurnOutcome } from "./conversationTypes";

const outcome = (over: Partial<Extract<TurnOutcome, { kind: "turn_result" }>> = {}) => ({
  kind: "turn_result" as const,
  plan: null,
  message: "Resolved all conflicts with 4 adjustments.",
  ...over,
});

describe("turn result presentation", () => {
  it("shows the applied-edit template while keeping resolver output in details", () => {
    const result = outcome({
      editMessage: "Applied your edit:\n1. Reordered orange_1 → fridge after apple_2 → fridge on robot0.",
      resolveSummary: "Resolved all conflicts with 4 adjustments:\n1. Re-routed robot0.",
    });

    expect(turnResultBubbleContent(result)).toBe(result.editMessage);
    expect(turnResultBubbleContent(result)).not.toContain("Resolved all conflicts");
    expect(turnResultDetails(result)).toBe(result.resolveSummary);
  });

  it("keeps unresolved warnings visible beside the edit summary", () => {
    const result = outcome({
      editMessage: "Applied your edit:\n1. Updated the route for mug_1 → sink.",
      resolveWarning: "1 conflict could not be fixed automatically.",
    });
    expect(turnResultBubbleContent(result)).toContain(result.resolveWarning);
  });

  it("uses a compact verification message for a no-edit Sync", () => {
    const result = outcome({ resolveSummary: "Resolved all conflicts with 2 adjustments." });
    expect(turnResultBubbleContent(result)).toBe("Re-checked the plan.");
    expect(turnResultDetails(result)).toBe(result.resolveSummary);
  });
});
