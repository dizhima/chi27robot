const ROBOT_COLORS = ["#2563EB", "#f472b6", "#a3e635", "#fbbf24"] as const;

const ROBOT_GANTT_COLORS = [
  { background: "rgba(55, 138, 221, 0.28)", border: "rgba(80, 160, 230, 0.5)" },
  { background: "rgba(219, 74, 148, 0.28)", border: "rgba(244, 114, 182, 0.55)" },
  { background: "rgba(132, 204, 22, 0.28)", border: "rgba(163, 230, 53, 0.55)" },
  { background: "rgba(217, 154, 22, 0.28)", border: "rgba(251, 191, 36, 0.55)" },
] as const;

function paletteIndex(robot: string): number {
  const match = /^robot(\d+)$/.exec(robot);
  return match ? Number(match[1]) % ROBOT_COLORS.length : 2;
}

/** Stable identity colour derived from a `robotN` namespace. */
export function colorForRobot(robot: string): string {
  return ROBOT_COLORS[paletteIndex(robot)];
}

/** Muted fill and border colours used by every Gantt bar for a robot. */
export function ganttColorsForRobot(robot: string) {
  return ROBOT_GANTT_COLORS[paletteIndex(robot)];
}
