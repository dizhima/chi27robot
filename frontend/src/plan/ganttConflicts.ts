/**
 * Pure clustering of timed conflicts for the Gantt timeline marker row.
 * Conflicts whose windows are close together (or overlap) collapse into one
 * marker so a dense run of near-simultaneous conflicts doesn't paper the
 * ruler with overlapping buttons.
 */
import type { Conflict } from "./planTypes";

export type ConflictCluster = {
  /** Midpoint of the cluster's combined window, in seconds. */
  mid: number;
  /** Combined window [start, end] in seconds — the marker renders this whole
   *  span as a highlighted interval, and a click seeks to `start`. */
  start: number;
  end: number;
  conflicts: Conflict[];
};

export function clusterConflicts(conflicts: Conflict[], total: number): ConflictCluster[] {
  const timed = conflicts.filter(
    (c): c is Conflict & { window: [number, number] } => c.window != null,
  );
  const sorted = [...timed].sort(
    (a, b) => a.window[0] + a.window[1] - (b.window[0] + b.window[1]),
  );
  const threshold = total * 0.02;

  type Building = { start: number; end: number; conflicts: Conflict[] };
  const clusters: Building[] = [];

  for (const c of sorted) {
    const [w0, w1] = c.window;
    const mid = (w0 + w1) / 2;
    const last = clusters[clusters.length - 1];
    if (last) {
      const lastMid = (last.start + last.end) / 2;
      const overlaps = w0 <= last.end && w1 >= last.start;
      if (Math.abs(mid - lastMid) <= threshold || overlaps) {
        last.start = Math.min(last.start, w0);
        last.end = Math.max(last.end, w1);
        last.conflicts.push(c);
        continue;
      }
    }
    clusters.push({ start: w0, end: w1, conflicts: [c] });
  }

  return clusters.map((cl) => ({
    mid: (cl.start + cl.end) / 2,
    start: cl.start,
    end: cl.end,
    conflicts: cl.conflicts,
  }));
}
