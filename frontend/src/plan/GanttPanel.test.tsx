import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { GanttPanel, timelineTicks } from "./GanttPanel";
import type { GanttBar } from "./ganttModel";

const bars: GanttBar[] = [
  {
    key: "a",
    robot: "robot0",
    label: "task a",
    start: 0,
    duration: 10,
    warned: false,
  },
];

describe("GanttPanel timeline ruler", () => {
  const rect = (left: number, top: number, width: number, height: number): DOMRect => ({
    x: left,
    y: top,
    left,
    right: left + width,
    top,
    bottom: top + height,
    width,
    height,
    toJSON: () => ({}),
  });

  it("keeps the time ruler and playhead while collapsed", () => {
    const { container } = render(
      <GanttPanel
        bars={bars}
        lanes={["robot0", "robot1"]}
        warnings={[]}
        time={4}
        playing={false}
        collapsed
      />,
    );
    expect(screen.queryByText("robot0")).toBeNull();
    expect(screen.queryByText("robot1")).toBeNull();
    expect(screen.queryByText("task a")).toBeNull();
    expect(screen.getByRole("slider", { name: "Plan time" })).toBeInTheDocument();
    expect(container.querySelector(".gantt-playhead")).not.toBeNull();
  });

  it("uses scene identity colors for enlarged robot lane labels", () => {
    render(
      <GanttPanel
        bars={bars}
        lanes={["robot0", "robot1"]}
        warnings={[]}
        time={0}
        playing={false}
      />,
    );
    const robot0 = screen.getByText("robot0").closest(".gantt-lane-label");
    const robot1 = screen.getByText("robot1").closest(".gantt-lane-label");
    expect(robot0).toHaveStyle({ "--robot-color": "#2563EB" });
    expect(robot1).toHaveStyle({ "--robot-color": "#f472b6" });
    expect(robot0?.querySelector(".gantt-lane-dot")).not.toBeNull();
    expect(robot1?.querySelector(".gantt-lane-dot")).not.toBeNull();
  });

  it("creates readable ticks including both schedule endpoints", () => {
    const ticks = timelineTicks(136.4);
    expect(ticks[0]).toBe(0);
    expect(ticks.at(-1)).toBe(136.4);
    expect(ticks.length).toBeGreaterThanOrEqual(5);
    expect(ticks.length).toBeLessThanOrEqual(12);
  });

  it("seeks to the clicked position on the shared ruler", () => {
    const onSeek = vi.fn();
    render(
      <GanttPanel
        bars={bars}
        warnings={[]}
        time={0}
        playing={false}
        onSeek={onSeek}
      />,
    );
    const ruler = screen.getByRole("slider", { name: "Plan time" });
    vi.spyOn(ruler, "getBoundingClientRect").mockReturnValue({
      x: 100,
      y: 0,
      left: 100,
      right: 300,
      top: 0,
      bottom: 30,
      width: 200,
      height: 30,
      toJSON: () => ({}),
    });

    fireEvent.pointerDown(ruler, { clientX: 150 });

    expect(onSeek).toHaveBeenLastCalledWith(2.5);
  });

  it("supports keyboard seeking and blocks uncompiled previews", () => {
    const onSeek = vi.fn();
    const { rerender } = render(
      <GanttPanel
        bars={bars}
        warnings={[]}
        time={0}
        playing={false}
        onSeek={onSeek}
      />,
    );
    fireEvent.keyDown(screen.getByRole("slider"), { key: "End" });
    expect(onSeek).toHaveBeenLastCalledWith(10);

    rerender(
      <GanttPanel
        bars={[{ ...bars[0], estimated: true }]}
        warnings={[]}
        time={0}
        playing={false}
        transportDisabled
        onSeek={onSeek}
      />,
    );
    const disabled = screen.getByRole("slider");
    expect(disabled).toHaveAttribute("aria-disabled", "true");
    fireEvent.keyDown(disabled, { key: "End" });
    expect(onSeek).toHaveBeenCalledTimes(1);
  });

  it("offers 8x playback and sends the selected multiplier", () => {
    const onSetSpeed = vi.fn();
    render(
      <GanttPanel
        bars={bars}
        warnings={[]}
        time={0}
        playing={false}
        speed={1}
        onSetSpeed={onSetSpeed}
      />,
    );

    const speed = screen.getByRole("combobox", { name: "Playback speed" });
    expect(speed).toHaveTextContent("8×");
    fireEvent.change(speed, { target: { value: "8" } });
    expect(onSetSpeed).toHaveBeenCalledWith(8);
  });

  it("routes a task-bar click to plan-reference picking instead of selecting/opening its editor", () => {
    const onReferenceBar = vi.fn();
    const onSelectBar = vi.fn();
    render(
      <GanttPanel
        bars={[{ ...bars[0], group: "move_milk_fridge" }]}
        warnings={[]}
        time={0}
        playing={false}
        viewMode="task"
        onReferenceBar={onReferenceBar}
        onSelectBar={onSelectBar}
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "task a" }), { clientX: 20 });
    expect(onReferenceBar).toHaveBeenCalledWith(expect.objectContaining({ group: "move_milk_fridge" }));
    expect(onSelectBar).not.toHaveBeenCalled();
  });

  it("selects an ordinary bar on pointer-up when the gesture did not become a drag", () => {
    const onSelectBar = vi.fn();
    render(
      <GanttPanel
        bars={bars}
        warnings={[]}
        time={0}
        playing={false}
        onSelectBar={onSelectBar}
      />,
    );

    fireEvent.pointerDown(screen.getByRole("button", { name: "task a" }), {
      clientX: 20,
      clientY: 10,
    });
    expect(onSelectBar).not.toHaveBeenCalled();
    fireEvent.pointerUp(window);

    expect(onSelectBar).toHaveBeenCalledOnce();
    expect(onSelectBar).toHaveBeenCalledWith(expect.objectContaining({ key: "a" }));
  });

  it("stages and undoes removal only for removable semantic task bars", () => {
    const onToggleTaskRemoval = vi.fn();
    const taskBars = [{ ...bars[0], group: "move_milk_fridge" }];
    const { rerender } = render(
      <GanttPanel
        bars={taskBars}
        warnings={[]}
        time={0}
        playing={false}
        viewMode="task"
        removableTaskIds={new Set(["move_milk_fridge"])}
        pendingRemovalIds={new Set()}
        onToggleTaskRemoval={onToggleTaskRemoval}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Remove task a" }));
    expect(onToggleTaskRemoval).toHaveBeenCalledWith("move_milk_fridge");

    rerender(
      <GanttPanel
        bars={taskBars}
        warnings={[]}
        time={0}
        playing={false}
        viewMode="task"
        removableTaskIds={new Set(["move_milk_fridge"])}
        pendingRemovalIds={new Set(["move_milk_fridge"])}
        onToggleTaskRemoval={onToggleTaskRemoval}
      />,
    );

    expect(screen.getByRole("button", { name: "task a" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Undo removal of task a" }))
      .toBeInTheDocument();
    expect(screen.getByRole("button", { name: "task a" }).parentElement)
      .toHaveClass("is-pending-removal");
  });

  it("does not expose remove controls in step view or for compiler-only groups", () => {
    render(
      <GanttPanel
        bars={[{ ...bars[0], group: "robot0#go_to_rest" }]}
        warnings={[]}
        time={0}
        playing={false}
        viewMode="step"
        removableTaskIds={new Set(["move_milk_fridge"])}
        pendingRemovalIds={new Set()}
        onToggleTaskRemoval={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: /^Remove / })).not.toBeInTheDocument();
  });

  it("inserts a task into another robot lane in task view", () => {
    const onMoveTask = vi.fn();
    const onSelectBar = vi.fn();
    const taskBars: GanttBar[] = [
      { ...bars[0], key: "a0", group: "a", robot: "robot0", start: 0, duration: 10 },
      { ...bars[0], key: "b0", group: "b", robot: "robot1", start: 0, duration: 10, label: "task b" },
      { ...bars[0], key: "d0", group: "d", robot: "robot1", start: 20, duration: 10, label: "task d" },
    ];
    const { container } = render(
      <GanttPanel
        bars={taskBars}
        lanes={["robot0", "robot1"]}
        warnings={[]}
        time={0}
        playing={false}
        viewMode="task"
        draggableTaskIds={new Set(["a", "b", "d"])}
        onMoveTask={onMoveTask}
        onSelectBar={onSelectBar}
      />,
    );
    vi.spyOn(container.querySelector(".gantt-tracks")!, "getBoundingClientRect")
      .mockReturnValue(rect(0, 0, 1000, 80));
    const tracks = container.querySelectorAll(".gantt-track");
    vi.spyOn(tracks[0], "getBoundingClientRect").mockReturnValue(rect(0, 0, 1000, 30));
    vi.spyOn(tracks[1], "getBoundingClientRect").mockReturnValue(rect(0, 40, 1000, 30));

    fireEvent.pointerDown(screen.getByRole("button", { name: "task a" }), { clientX: 10, clientY: 10 });
    fireEvent.pointerMove(window, { clientX: 400, clientY: 45 });
    expect(container.querySelector(".gantt-track.is-drop-target")).toBe(tracks[1]);
    expect(container.querySelector(".gantt-insert-caret")).not.toBeNull();
    fireEvent.pointerUp(window);

    expect(onMoveTask).toHaveBeenCalledWith("a", "robot1", "b");
    expect(onSelectBar).not.toHaveBeenCalled();
  });

  it("reassigns on a purely vertical drag into the other lane", () => {
    const onMoveTask = vi.fn();
    const taskBars: GanttBar[] = [
      { ...bars[0], key: "a0", group: "a", robot: "robot0", start: 0, duration: 10 },
      { ...bars[0], key: "b0", group: "b", robot: "robot1", start: 0, duration: 10, label: "task b" },
    ];
    const { container } = render(
      <GanttPanel
        bars={taskBars}
        lanes={["robot0", "robot1"]}
        warnings={[]}
        time={0}
        playing={false}
        viewMode="task"
        draggableTaskIds={new Set(["a", "b"])}
        onMoveTask={onMoveTask}
      />,
    );
    vi.spyOn(container.querySelector(".gantt-tracks")!, "getBoundingClientRect")
      .mockReturnValue(rect(0, 0, 1000, 80));
    const tracks = container.querySelectorAll(".gantt-track");
    vi.spyOn(tracks[0], "getBoundingClientRect").mockReturnValue(rect(0, 0, 1000, 30));
    vi.spyOn(tracks[1], "getBoundingClientRect").mockReturnValue(rect(0, 40, 1000, 30));

    // Same clientX throughout: only the lane changes, which is the natural
    // reassignment gesture and must not be dismissed as a click.
    fireEvent.pointerDown(screen.getByRole("button", { name: "task a" }), { clientX: 3, clientY: 10 });
    fireEvent.pointerMove(window, { clientX: 3, clientY: 45 });
    fireEvent.pointerUp(window);

    expect(onMoveTask).toHaveBeenCalledWith("a", "robot1", null);
  });

  it("reorders within the same lane without a modifier", () => {
    const onMoveTask = vi.fn();
    const onSetTaskAfter = vi.fn();
    const taskBars: GanttBar[] = [
      { ...bars[0], key: "a0", group: "a", robot: "robot0", start: 0, duration: 10 },
      { ...bars[0], key: "c0", group: "c", robot: "robot0", start: 10, duration: 10, label: "task c" },
    ];
    const { container } = render(
      <GanttPanel
        bars={taskBars}
        lanes={["robot0", "robot1"]}
        warnings={[]}
        time={0}
        playing={false}
        viewMode="task"
        draggableTaskIds={new Set(["a", "c"])}
        onMoveTask={onMoveTask}
        onSetTaskAfter={onSetTaskAfter}
      />,
    );
    vi.spyOn(container.querySelector(".gantt-tracks")!, "getBoundingClientRect")
      .mockReturnValue(rect(0, 0, 1000, 80));
    const tracks = container.querySelectorAll(".gantt-track");
    vi.spyOn(tracks[0], "getBoundingClientRect").mockReturnValue(rect(0, 0, 1000, 30));
    vi.spyOn(tracks[1], "getBoundingClientRect").mockReturnValue(rect(0, 40, 1000, 30));

    fireEvent.pointerDown(screen.getByRole("button", { name: "task a" }), { clientX: 10, clientY: 10 });
    fireEvent.pointerMove(window, { clientX: 900, clientY: 10 });
    fireEvent.pointerUp(window);

    expect(onMoveTask).toHaveBeenCalledWith("a", "robot0", "c");
    expect(onSetTaskAfter).not.toHaveBeenCalled();
  });

  it("shift-drag onto another lane's task queues a dependency in semantic ids", () => {
    const onMoveTask = vi.fn();
    const onSetTaskAfter = vi.fn();
    const taskBars: GanttBar[] = [
      { ...bars[0], key: "a0", group: "a", robot: "robot0", start: 0, duration: 10 },
      {
        ...bars[0],
        key: "b0",
        group: "b",
        robot: "robot1",
        start: 20,
        duration: 20,
        label: "task b",
        // A compiler-generated tail step: passing this on is exactly what made
        // the old dependency drop a silent no-op.
        endStepKey: "b0#reposition3",
      },
    ];
    const { container } = render(
      <GanttPanel
        bars={taskBars}
        lanes={["robot0", "robot1"]}
        warnings={[]}
        time={0}
        playing={false}
        viewMode="task"
        draggableTaskIds={new Set(["a", "b"])}
        onMoveTask={onMoveTask}
        onSetTaskAfter={onSetTaskAfter}
      />,
    );
    vi.spyOn(container.querySelector(".gantt-tracks")!, "getBoundingClientRect")
      .mockReturnValue(rect(0, 0, 1000, 80));
    const tracks = container.querySelectorAll(".gantt-track");
    vi.spyOn(tracks[0], "getBoundingClientRect").mockReturnValue(rect(0, 0, 1000, 30));
    vi.spyOn(tracks[1], "getBoundingClientRect").mockReturnValue(rect(0, 40, 1000, 30));

    fireEvent.pointerDown(screen.getByRole("button", { name: "task a" }), { clientX: 10, clientY: 10 });
    // Anywhere over the target bar's body (25s of a 40s total = 625px).
    fireEvent.pointerMove(window, { clientX: 625, clientY: 45, shiftKey: true });
    expect(container.querySelector(".gantt-after-caret")).not.toBeNull();
    expect(container.querySelector(".gantt-insert-caret")).toBeNull();
    fireEvent.pointerUp(window);

    expect(onSetTaskAfter).toHaveBeenCalledWith("a", "b");
    expect(onMoveTask).not.toHaveBeenCalled();
  });

  it("falls back to insert when shift is released mid-drag", () => {
    const onMoveTask = vi.fn();
    const onSetTaskAfter = vi.fn();
    const taskBars: GanttBar[] = [
      { ...bars[0], key: "a0", group: "a", robot: "robot0", start: 0, duration: 10 },
      { ...bars[0], key: "b0", group: "b", robot: "robot1", start: 20, duration: 20, label: "task b" },
    ];
    const { container } = render(
      <GanttPanel
        bars={taskBars}
        lanes={["robot0", "robot1"]}
        warnings={[]}
        time={0}
        playing={false}
        viewMode="task"
        draggableTaskIds={new Set(["a", "b"])}
        onMoveTask={onMoveTask}
        onSetTaskAfter={onSetTaskAfter}
      />,
    );
    vi.spyOn(container.querySelector(".gantt-tracks")!, "getBoundingClientRect")
      .mockReturnValue(rect(0, 0, 1000, 80));
    const tracks = container.querySelectorAll(".gantt-track");
    vi.spyOn(tracks[0], "getBoundingClientRect").mockReturnValue(rect(0, 0, 1000, 30));
    vi.spyOn(tracks[1], "getBoundingClientRect").mockReturnValue(rect(0, 40, 1000, 30));

    fireEvent.pointerDown(screen.getByRole("button", { name: "task a" }), { clientX: 10, clientY: 10 });
    fireEvent.pointerMove(window, { clientX: 625, clientY: 45, shiftKey: true });
    expect(container.querySelector(".gantt-after-caret")).not.toBeNull();
    // Releasing Shift must re-classify without any further pointer movement.
    fireEvent.keyUp(window, { key: "Shift" });
    expect(container.querySelector(".gantt-after-caret")).toBeNull();
    expect(container.querySelector(".gantt-insert-caret")).not.toBeNull();
    fireEvent.pointerUp(window);

    // 25s is left of task b's midpoint, so the plain drop lands ahead of it.
    expect(onMoveTask).toHaveBeenCalledWith("a", "robot1", null);
    expect(onSetTaskAfter).not.toHaveBeenCalled();
  });

  it("ignores a shift-drag that stays on the source lane", () => {
    const onMoveTask = vi.fn();
    const onSetTaskAfter = vi.fn();
    const taskBars: GanttBar[] = [
      { ...bars[0], key: "a0", group: "a", robot: "robot0", start: 0, duration: 10 },
      { ...bars[0], key: "c0", group: "c", robot: "robot0", start: 10, duration: 10, label: "task c" },
    ];
    const { container } = render(
      <GanttPanel
        bars={taskBars}
        lanes={["robot0", "robot1"]}
        warnings={[]}
        time={0}
        playing={false}
        viewMode="task"
        draggableTaskIds={new Set(["a", "c"])}
        onMoveTask={onMoveTask}
        onSetTaskAfter={onSetTaskAfter}
      />,
    );
    vi.spyOn(container.querySelector(".gantt-tracks")!, "getBoundingClientRect")
      .mockReturnValue(rect(0, 0, 1000, 80));
    const tracks = container.querySelectorAll(".gantt-track");
    vi.spyOn(tracks[0], "getBoundingClientRect").mockReturnValue(rect(0, 0, 1000, 30));
    vi.spyOn(tracks[1], "getBoundingClientRect").mockReturnValue(rect(0, 40, 1000, 30));

    fireEvent.pointerDown(screen.getByRole("button", { name: "task a" }), { clientX: 10, clientY: 10 });
    fireEvent.pointerMove(window, { clientX: 700, clientY: 10, shiftKey: true });
    fireEvent.pointerUp(window);

    expect(onSetTaskAfter).not.toHaveBeenCalled();
    expect(onMoveTask).not.toHaveBeenCalled();
  });

  it("sets after only against a different robot in step view", () => {
    const onSetAfter = vi.fn();
    const stepBars: GanttBar[] = [
      { ...bars[0], key: "a", robot: "robot0", start: 0, duration: 5 },
      { ...bars[0], key: "same", robot: "robot0", start: 10, duration: 5, label: "same robot" },
      { ...bars[0], key: "cross", robot: "robot1", start: 20, duration: 5, label: "cross robot" },
    ];
    const { container } = render(
      <GanttPanel
        bars={stepBars}
        lanes={["robot0", "robot1"]}
        warnings={[]}
        time={0}
        playing={false}
        viewMode="step"
        onSetAfter={onSetAfter}
      />,
    );
    vi.spyOn(container.querySelector(".gantt-tracks")!, "getBoundingClientRect")
      .mockReturnValue(rect(0, 0, 1000, 80));

    fireEvent.pointerDown(screen.getByRole("button", { name: "task a" }), { clientX: 0 });
    fireEvent.pointerMove(window, { clientX: 1000, clientY: 10 });
    fireEvent.pointerUp(window);

    expect(onSetAfter).toHaveBeenCalledWith("a", "cross");
  });
});
