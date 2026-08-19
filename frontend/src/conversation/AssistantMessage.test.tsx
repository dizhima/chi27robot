import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AssistantMessage } from "./AssistantMessage";
import type { ConversationMessage } from "./conversationTypes";

// Phase 4 Part E / D8: a collapsible "See details" toggle renders above the
// final answer once a done/error assistant message carries `details` text
// (the resolve summary's numbered adjustments + compound-turn time -- D8 moved
// this here from the bubble). The live per-attempt trail (`activities[]`)
// is a SEPARATE thing that only matters while the turn is working (see
// AssistantMessage's docstring) -- it is deliberately not rendered here at
// all, even when present on a done message, which is why these fixtures
// carry `activities` too: proving the toggle is driven by `details`, not by
// the trail array happening to be non-empty. ScenePage itself pulls in the
// full app (Mujoco canvas, WASM, etc.) which makes a full-page DOM harness
// impractical here, so per the phase4 spec (section 8, frontend test 4) this
// exercises the extracted AssistantMessage component in isolation instead.
const doneMessage: ConversationMessage = {
  id: "a1",
  role: "assistant",
  status: "done",
  intent: "author",
  content: "Added the mug to sink task.",
  activities: [
    { seq: 2, stage: "intent_selected", text: "Understanding your plan change." },
    { seq: 3, stage: "augment", text: "Filling in shared-facility steps." },
    { seq: 4, stage: "decomposition", text: "Draft ready for review." },
  ],
  details:
    "Resolved all conflicts with 2 adjustments:\n" +
    "1. Sent robot0 to its parking spot.\n" +
    "2. Made robot1 wait for robot0.\n" +
    "Turn time used: 12.3s.",
};

describe("AssistantMessage details toggle", () => {
  it("hides the details by default and shows only the toggle", () => {
    render(<AssistantMessage message={doneMessage} />);

    expect(screen.getByText("Added the mug to sink task.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "See details ▸" })).toBeInTheDocument();
    expect(screen.queryByText(/Sent robot0 to its parking spot/)).not.toBeInTheDocument();
  });

  it("expands the resolve summary lines on toggle, and collapses again on a second click", async () => {
    render(<AssistantMessage message={doneMessage} />);

    await userEvent.click(screen.getByRole("button", { name: "See details ▸" }));

    const items = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(items).toEqual([
      "Resolved all conflicts with 2 adjustments:",
      "1. Sent robot0 to its parking spot.",
      "2. Made robot1 wait for robot0.",
      "Turn time used: 12.3s.",
    ]);
    expect(screen.getByRole("button", { name: "Hide details ▾" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Hide details ▾" }));
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  });

  it("never renders the live attempt trail post-turn, even though activities[] is populated", () => {
    render(<AssistantMessage message={doneMessage} />);
    for (const activity of doneMessage.activities ?? []) {
      expect(screen.queryByText(activity.text)).not.toBeInTheDocument();
    }
  });

  it("does not render a toggle while the turn is still working", () => {
    render(
      <AssistantMessage
        message={{ ...doneMessage, status: "working", content: "Authoring…" }}
      />,
    );

    expect(screen.queryByRole("button", { name: /see details/i })).not.toBeInTheDocument();
  });

  it("does not render a toggle when there are no details (fast path)", () => {
    render(<AssistantMessage message={{ ...doneMessage, details: undefined }} />);

    expect(screen.queryByRole("button", { name: /see details/i })).not.toBeInTheDocument();
  });

  it("does not render a toggle for an empty details string", () => {
    render(<AssistantMessage message={{ ...doneMessage, details: "" }} />);

    expect(screen.queryByRole("button", { name: /see details/i })).not.toBeInTheDocument();
  });
});
