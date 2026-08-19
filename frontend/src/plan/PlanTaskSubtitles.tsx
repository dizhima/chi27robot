import type { GanttBar } from "./ganttModel";

const ROBOTS = ["robot0", "robot1"] as const;

type PlanTaskSubtitlesProps = {
  bars: GanttBar[];
  time: number;
};

/** Compiler-only coordination tasks are deliberately not presented as user work. */
export function isHiddenSubtitleTask(bar: GanttBar): boolean {
  const identity = `${bar.group ?? ""} ${bar.label}`.toLowerCase();
  return identity.includes("#go_to_rest")
    || identity.includes("#detour")
    || identity.endsWith("#yield detour")
    || bar.repairKind === "detour"
    || bar.repairKind === "go_away"
    || bar.label.trim().toLowerCase() === "go to rest"
    || bar.label.trim().toLowerCase() === "detour";
}

export function activeTaskLabel(
  bars: GanttBar[],
  robot: string,
  time: number,
): string | null {
  const active = bars
    .filter((bar) =>
      bar.robot === robot
      && time >= bar.start
      && time < bar.start + bar.duration
      && !isHiddenSubtitleTask(bar),
    )
    .sort((a, b) => b.start - a.start)[0];
  return active?.label.trim() || null;
}

export function PlanTaskSubtitles({ bars, time }: PlanTaskSubtitlesProps) {
  return (
    <section className="plan-task-subtitles" aria-label="Current robot tasks">
      {ROBOTS.map((robot, index) => (
        <div className="plan-task-subtitle-row" key={robot}>
          <span className={`plan-task-subtitle-dot robot-${index}`} aria-hidden="true" />
          <span className="plan-task-subtitle-name">Robot {index}:</span>
          <span className="plan-task-subtitle-task">
            {activeTaskLabel(bars, robot, time) ?? "—"}
          </span>
        </div>
      ))}
    </section>
  );
}
