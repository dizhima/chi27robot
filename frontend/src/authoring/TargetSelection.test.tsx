import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { AuthoringPanel } from "./AuthoringPanel";
import { createMockAuthoringState } from "./mockData";
import { authoringReducer } from "./reducer";

it("ignores scene selection outside target-edit mode", () => {
  const state = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
  const dispatch = vi.fn();
  render(
    <AuthoringPanel
      state={state}
      dispatch={dispatch}
      selectedBody={{ bodyId: 77, name: "counter_right" }}
    />,
  );
  expect(dispatch).not.toHaveBeenCalledWith({
    type: "stage_target",
    target: { bodyId: 77, name: "counter_right" },
  });
});

it("stages a scene selection and requires Apply in target-edit mode", async () => {
  const planned = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
  const selecting = authoringReducer(planned, {
    type: "begin_target_edit",
    taskId: "task-mug-1",
  });
  const dispatch = vi.fn();
  const { rerender } = render(
    <AuthoringPanel state={selecting} dispatch={dispatch} selectedBody={null} />,
  );

  expect(screen.getByText("Select a target in the scene")).toBeInTheDocument();

  rerender(
    <AuthoringPanel
      state={selecting}
      dispatch={dispatch}
      selectedBody={{ bodyId: 77, name: "counter_right" }}
    />,
  );
  expect(dispatch).toHaveBeenCalledWith({
    type: "stage_target",
    target: { bodyId: 77, name: "counter_right" },
  });

  const staged = authoringReducer(selecting, {
    type: "stage_target",
    target: { bodyId: 77, name: "counter_right" },
  });
  rerender(<AuthoringPanel state={staged} dispatch={dispatch} selectedBody={null} />);
  expect(screen.getByText(/counter_right/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Apply target" }));
  expect(dispatch).toHaveBeenCalledWith({ type: "apply_target" });
});

it("cancels target-edit mode", async () => {
  const planned = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
  const selecting = authoringReducer(planned, {
    type: "begin_target_edit",
    taskId: "task-mug-1",
  });
  const dispatch = vi.fn();
  render(<AuthoringPanel state={selecting} dispatch={dispatch} selectedBody={null} />);
  await userEvent.click(screen.getByRole("button", { name: "Cancel target edit" }));
  expect(dispatch).toHaveBeenCalledWith({ type: "cancel_target_edit" });
});
