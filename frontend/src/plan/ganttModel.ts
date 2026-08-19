/**
 * Presentational model for the Gantt: one bar per scheduled item. Derived from
 * a compile_plan response (ScheduledItem[] + warnings) but deliberately free of
 * track data, so GanttPanel is a pure view and can be driven by a static mock
 * during layout work before the live compile is wired in.
 */
import type { ScheduleEntry } from "./planTypes";
import type { AugmentedAction } from "./authorPlan";

export type GanttBar = {
  /** = the authored step id, so a bar click maps straight to a plan step. */
  key: string;
  robot: string;
  label: string;
  /** Compiled/authored operation; used only for presentation transforms. */
  op?: string | null;
  start: number;
  duration: number;
  group?: string | null;
  facility?: string | null;
  /** True when a conflict warning names this bar (robot/label) — tints it. */
  warned: boolean;
  /** Duration is a local preview estimate pending the next backend compile. */
  estimated?: boolean;
  /** Step keys covered by this bar (task-view aggregate bars); absent for step bars. */
  memberKeys?: string[];
  /** Step id at this bar's right edge — the `after` target when another bar snaps to it. Defaults to `key`. */
  endStepKey?: string;
  /** Visual distinction between object-moving work and support/coordination work. */
  tone?: "primary" | "support";
  /** Role in an unsynced `after` dependency, for pending-edit highlighting. */
  pendingAfter?: "waiter" | "anchor";
  /** Compiler-generated coordination metadata. */
  source?: string | null;
  repairKind?: string | null;
  parentGroup?: string | null;
  detourOverridden?: boolean;
};

export type TaskMoveProjectionEdit = {
  actionId: string;
  sourceRobot: string;
  robot: string;
  afterActionId: string | null;
};

export type TaskMoveProjection = {
  bars: GanttBar[];
  ghostBars: GanttBar[];
};

/**
 * Project pending task moves without pretending to calculate a schedule.
 *
 * Every bar keeps the duration from the last successful compile. Only lanes
 * touched by a move are packed into the edited program order; untouched lanes
 * retain their compiled positions. A moved task's compiled bar is also
 * returned as a non-interactive origin ghost for the Gantt to render.
 */
export function projectTaskMoveBars(
  compiledBars: GanttBar[],
  edits: TaskMoveProjectionEdit[],
): TaskMoveProjection {
  if (edits.length === 0) return { bars: compiledBars, ghostBars: [] };

  const originalIndex = new Map(compiledBars.map((bar, index) => [bar.key, index]));
  const lanes = new Map<string, GanttBar[]>();
  for (const bar of compiledBars) {
    if (!lanes.has(bar.robot)) lanes.set(bar.robot, []);
    lanes.get(bar.robot)!.push({ ...bar });
  }
  for (const lane of lanes.values()) {
    lane.sort((a, b) => a.start - b.start
      || (originalIndex.get(a.key) ?? 0) - (originalIndex.get(b.key) ?? 0));
  }

  const origins = new Map<string, GanttBar>();
  const movedKeys = new Map<string, string>();
  for (const edit of edits) {
    let sourceRobot = edit.sourceRobot;
    let sourceLane = lanes.get(sourceRobot);
    let sourceIndex = sourceLane?.findIndex((bar) => bar.group === edit.actionId) ?? -1;
    if (sourceIndex < 0) {
      for (const [robot, lane] of lanes) {
        const index = lane.findIndex((bar) => bar.group === edit.actionId);
        if (index < 0) continue;
        sourceRobot = robot;
        sourceLane = lane;
        sourceIndex = index;
        break;
      }
    }
    if (!sourceLane || sourceIndex < 0) continue;
    const [moved] = sourceLane.splice(sourceIndex, 1);
    if (!origins.has(edit.actionId)) origins.set(edit.actionId, { ...moved });
    movedKeys.set(edit.actionId, moved.key);

    const targetLane = lanes.get(edit.robot) ?? [];
    if (!lanes.has(edit.robot)) lanes.set(edit.robot, targetLane);
    const anchorIndex = edit.afterActionId
      ? targetLane.findIndex((bar) => bar.group === edit.afterActionId)
      : -1;
    const insertAt = edit.afterActionId && anchorIndex >= 0 ? anchorIndex + 1 : 0;

    if (sourceRobot === edit.robot) {
      targetLane.splice(insertAt, 0, moved);
      const compiledLane = compiledBars.filter((bar) => bar.robot === edit.robot);
      let cursor = compiledLane.length > 0
        ? Math.min(...compiledLane.map((bar) => bar.start))
        : 0;
      for (const bar of targetLane) {
        bar.start = cursor;
        cursor += bar.duration;
      }
      continue;
    }

    const insertStart = anchorIndex >= 0
      ? targetLane[anchorIndex].start + targetLane[anchorIndex].duration
      : targetLane.length > 0
        ? Math.min(...targetLane.map((bar) => bar.start))
        : 0;
    for (let index = insertAt; index < targetLane.length; index++) {
      targetLane[index].start += moved.duration;
    }
    targetLane.splice(insertAt, 0, {
      ...moved,
      robot: edit.robot,
      start: insertStart,
    });
  }

  const bars = [...lanes.values()].flat();
  const finalByKey = new Map(bars.map((bar) => [bar.key, bar]));
  const ghostBars = [...origins.entries()].flatMap(([actionId, origin]) => {
    const movedKey = movedKeys.get(actionId);
    const projected = movedKey ? finalByKey.get(movedKey) : undefined;
    return projected && projected.robot !== origin.robot ? [origin] : [];
  });
  return { bars, ghostBars };
}

export type TaskAfterProjectionEdit = {
  /** Semantic task that must wait. */
  actionId: string;
  /** Semantic task it waits for. */
  afterActionId: string;
};

/**
 * Project pending cross-robot `after` edits so the dependency is visible before
 * Sync: the waiting task's left edge is pulled to the anchor task's right edge,
 * and both ends are tagged for highlighting.
 *
 * Without this the bars never move (the Gantt stays on the last compiled
 * schedule until Sync), so a successful dependency edit looked identical to a
 * dropped one. Neither robot changes and no ghost is produced — the task keeps
 * its lane, it only starts later.
 *
 * Durations still come from the last compile; only start times move. A task
 * already starting after its anchor is left alone, since a dependency can only
 * ever delay work, never pull it earlier.
 */
export function projectTaskAfterBars(
  bars: GanttBar[],
  edits: TaskAfterProjectionEdit[],
): GanttBar[] {
  if (edits.length === 0) return bars;
  const working = bars.map((bar) => ({ ...bar }));

  for (const edit of edits) {
    const anchors = working.filter((bar) => bar.group === edit.afterActionId);
    const waiters = working.filter((bar) => bar.group === edit.actionId);
    if (anchors.length === 0 || waiters.length === 0) continue;
    for (const anchor of anchors) anchor.pendingAfter = "anchor";
    for (const waiter of waiters) waiter.pendingAfter = "waiter";

    const anchorEnd = Math.max(...anchors.map((bar) => bar.start + bar.duration));
    const waiterStart = Math.min(...waiters.map((bar) => bar.start));
    // A robot is serial, so the waiter can never start before the work already
    // queued ahead of it on its own lane finishes. Lane order is captured before
    // any shift and then held fixed: delaying a task must push the rest of that
    // robot's program along, never let a later task overtake it.
    const lane = working
      .filter((bar) => bar.robot === waiters[0].robot)
      .sort((a, b) => a.start - b.start);
    const firstIndex = lane.findIndex((bar) => bar.group === edit.actionId);
    const previous = firstIndex > 0 ? lane[firstIndex - 1] : null;
    const floor = previous ? previous.start + previous.duration : 0;
    const delta = Math.max(anchorEnd, floor) - waiterStart;
    if (delta <= 0) continue;

    for (const waiter of waiters) waiter.start += delta;
    let cursor = Number.NEGATIVE_INFINITY;
    for (const bar of lane) {
      if (bar.start < cursor) bar.start = cursor;
      cursor = bar.start + bar.duration;
    }
  }

  return working;
}

const CONTIGUOUS_EPSILON_SECONDS = 1e-6;

/**
 * Step-view presentation transform: fold each contiguous reset into the
 * preceding visible bar on the same robot. The schedule itself is untouched.
 *
 * The primary bar keeps its key/label so click and drag still target the
 * meaningful operation. memberKeys records the hidden reset steps, while
 * endStepKey advances to the final reset so another bar snapping to this
 * visual block still waits for the complete sequence.
 */
export function mergeResetBars(bars: GanttBar[]): GanttBar[] {
  const merged: GanttBar[] = [];
  const lastIndexByRobot = new Map<string, number>();

  for (const source of bars) {
    const current: GanttBar = {
      ...source,
      memberKeys: source.memberKeys ? [...source.memberKeys] : undefined,
    };
    const previousIndex = lastIndexByRobot.get(current.robot);
    const previous = previousIndex === undefined ? undefined : merged[previousIndex];
    const isReset = current.op?.toLowerCase() === "reset";
    const contiguous = previous !== undefined
      && Math.abs(current.start - (previous.start + previous.duration))
        <= CONTIGUOUS_EPSILON_SECONDS;

    if (isReset && previous && contiguous) {
      const previousEnd = previous.start + previous.duration;
      const currentEnd = current.start + current.duration;
      previous.duration = Math.max(previousEnd, currentEnd) - previous.start;
      previous.warned ||= current.warned;
      previous.estimated ||= current.estimated;
      previous.memberKeys = [
        ...(previous.memberKeys ?? [previous.key]),
        ...(current.memberKeys ?? [current.key]),
      ];
      previous.endStepKey = current.endStepKey ?? current.key;
      continue;
    }

    merged.push(current);
    lastIndexByRobot.set(current.robot, merged.length - 1);
  }

  return merged;
}

/**
 * Human-readable label for a task bar, derived from the semantic action that
 * produced it (as opposed to the raw action id used for `key`/`group`
 * identity). Falls back to the action id when the op/fields don't match a
 * known shape, or when no action is available at all.
 */
export function prettyTaskLabel(action: AugmentedAction | undefined | null): string {
  if (!action) return "";
  switch (action.op) {
    case "move":
      if (action.object && action.dest) return `${action.object} → ${action.dest}`;
      break;
    case "open":
      if (action.facility) return `open ${action.facility}`;
      break;
    case "close":
      if (action.facility) return `close ${action.facility}`;
      break;
    case "go_to":
      if (action.target) return `go to ${action.target}`;
      break;
  }
  return action.id;
}

/** Compact Step Plan label. Geometry targets remain available through click/
 * tooltip/path visualization; repeating them inside narrow bars makes the
 * timeline unreadable. */
export function prettyStepLabel(entry: {
  op?: string | null;
  facility?: string | null;
  repairKind?: string | null;
}): string {
  if (entry.repairKind === "detour" || entry.repairKind === "go_away") {
    return "navigate";
  }
  const raw = entry.op ?? "";
  const op = raw.toLowerCase();
  if (op === "navigate" || op === "pick" || op === "place") return op;
  const action = op.startsWith("open") ? "open"
    : op.startsWith("close") ? "close" : null;
  if (action) {
    const rawFacility = entry.facility
      ?? raw.slice(action.length).split("_with_", 1)[0];
    const facility = rawFacility
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .replace(/_/g, " ")
      .trim()
      .toLowerCase();
    return facility ? `${action} ${facility}` : action;
  }
  return op || raw;
}

/**
 * Human-readable fallback for a task-bar group that has no author action —
 * notably resolver-generated go-to-rest and deadlock-yield groups, which would
 * otherwise show their raw ids. Returns the group unchanged when unrecognized.
 */
export function prettyGroupFallback(group: string): string {
  if (group.endsWith("#go_to_rest")) return "go to rest";
  if (group.endsWith("#yield")) return "detour";
  if (group.includes("#go_away") || group.includes("#detour")) return "detour";
  return group;
}

/** Fixture open/close and terminal rest are supporting work. Detours are
 * navigation in Step Plan, so they deliberately keep the normal blue bar. */
export function isSupportTaskBar(bar: GanttBar): boolean {
  if (bar.tone) return bar.tone === "support";
  const group = bar.group ?? "";
  if (
    group.endsWith("#go_to_rest")
  ) return true;
  const op = (bar.op ?? "").toLowerCase();
  return op.startsWith("open") || op.startsWith("close");
}

/**
 * Task-view aggregation: collapse a robot's contiguous same-`group` step bars
 * into one bar spanning their combined time range. Robot allocation is a
 * task-level concern, so this is the view for reassigning; step view keeps the
 * per-step bars for drag ordering. Bar `key` is the task's first step (so a
 * click still resolves to a plan step → its task). When `actionsById` is
 * given, the displayed `label` is the pretty human form of the group's
 * action; `group`/`key` identity is unchanged either way.
 */
export function aggregateToTaskBars(
  bars: GanttBar[],
  actionsById?: Map<string, AugmentedAction>,
): GanttBar[] {
  const groups = new Map<string, GanttBar[]>();
  const order: string[] = [];
  for (const b of bars) {
    const taskGroup = b.repairKind === "detour" && b.parentGroup
      ? b.parentGroup
      : b.group ?? b.key;
    const g = `${b.robot}::${taskGroup}`;
    if (!groups.has(g)) {
      groups.set(g, []);
      order.push(g);
    }
    groups.get(g)!.push(b);
  }
  return order.map((g) => {
    const members = groups.get(g)!;
    const start = Math.min(...members.map((m) => m.start));
    const end = Math.max(...members.map((m) => m.start + m.duration));
    const first = members.find((member) => member.repairKind !== "detour")
      ?? members[0];
    const taskGroup = first.group ?? first.key;
    // The member whose end defines the bar's right edge — the `after` anchor a
    // bar snapping to this task should depend on (its last-finishing step).
    const endMember = members.reduce((a, b) =>
      b.start + b.duration > a.start + a.duration ? b : a,
    );
    const action = actionsById?.get(taskGroup);
    const prettyLabel = action
      ? prettyTaskLabel(action)
      : taskGroup
        ? prettyGroupFallback(taskGroup)
        : "";
    const support = action
      ? action.op === "open" || action.op === "close"
      : members.some(isSupportTaskBar);
    return {
      key: first.key,
      robot: first.robot,
      label: prettyLabel || taskGroup || first.label,
      start,
      duration: end - start,
      group: taskGroup,
      facility: null,
      warned: members.some((m) => m.warned),
      estimated: members.some((m) => m.estimated),
      memberKeys: members.map((m) => m.key),
      endStepKey: endMember.key,
      tone: support ? "support" : "primary",
    };
  });
}

/**
 * Build bars from schedule entries, flagging any whose step id is in a conflict.
 * `warnedStepIds` comes from the structured `conflicts[].steps`, so tinting is
 * exact (an earlier substring match on the message text mis-tinted steps whose
 * label was a prefix of another, e.g. navigate_fridge ⊂ navigate_fridge_for_*).
 * aggregateToTaskBars then propagates a warned step up to its task bar.
 */
export function toGanttBars(
  schedule: ScheduleEntry[],
  warnedStepIds: Set<string>,
): GanttBar[] {
  return schedule.map((entry) => {
    return {
      key: entry.id,
      robot: entry.robot,
      label: prettyStepLabel({
        op: entry.op,
        facility: entry.facility,
        repairKind: entry.repair_kind,
      }),
      op: entry.op,
      start: entry.start,
      duration: entry.duration,
      group: entry.group,
      facility: entry.facility,
      warned: warnedStepIds.has(entry.id),
      source: entry.source,
      repairKind: entry.repair_kind,
      parentGroup: entry.parent_group,
      detourOverridden: entry.detour_overridden,
    };
  });
}

/** Total schedule length (seconds) — the Gantt's horizontal extent. */
export function ganttTotal(bars: GanttBar[]): number {
  return bars.reduce((max, b) => Math.max(max, b.start + b.duration), 0);
}

/**
 * Robot lanes to render, in stable order. `base` lanes are always included even
 * when no bar uses them (so an idle robot still gets an empty row, and the Gantt
 * shows its lanes before any plan exists); bars can introduce further lanes.
 */
export function ganttLanes(bars: GanttBar[], base: string[] = []): string[] {
  const seen = new Set<string>(base);
  for (const b of bars) seen.add(b.robot);
  return [...seen].sort();
}

/**
 * Static sample for layout work — mirrors the compiled SAMPLE_PLAN schedule
 * (both mugs to the sink + robot0 opens the fridge, the sink bars flagged).
 * Replaced by live toGanttBars(items, warnings) at integration time.
 */
export const MOCK_BARS: GanttBar[] = [
  { key: "r0:navigate_mug_1", robot: "robot0", label: "navigate_mug_1", start: 0, duration: 13.47, group: "mug_1 to sink", warned: false },
  { key: "r0:pick_mug_1", robot: "robot0", label: "pick_mug_1", start: 13.47, duration: 3.6, group: "mug_1 to sink", warned: false },
  { key: "r0:navigate_sink", robot: "robot0", label: "navigate_sink", start: 17.07, duration: 14.77, group: "mug_1 to sink", facility: "sink", warned: true },
  { key: "r0:place_mug_1_sink", robot: "robot0", label: "place_mug_1_sink", start: 31.83, duration: 3.3, group: "mug_1 to sink", facility: "sink", warned: true },
  { key: "r0:OpenFridge", robot: "robot0", label: "OpenFridge", start: 35.13, duration: 23.07, group: "open fridge", facility: "fridge", warned: false },
  { key: "r1:navigate_mug_2", robot: "robot1", label: "navigate_mug_2", start: 0, duration: 6.13, group: "mug_2 to sink", warned: false },
  { key: "r1:pick_mug_2", robot: "robot1", label: "pick_mug_2", start: 6.13, duration: 3.6, group: "mug_2 to sink", warned: false },
  { key: "r1:navigate_sink", robot: "robot1", label: "navigate_sink", start: 9.73, duration: 14.3, group: "mug_2 to sink", facility: "sink", warned: true },
  { key: "r1:place_mug_2_sink", robot: "robot1", label: "place_mug_2_sink", start: 24.03, duration: 3.3, group: "mug_2 to sink", facility: "sink", warned: true },
];
