import { createRef } from "react";
import { act, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RefComposer, type ComposerHandle } from "./RefComposer";

describe("RefComposer", () => {
  it("colors plan-task tokens with their robot's Gantt palette", () => {
    const ref = createRef<ComposerHandle>();
    const { container } = render(<RefComposer ref={ref} onSend={vi.fn()} />);

    act(() => {
      ref.current?.insertToken(
        "task-1",
        "robot1 · open fridge",
        "plan_task",
        "robot1",
      );
    });

    expect(container.querySelector(".ref-token.is-plan_task")).toHaveStyle({
      "--robot-bar-background": "rgba(219, 74, 148, 0.28)",
      "--robot-bar-border": "rgba(244, 114, 182, 0.55)",
    });
  });

  it("updates an existing plan-task token when its bar changes lanes", () => {
    const ref = createRef<ComposerHandle>();
    const { container } = render(<RefComposer ref={ref} onSend={vi.fn()} />);

    act(() => {
      ref.current?.insertToken(
        "task-1",
        "robot0 · apple_1 → fridge",
        "plan_task",
        "robot0",
      );
      ref.current?.updatePlanTaskToken(
        "task-1",
        "robot1 · apple_1 → fridge",
        "robot1",
      );
    });

    const token = container.querySelector(".ref-token.is-plan_task");
    expect(token).toHaveTextContent("robot1 · apple_1 → fridge");
    expect(token).toHaveAttribute("data-robot", "robot1");
    expect(token).toHaveStyle({
      "--robot-bar-background": "rgba(219, 74, 148, 0.28)",
      "--robot-bar-border": "rgba(244, 114, 182, 0.55)",
    });
  });
});
