import { describe, expect, it } from "vitest";
import { createMockAuthoringState, destinationTasks } from "./mockData";
import { authoringReducer } from "./reducer";

describe("authoringReducer", () => {
  it("captures full messages and current UI tasks in each send_message request", () => {
    const initial = {
      ...createMockAuthoringState(),
      tasks: [
        {
          id: "t0",
          type: "object_goal" as const,
          object: { bodyId: 1, name: "mug_1" },
          relation: "on" as const,
          target: { bodyId: 2, name: "sink" },
          assignee: "robot_b" as const,
        },
      ],
      messages: [{ role: "assistant" as const, content: "已有一个任务。" }],
    };
    const next = authoringReducer(initial, {
      type: "send_message",
      text: "苹果也放冰箱",
    });
    expect(next.pendingGround).toEqual({
      requestId: 1,
      messages: [
        { role: "assistant", content: "已有一个任务。" },
        { role: "user", content: "苹果也放冰箱" },
      ],
      currentTasks: [
        { id: "t0", action: "move", object: "mug_1", dest: "sink" },
      ],
    });
  });

  it("full-replaces tasks while preserving an existing manual assignment", () => {
    const sent = authoringReducer(
      {
        ...createMockAuthoringState(),
        tasks: [
          {
            id: "t0",
            type: "object_goal",
            object: { bodyId: 1, name: "mug_1" },
            relation: "on",
            target: { bodyId: 2, name: "sink" },
            assignee: "robot_b",
          },
        ],
      },
      { type: "send_message", text: "苹果也放冰箱" },
    );
    const next = authoringReducer(sent, {
      type: "ground_succeeded",
      requestId: 1,
      tasks: [
        { id: "t0", action: "move", object: "mug_1", dest: "sink" },
        { id: "t1", action: "move", object: "apple_2", dest: "fridge" },
      ],
      message: "已新增苹果任务。",
      reason: null,
    });
    expect(next.tasks.map((task) => task.id)).toEqual(["t0", "t1"]);
    expect(next.tasks[0].assignee).toBe("robot_b");
    expect(next.messages.at(-1)).toEqual({
      role: "assistant",
      content: "已新增苹果任务。",
    });
  });

  it("sends a manually deleted list as the next authoritative current_tasks", () => {
    const state = {
      ...createMockAuthoringState(),
      tasks: destinationTasks.slice(0, 2),
    };
    const deleted = authoringReducer(state, {
      type: "delete_task",
      taskId: destinationTasks[0].id,
    });
    const sent = authoringReducer(deleted, {
      type: "send_message",
      text: "再加苹果",
    });
    expect(sent.pendingGround?.currentTasks.map((task) => task.id)).toEqual([
      destinationTasks[1].id,
    ]);
  });

  it("confirms intent before exposing four tasks", () => {
    const next = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
    expect(next.phase).toBe("plan_review");
    expect(next.tasks).toHaveLength(4);
    expect(next.selectedStrategyId).toBe("by_destination");
  });

  it("returns Edit to intent review instead of an extra clarifying screen", () => {
    const planned = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
    const next = authoringReducer(planned, { type: "edit_intent" });
    expect(next.phase).toBe("intent_review");
    expect(next.tasks).toHaveLength(0);
    expect(next.selectedStrategyId).toBeNull();
  });

  it("updates mock intent text and preserves it when returning from plan review", () => {
    const edited = authoringReducer(createMockAuthoringState(), {
      type: "update_intent",
      value: "Keep mug 3 on the island.",
    });
    expect(edited.refinedIntent).toBe("Keep mug 3 on the island.");

    const planned = authoringReducer(edited, { type: "confirm_intent" });
    const reopened = authoringReducer(planned, { type: "edit_intent" });
    expect(reopened.refinedIntent).toBe("Keep mug 3 on the island.");
  });

  it("switches strategy without changing semantic goals or task ids", () => {
    const state = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
    const before = state.tasks.map(({ id, object, relation, target }) => ({ id, object, relation, target }));
    const next = authoringReducer(state, { type: "select_strategy", strategyId: "by_region" });
    expect(next.selectedStrategyId).toBe("by_region");
    expect(next.tasks.map(({ id, object, relation, target }) => ({ id, object, relation, target }))).toEqual(before);
    expect(next.tasks.map((task) => task.assignee)).not.toEqual(state.tasks.map((task) => task.assignee));
  });

  it("reassigns one task", () => {
    const state = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
    const next = authoringReducer(state, {
      type: "reassign_task",
      taskId: "task-fruit-a",
      robotId: "robot_a",
    });
    expect(next.tasks.find((task) => task.id === "task-fruit-a")?.assignee).toBe("robot_a");
  });

  it("deletes and restores the same stable task", () => {
    const state = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
    const deleted = authoringReducer(state, { type: "delete_task", taskId: "task-mug-1" });
    expect(deleted.tasks.some((task) => task.id === "task-mug-1")).toBe(false);
    const restored = authoringReducer(deleted, { type: "undo_delete" });
    expect(restored.tasks.map((task) => task.id)).toEqual(state.tasks.map((task) => task.id));
  });

  it("stages and applies a target only in explicit target-edit mode", () => {
    const state = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
    const ignored = authoringReducer(state, {
      type: "stage_target",
      target: { bodyId: 77, name: "counter_right" },
    });
    expect(ignored).toBe(state);

    const selecting = authoringReducer(state, { type: "begin_target_edit", taskId: "task-mug-1" });
    const staged = authoringReducer(selecting, {
      type: "stage_target",
      target: { bodyId: 77, name: "counter_right" },
    });
    const applied = authoringReducer(staged, { type: "apply_target" });
    expect(applied.tasks.find((task) => task.id === "task-mug-1")?.target).toEqual({
      bodyId: 77,
      name: "counter_right",
    });
    expect(applied.editingTargetTaskId).toBeNull();
  });

  it("confirms the semantic plan without creating execution steps", () => {
    const state = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
    const next = authoringReducer(state, { type: "confirm_plan" });
    expect(next.phase).toBe("plan_confirmed");
    expect(next.tasks.every((task) => task.type === "object_goal")).toBe(true);
  });
});
