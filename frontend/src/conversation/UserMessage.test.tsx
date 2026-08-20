import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ConversationMessage } from "./conversationTypes";
import { UserMessage } from "./UserMessage";

describe("UserMessage", () => {
  it("preserves scene and plan references as static composer-style chips", () => {
    const message: ConversationMessage = {
      id: "user-1",
      role: "user",
      content: "move banana_2 in robot0 · banana_2 → fridge (plan task t1)",
      displayParts: [
        { type: "text", value: "move " },
        { type: "ref", label: "banana_2", kind: "object" },
        { type: "ref", label: "upper_cabinet", kind: "facility" },
        { type: "ref", label: "robot0", kind: "robot" },
        { type: "text", value: " in " },
        { type: "ref", label: "robot0 · banana_2 → fridge", kind: "plan_task" },
      ],
    };

    const { container } = render(<UserMessage message={message} />);
    expect(screen.getByText("banana_2")).toHaveClass(
      "ref-token",
      "is-object",
      "message-ref-token",
    );
    expect(screen.getByText(/◇ upper_cabinet/)).toHaveClass("is-facility", "message-ref-token");
    expect(screen.getByText(/● robot0/)).toHaveClass("is-robot", "message-ref-token");
    expect(screen.getByText(/robot0 · banana_2 → fridge/)).toHaveClass(
      "is-plan_task",
      "message-ref-token",
    );
    expect(container.querySelector("a")).not.toBeInTheDocument();
    expect(container.querySelector("button")).not.toBeInTheDocument();
    expect(container).not.toHaveTextContent("plan task t1");
  });

  it("renders position references and falls back to content for old messages", () => {
    const rich: ConversationMessage = {
      id: "user-2",
      role: "user",
      content: "use counter_left (pin p1)",
      displayParts: [{ type: "ref", label: "counter_left", kind: "position" }],
    };
    const { rerender } = render(<UserMessage message={rich} />);
    expect(screen.getByText(/📍 counter_left/)).toHaveClass("is-position");

    rerender(<UserMessage message={{ id: "old", role: "user", content: "legacy text" }} />);
    expect(screen.getByText("legacy text")).toBeInTheDocument();
  });
});
