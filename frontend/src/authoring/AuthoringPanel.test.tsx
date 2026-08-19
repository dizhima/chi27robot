import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AuthoringPanel } from "./AuthoringPanel";
import { createMockAuthoringState } from "./mockData";
import { authoringReducer } from "./reducer";

describe("AuthoringPanel", () => {
  it("starts with a grounding chat and live semantic task list", async () => {
    const dispatch = vi.fn();
    render(
      <AuthoringPanel
        state={createMockAuthoringState()}
        dispatch={dispatch}
        selectedBody={null}
      />,
    );

    expect(screen.getByRole("heading", { name: "Describe the task" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Tasks" })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Task message"), "把杯子放水池");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(dispatch).toHaveBeenCalledWith({
      type: "send_message",
      text: "把杯子放水池",
    });
  });

  it("disables sending a blank message", () => {
    const dispatch = vi.fn();
    const state = createMockAuthoringState();
    render(
      <AuthoringPanel state={state} dispatch={dispatch} selectedBody={null} />,
    );
    fireEvent.change(screen.getByLabelText("Task message"), {
      target: { value: "   " },
    });
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("renders two strategies and four individual semantic tasks", () => {
    const state = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
    render(<AuthoringPanel state={state} dispatch={vi.fn()} selectedBody={null} />);

    expect(screen.getByRole("button", { name: /By destination/i })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: /By island region/i })).toBeInTheDocument();
    const taskRows = screen.getAllByTestId("semantic-task");
    expect(taskRows).toHaveLength(4);
    for (const taskRow of taskRows) {
      expect(taskRow).not.toHaveTextContent(/\b(navigate|pick|place|open refrigerator)\b/i);
    }
  });

  it("dispatches strategy, assignment, deletion, and confirmation actions", async () => {
    const state = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
    const dispatch = vi.fn();
    render(<AuthoringPanel state={state} dispatch={dispatch} selectedBody={null} />);

    await userEvent.click(screen.getByRole("button", { name: /By island region/i }));
    expect(dispatch).toHaveBeenCalledWith({
      type: "select_strategy",
      strategyId: "by_region",
    });

    await userEvent.selectOptions(screen.getByLabelText("Assign mug_1"), "robot_b");
    expect(dispatch).toHaveBeenCalledWith({
      type: "reassign_task",
      taskId: "task-mug-1",
      robotId: "robot_b",
    });

    await userEvent.click(screen.getByRole("button", { name: "Delete mug_1" }));
    expect(dispatch).toHaveBeenCalledWith({ type: "delete_task", taskId: "task-mug-1" });

    await userEvent.click(screen.getByRole("button", { name: "Confirm semantic plan" }));
    expect(dispatch).toHaveBeenCalledWith({ type: "confirm_plan" });
  });

  it("offers undo for a deleted task", async () => {
    const planned = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
    const state = authoringReducer(planned, { type: "delete_task", taskId: "task-mug-1" });
    const dispatch = vi.fn();
    render(<AuthoringPanel state={state} dispatch={dispatch} selectedBody={null} />);

    expect(screen.queryByText("mug_1")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(dispatch).toHaveBeenCalledWith({ type: "undo_delete" });
  });

  it("stops at semantic plan confirmation", () => {
    const planned = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
    const state = authoringReducer(planned, { type: "confirm_plan" });
    render(<AuthoringPanel state={state} dispatch={vi.fn()} selectedBody={null} />);

    expect(screen.getByText("Semantic task plan confirmed")).toBeInTheDocument();
    expect(screen.getByText("Execution planning is not implemented yet")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /execute/i })).not.toBeInTheDocument();
  });
});
