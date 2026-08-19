import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { activeTaskLabel, PlanTaskSubtitles } from "./PlanTaskSubtitles";
import type { GanttBar } from "./ganttModel";

const bars: GanttBar[] = [
  { key: "a", robot: "robot0", label: "bowl_1 → island", start: 0, duration: 10, group: "a", warned: false },
  { key: "rest", robot: "robot0", label: "go to rest", start: 10, duration: 5, group: "robot0#go_to_rest", warned: false },
  { key: "detour", robot: "robot1", label: "detour", start: 0, duration: 4, group: "a#detour_robot1", warned: false },
  { key: "b", robot: "robot1", label: "spoon_2 → island", start: 4, duration: 6, group: "b", warned: false },
];

describe("PlanTaskSubtitles", () => {
  it("selects active semantic tasks with end-exclusive boundaries", () => {
    expect(activeTaskLabel(bars, "robot0", 9.99)).toBe("bowl_1 → island");
    expect(activeTaskLabel(bars, "robot0", 10)).toBeNull();
    expect(activeTaskLabel(bars, "robot1", 4)).toBe("spoon_2 → island");
  });

  it("suppresses detours and go-to-rest tasks while retaining both robot rows", () => {
    render(<PlanTaskSubtitles bars={bars} time={2} />);
    expect(screen.getByText("Robot 0:")).toBeInTheDocument();
    expect(screen.getByText("Robot 1:")).toBeInTheDocument();
    expect(screen.getByText("bowl_1 → island")).toBeInTheDocument();
    expect(screen.queryByText("detour")).not.toBeInTheDocument();
  });
});
