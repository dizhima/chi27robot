const ROBOT_COLORS = ["#2563EB", "#f472b6", "#a3e635", "#fbbf24"] as const;

/** Stable identity colour derived from a `robotN` namespace. */
export function colorForRobot(robot: string): string {
  const match = /^robot(\d+)$/.exec(robot);
  if (!match) return ROBOT_COLORS[2];
  return ROBOT_COLORS[Number(match[1]) % ROBOT_COLORS.length];
}
