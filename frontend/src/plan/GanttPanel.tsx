/**
 * Presentational Gantt: robot lanes with time-positioned step bars, a playhead,
 * a conflict marker row + click-to-open conflicts popover, and transport
 * controls. Rows read top-to-bottom as the plan; bars extend horizontally as
 * the schedule.
 *
 * Interaction is view-specific. In Task view a plain drag places the task in a
 * robot's order (same lane = reorder, other lane = reassign + insert), while
 * Shift-drag onto a task on the other lane adds an explicit `after` dependency.
 * The two outcomes are separated by the modifier rather than by competing hit
 * areas, so a dependency drop can use the whole target bar without eating the
 * gaps that insert needs. Step view preserves its own cross-lane `after`.
 */
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { ganttLanes, ganttTotal, isSupportTaskBar, type GanttBar } from "./ganttModel";
import { clusterConflicts } from "./ganttConflicts";
import type { Conflict } from "./planTypes";
import { colorForRobot, ganttColorsForRobot } from "../robotVisuals";

type GanttPanelProps = {
  bars: GanttBar[];
  /** Last-compiled positions of task bars moved in the current draft. */
  ghostBars?: GanttBar[];
  /** The horizontal layout expresses draft order, not compiled seconds. */
  draftProjection?: boolean;
  warnings: string[];
  /** Structured conflicts; drives the timeline markers + popover. `warnings` remains the badge-count fallback. */
  conflicts?: Conflict[];
  time: number;
  playing: boolean;
  status?: string;
  /** Robot lanes always shown, even with no bars (idle robots / no plan yet). */
  lanes?: string[];
  collapsed?: boolean;
  selectedKey?: string | null;
  transportDisabled?: boolean;
  /** "task" = one bar per task (allocation view), "step" = per-step bars (drag view). */
  viewMode?: "task" | "step";
  /** Playback speed multiplier; renders a speed selector when onSetSpeed is set. */
  speed?: number;
  onSetSpeed?: (speed: number) => void;
  headerActions?: ReactNode;
  onToggleView?: () => void;
  onToggleCollapse?: () => void;
  onPlayToggle?: () => void;
  onReset?: () => void;
  /** Seek the compiled schedule to an absolute time in seconds. */
  onSeek?: (time: number) => void;
  /** Plan-reference mode: clicking a task bar inserts a semantic-task token. */
  onReferenceBar?: (bar: GanttBar) => void;
  /** Ordinary click: select a bar so the owner can show its scene trajectory. */
  onSelectBar?: (bar: GanttBar) => void;
  /** Semantic move ids which may be staged for removal in task view. */
  removableTaskIds?: ReadonlySet<string>;
  /** Semantic move ids currently staged for removal. */
  pendingRemovalIds?: ReadonlySet<string>;
  /** Toggle a semantic task's staged removal state. */
  onToggleTaskRemoval?: (actionId: string) => void;
  /** Semantic task ids that may be structurally moved in task view. */
  draggableTaskIds?: ReadonlySet<string>;
  /** Task move committed. Null means the first slot on the destination robot. */
  onMoveTask?: (actionId: string, robot: string, afterActionId: string | null) => void;
  /** Drag committed: set this step/task head's `after` to [afterId], or clear when null. */
  onSetAfter?: (stepId: string, afterId: string | null) => void;
  /**
   * Task-view cross-robot dependency committed, in SEMANTIC ids. Compiled step
   * keys are deliberately not used here: a task's last-finishing compiled step
   * is often compiler-generated (`#reposition`, `#detour_`, `#go_to_rest`,
   * `#yield`) and absent from the authored plan, so passing it on produced a
   * silently discarded edit. The owner resolves both ids against its plan.
   */
  onSetTaskAfter?: (actionId: string, afterActionId: string) => void;
};

const fmt = (s: number) => `${s.toFixed(1)}s`;
const DRAG_THRESHOLD_PX = 4;
const SNAP_FRACTION = 0.05;
// Temporary presentation-only switch. Warning/conflict data and timeline
// markers remain active; only the compact header badge/popover entry is hidden.
const TEMP_HIDE_WARNING_BADGE = true;

type DragState = {
  kind: "task" | "dependency";
  stepId: string;
  actionId: string | null;
  sourceRobot: string;
  targetRobot: string | null;
  afterActionId: string | null;
  leftPct: number;
  topOffsetPx: number;
  snapId: string | null;
  taskDropKind: "reorder" | "dependency" | "insert" | null;
  moved: boolean;
};

type DragInternals = DragState & { startX: number; startY: number };

/** Human-friendly ruler ticks, targeting roughly 6–10 labels at any duration. */
export function timelineTicks(total: number, targetCount = 8): number[] {
  if (total <= 0) return [0];
  const rough = total / Math.max(1, targetCount);
  const power = 10 ** Math.floor(Math.log10(rough));
  const normalized = rough / power;
  const nice = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  const step = nice * power;
  const ticks = [0];
  for (let value = step; value < total - step * 0.15; value += step) {
    ticks.push(value);
  }
  if (Math.abs(ticks[ticks.length - 1] - total) > 1e-6) ticks.push(total);
  return ticks;
}

export function GanttPanel({
  bars,
  ghostBars = [],
  draftProjection = false,
  warnings,
  conflicts,
  time,
  playing,
  status,
  lanes: baseLanes,
  collapsed = false,
  selectedKey,
  transportDisabled = false,
  viewMode = "step",
  speed = 1,
  onSetSpeed,
  headerActions,
  onToggleView,
  onToggleCollapse,
  onPlayToggle,
  onReset,
  onSeek,
  onReferenceBar,
  onSelectBar,
  removableTaskIds,
  pendingRemovalIds,
  onToggleTaskRemoval,
  draggableTaskIds,
  onMoveTask,
  onSetAfter,
  onSetTaskAfter,
}: GanttPanelProps) {
  const total = Math.max(ganttTotal(bars), ganttTotal(ghostBars)) || 1;
  const lanes = ganttLanes([...bars, ...ghostBars], baseLanes);
  const pct = (v: number) => `${(v / total) * 100}%`;

  const tracksRef = useRef<HTMLDivElement>(null);
  const rulerRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragInternals | null>(null);
  const laneRefs = useRef(new Map<string, HTMLDivElement>());
  const [drag, setDrag] = useState<DragState | null>(null);
  const [scrubbing, setScrubbing] = useState(false);
  const seekDisabled = draftProjection || transportDisabled || bars.length === 0 || !onSeek;
  const ticks = draftProjection ? [] : timelineTicks(total);

  const hasConflicts = !!conflicts && conflicts.length > 0;
  const badgeCount = hasConflicts ? conflicts.length : warnings.length;
  const timedConflicts = hasConflicts ? conflicts.filter((c) => c.window !== null) : [];
  const untimedConflicts = hasConflicts ? conflicts.filter((c) => c.window === null) : [];
  const clusters = hasConflicts ? clusterConflicts(conflicts, total) : [];

  const [warnOpen, setWarnOpen] = useState(false);
  const warnAnchorRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!warnOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (warnAnchorRef.current && !warnAnchorRef.current.contains(event.target as Node)) {
        setWarnOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setWarnOpen(false);
    };
    window.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [warnOpen]);

  const seekToWindowStart = (win: [number, number] | null) => {
    if (!win) return;
    onSeek?.(Math.max(0, Math.min(total, win[0])));
  };

  const seekAtClientX = (clientX: number) => {
    const rect = rulerRef.current?.getBoundingClientRect();
    if (seekDisabled || !rect || rect.width === 0) return;
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    onSeek?.(ratio * total);
  };

  const beginSeek = (clientX: number) => {
    if (seekDisabled) return;
    setScrubbing(true);
    seekAtClientX(clientX);
    const move = (event: PointerEvent) => seekAtClientX(event.clientX);
    const up = () => {
      window.removeEventListener("pointermove", move);
      setScrubbing(false);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up, { once: true });
  };

  // Attach the move/up listeners synchronously on pointerdown (not via an
  // effect) so a fast click still gets its pointerup — the earlier effect-based
  // attach could miss it, so bars never opened the editor.
  const beginDrag = (bar: GanttBar, clientX: number, clientY: number, shiftKey: boolean) => {
    if (onReferenceBar) {
      onReferenceBar(bar);
      return;
    }
    const actionId = bar.group ?? null;
    const taskDrag = viewMode === "task" && !!actionId && !!draggableTaskIds?.has(actionId);
    if (viewMode === "task" && !taskDrag) {
      onSelectBar?.(bar);
      return;
    }
    const state: DragInternals = {
      kind: taskDrag ? "task" as const : "dependency" as const,
      stepId: bar.key,
      actionId,
      sourceRobot: bar.robot,
      targetRobot: taskDrag ? bar.robot : null,
      afterActionId: null,
      leftPct: (bar.start / total) * 100,
      topOffsetPx: 0,
      snapId: null,
      taskDropKind: null,
      moved: false,
      startX: clientX,
      startY: clientY,
    };
    dragRef.current = state;
    setDrag(state);

    // Last pointer position and modifier state, so pressing or releasing Shift
    // re-classifies the drop without requiring further pointer movement.
    const pointer = { x: clientX, y: clientY, shift: shiftKey };

    const classifyTask = (d: DragInternals, cursorT: number) => {
      const laneEntry = [...laneRefs.current.entries()].find(([, lane]) => {
        const laneRect = lane.getBoundingClientRect();
        return pointer.y >= laneRect.top && pointer.y <= laneRect.bottom;
      });
      if (!laneEntry) {
        d.targetRobot = null;
        d.afterActionId = null;
        d.snapId = null;
        d.taskDropKind = null;
        return;
      }
      const [targetRobot, targetLane] = laneEntry;
      const sourceLane = laneRefs.current.get(d.sourceRobot);
      const laneOffset = sourceLane
        ? targetLane.getBoundingClientRect().top - sourceLane.getBoundingClientRect().top
        : 0;
      // A dependency target only has to be an authored semantic task; the same
      // `draggableTaskIds` set expresses that, and it correctly excludes
      // compiler-only groups (`#yield`, `#go_to_rest`) that cannot be authored.
      const candidates = bars
        .filter((candidate) => candidate.robot === targetRobot
          && candidate.group !== d.actionId
          && !!candidate.group
          && !!draggableTaskIds?.has(candidate.group))
        .sort((a, b) => a.start - b.start);

      if (pointer.shift) {
        // Shift-drag = add an `after` dependency, which is meaningful only
        // across robots (same-lane order is already an explicit dependency).
        const target = targetRobot === d.sourceRobot
          ? undefined
          : candidates.find((candidate) => cursorT >= candidate.start
            && cursorT <= candidate.start + candidate.duration);
        d.targetRobot = targetRobot;
        d.afterActionId = target?.group ?? null;
        d.snapId = target?.key ?? null;
        d.taskDropKind = target ? "dependency" : null;
        // A dependency drop aligns the source task in time with a task on the
        // other robot, but it does not reassign the source task. Keep the
        // dragged bar on its own lane; the target-lane caret still identifies
        // which cross-robot task supplies the dependency.
        d.topOffsetPx = 0;
        if (target) d.leftPct = ((target.start + target.duration) / total) * 100;
        return;
      }

      // Plain drag = place the task in this robot's order. The slot is chosen
      // by target midpoints so every point on the lane resolves to a slot; the
      // caret shows exactly where the task will land.
      let afterActionId: string | null = null;
      let insertT = candidates.length > 0
        ? Math.min(...candidates.map((candidate) => candidate.start))
        : 0;
      for (const candidate of candidates) {
        if (cursorT < candidate.start + candidate.duration / 2) break;
        afterActionId = candidate.group!;
        insertT = candidate.start + candidate.duration;
      }
      d.targetRobot = targetRobot;
      d.afterActionId = afterActionId;
      d.snapId = null;
      d.taskDropKind = targetRobot === d.sourceRobot ? "reorder" : "insert";
      d.leftPct = (insertT / total) * 100;
      d.topOffsetPx = laneOffset;
    };

    const reclassify = () => {
      const d = dragRef.current;
      const rect = tracksRef.current?.getBoundingClientRect();
      if (!d || !rect || rect.width === 0) return;
      const cursorT = Math.max(
        0,
        Math.min(total, ((pointer.x - rect.left) / rect.width) * total),
      );
      // Vertical travel counts too: dragging straight down into the other lane
      // is the most natural reassignment gesture and used to register as a
      // click because only |dx| was measured.
      d.moved = d.moved
        || Math.abs(pointer.x - d.startX) > DRAG_THRESHOLD_PX
        || Math.abs(pointer.y - d.startY) > DRAG_THRESHOLD_PX;
      if (d.kind === "task") {
        classifyTask(d, cursorT);
        setDrag({ ...d });
        return;
      }
      let snapId: string | null = null;
      let snapT = cursorT;
      let best = total * SNAP_FRACTION;
      for (const b of bars) {
        if (b.key === d.stepId || b.robot === d.sourceRobot) continue;
        const end = b.start + b.duration;
        const dist = Math.abs(end - cursorT);
        if (dist <= best) {
          best = dist;
          snapId = b.key;
          snapT = end;
        }
      }
      d.snapId = snapId;
      d.leftPct = (snapT / total) * 100;
      setDrag({ ...d });
    };

    const move = (e: PointerEvent) => {
      pointer.x = e.clientX;
      pointer.y = e.clientY;
      pointer.shift = e.shiftKey;
      reclassify();
    };
    const onModifier = (e: KeyboardEvent) => {
      if (e.key !== "Shift") return;
      pointer.shift = e.type === "keydown";
      reclassify();
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("keydown", onModifier);
      window.removeEventListener("keyup", onModifier);
      const d = dragRef.current;
      dragRef.current = null;
      setDrag(null);
      if (!d) return;
      if (!d.moved) {
        onSelectBar?.(bar);
        return;
      }
      if (d.moved) {
        if (d.kind === "task") {
          if (d.taskDropKind === "dependency" && d.actionId && d.afterActionId) {
            onSetTaskAfter?.(d.actionId, d.afterActionId);
          } else if (
            d.actionId
            && d.targetRobot
            && (d.taskDropKind === "reorder" || d.taskDropKind === "insert")
          ) {
            onMoveTask?.(d.actionId, d.targetRobot, d.afterActionId);
          }
          return;
        }
        // Snap to the target bar's END step (a task bar ends at its last step),
        // so a dragged task's first step waits on the target task's last step.
        const snapBar = d.snapId ? bars.find((b) => b.key === d.snapId) : null;
        const afterId = snapBar ? snapBar.endStepKey ?? snapBar.key : null;
        onSetAfter?.(d.stepId, afterId);
      }
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("keydown", onModifier);
    window.addEventListener("keyup", onModifier);
    window.addEventListener("pointerup", up, { once: true });
  };

  return (
    <section className="gantt-panel" aria-label="Plan and timeline">
      <div className="gantt-head">
        <button
          type="button"
          className="gantt-icon-btn"
          onClick={onToggleCollapse}
          aria-label={collapsed ? "Expand plan" : "Collapse plan"}
        >
          {collapsed ? "▸" : "▾"}
        </button>
        <strong className="gantt-title">Plan</strong>
        {onToggleView ? (
          <button
            type="button"
            className="gantt-icon-btn"
            onClick={onToggleView}
            title="Toggle task / step view"
          >
            {viewMode === "task" ? "▭ task" : "≣ step"}
          </button>
        ) : null}
        {!TEMP_HIDE_WARNING_BADGE && badgeCount > 0 ? (
          <div className="gantt-warn-anchor" ref={warnAnchorRef}>
            <button
              type="button"
              className={"gantt-warn-badge" + (warnOpen ? " is-open" : "")}
              aria-haspopup="dialog"
              aria-expanded={warnOpen}
              onClick={() => setWarnOpen((o) => !o)}
            >
              ⚠ {badgeCount}
            </button>
            {warnOpen ? (
              <div className="gantt-warn-popover" role="dialog" aria-label="Plan conflicts">
                {hasConflicts ? (
                  <>
                    {timedConflicts.map((c, i) => (
                      <div
                        key={`t-${i}`}
                        className="gantt-warn-row is-clickable"
                        onClick={() => seekToWindowStart(c.window)}
                      >
                        ⚠ {c.message}
                      </div>
                    ))}
                    {untimedConflicts.length > 0 ? (
                      <>
                        <div className="gantt-warn-group-heading">Not time-related</div>
                        {untimedConflicts.map((c, i) => (
                          <div key={`u-${i}`} className="gantt-warn-row">
                            ⚠ {c.message}
                          </div>
                        ))}
                      </>
                    ) : null}
                  </>
                ) : (
                  warnings.map((w) => (
                    <div key={w} className="gantt-warn-row">
                      ⚠ {w}
                    </div>
                  ))
                )}
              </div>
            ) : null}
          </div>
        ) : null}
        {headerActions}
        <span className="gantt-spacer" />
        {status ? <span className="gantt-status">{status}</span> : null}
        {onSetSpeed ? (
          <select
            className="gantt-speed"
            aria-label="Playback speed"
            value={speed}
            disabled={transportDisabled || bars.length === 0}
            onChange={(event) => onSetSpeed(Number(event.target.value))}
          >
            <option value={0.25}>0.25×</option>
            <option value={0.5}>0.5×</option>
            <option value={1}>1×</option>
            <option value={2}>2×</option>
            <option value={4}>4×</option>
            <option value={8}>8×</option>
          </select>
        ) : null}
        <button
          type="button"
          className="gantt-icon-btn"
          onClick={onPlayToggle}
          disabled={transportDisabled || bars.length === 0}
          aria-label={playing ? "Pause" : "Play"}
        >
          {playing ? "❚❚" : "▶"}
        </button>
        <button
          type="button"
          className="gantt-icon-btn"
          onClick={onReset}
          disabled={transportDisabled || bars.length === 0}
          aria-label="Reset"
        >
          ⟲
        </button>
      </div>

      <div className={"gantt-body" + (collapsed ? " is-collapsed" : "")}>
            <div className="gantt-labels">
              {collapsed ? null : lanes.map((robot) => (
                <span
                  key={robot}
                  className="gantt-lane-label"
                  style={{ "--robot-color": colorForRobot(robot) } as CSSProperties}
                >
                  <span className="gantt-lane-dot" aria-hidden="true" />
                  <span>{robot}</span>
                </span>
              ))}
              <span className="gantt-ruler-label">{draftProjection ? "order" : "time"}</span>
            </div>
            <div className="gantt-tracks" ref={tracksRef}>
              {collapsed ? null : lanes.map((robot) => {
                const barColors = ganttColorsForRobot(robot);
                return (
                  <div
                    key={robot}
                    ref={(node) => {
                      if (node) laneRefs.current.set(robot, node);
                      else laneRefs.current.delete(robot);
                    }}
                    className={"gantt-track" + (drag?.kind === "task"
                      && drag.targetRobot === robot
                      && drag.taskDropKind !== null ? " is-drop-target" : "")}
                    style={{
                      "--robot-bar-background": barColors.background,
                      "--robot-bar-border": barColors.border,
                    } as CSSProperties}
                  >
                  {ghostBars
                    .filter((bar) => bar.robot === robot)
                    .map((bar) => (
                      <div
                        key={`ghost:${bar.key}`}
                        className="gantt-bar-frame is-drag-origin"
                        style={{ left: pct(bar.start), width: pct(bar.duration) }}
                        title={`${bar.label} · last compiled position`}
                        aria-hidden="true"
                      >
                        <span className={"gantt-bar" + (isSupportTaskBar(bar) ? " is-support" : "")}>
                          <span className="gantt-bar-label">{bar.label}</span>
                        </span>
                      </div>
                    ))}
                  {bars
                    .filter((b) => b.robot === robot)
                    .map((b) => {
                      const dragging = drag?.stepId === b.key && drag.moved;
                      const isSnap = drag?.moved && drag.snapId === b.key;
                      const isSelected =
                        !!selectedKey &&
                        (b.key === selectedKey || (b.memberKeys?.includes(selectedKey) ?? false));
                      const actionId = viewMode === "task" ? b.group ?? null : null;
                      const removable = !!actionId && !!removableTaskIds?.has(actionId);
                      const pendingRemoval = !!actionId && !!pendingRemovalIds?.has(actionId);
                      const taskDraggable = !!actionId && !!draggableTaskIds?.has(actionId);
                      return (
                        <div
                          key={b.key}
                          className={
                            "gantt-bar-frame" +
                            (removable ? " has-remove" : "") +
                            (pendingRemoval ? " is-pending-removal" : "")
                          }
                          style={{
                            left: dragging ? `${drag.leftPct}%` : pct(b.start),
                            width: pct(b.duration),
                            transform: dragging && drag.kind === "task"
                              ? `translateY(${drag.topOffsetPx}px)` : undefined,
                          }}
                          title={pendingRemoval ? `${b.label} · Pending removal` : undefined}
                        >
                        <button
                          type="button"
                          className={
                            "gantt-bar" +
                            (isSupportTaskBar(b) ? " is-support" : "") +
                            (b.warned ? " is-warned" : "") +
                            (b.estimated ? " is-estimated" : "") +
                            (isSelected ? " is-selected" : "") +
                            (dragging ? " is-dragging" : "") +
                            (isSnap ? " is-snap-target" : "") +
                            (b.pendingAfter === "waiter" ? " is-after-waiter" : "") +
                            (b.pendingAfter === "anchor" ? " is-after-anchor" : "")
                          }
                          title={pendingRemoval
                            ? `${b.label} · Pending removal · click Undo to restore`
                            : `${b.label} · ${b.estimated ? "estimated " : ""}${fmt(b.start)}→${fmt(b.start + b.duration)}`
                              + (b.pendingAfter === "waiter"
                                ? "\nPending dependency · now waits for the other highlighted task"
                                : b.pendingAfter === "anchor"
                                  ? "\nPending dependency · the other highlighted task waits for this"
                                  : "")
                              + (taskDraggable
                                ? "\nDrag to reorder or reassign · Shift-drag onto the other robot's task to wait for it"
                                : "")}
                          disabled={pendingRemoval}
                          onPointerDown={(e) => {
                            e.preventDefault();
                            beginDrag(b, e.clientX, e.clientY, e.shiftKey);
                          }}
                        >
                          <span className="gantt-bar-label">{b.label}</span>
                        </button>
                        {removable && onToggleTaskRemoval ? (
                          <button
                            type="button"
                            className="gantt-bar-remove"
                            aria-label={pendingRemoval ? `Undo removal of ${b.label}` : `Remove ${b.label}`}
                            title={pendingRemoval ? "Undo pending removal" : "Remove task on next Sync"}
                            onPointerDown={(event) => event.stopPropagation()}
                            onClick={(event) => {
                              event.stopPropagation();
                              onToggleTaskRemoval(actionId!);
                            }}
                          >
                            {pendingRemoval ? "↶" : "×"}
                          </button>
                        ) : null}
                        </div>
                      );
                    })}
                  {drag?.kind === "task"
                    && drag.moved
                    && drag.targetRobot === robot
                    && (drag.taskDropKind === "reorder" || drag.taskDropKind === "insert") ? (
                    <span className="gantt-insert-caret" style={{ left: `${drag.leftPct}%` }} />
                  ) : null}
                  {drag?.kind === "task"
                    && drag.moved
                    && drag.targetRobot === robot
                    && drag.taskDropKind === "dependency" ? (
                    <span
                      className="gantt-after-caret"
                      style={{ left: `${drag.leftPct}%` }}
                      title="Wait for this task"
                    />
                  ) : null}
                  </div>
                );
              })}
              {!collapsed && clusters.length > 0 ? (
                <div className="gantt-marker-row">
                  {clusters.map((cluster, index) => (
                    <button
                      type="button"
                      key={index}
                      className="gantt-conflict-marker"
                      style={{
                        left: pct(cluster.start),
                        // Never thinner than the ruler minor tick, so a
                        // near-instant window stays visible and clickable.
                        minWidth: 14,
                        width: pct(cluster.end - cluster.start),
                      }}
                      title={cluster.conflicts.map((c) => c.message).join("\n")}
                      onClick={() => onSeek?.(Math.max(0, Math.min(total, cluster.start)))}
                    >
                      ⚠{cluster.conflicts.length > 1 ? cluster.conflicts.length : ""}
                    </button>
                  ))}
                </div>
              ) : null}
              <div
                ref={rulerRef}
                className={
                  "gantt-ruler" +
                  (seekDisabled ? " is-disabled" : "") +
                  (scrubbing ? " is-scrubbing" : "")
                }
                role="slider"
                tabIndex={seekDisabled ? -1 : 0}
                aria-label="Plan time"
                aria-valuemin={0}
                aria-valuemax={total}
                aria-valuenow={Math.max(0, Math.min(total, time))}
                aria-valuetext={fmt(Math.max(0, Math.min(total, time)))}
                aria-disabled={seekDisabled}
                title={seekDisabled ? "Compile the plan to enable seeking" : "Click or drag to seek"}
                onPointerDown={(event) => {
                  event.preventDefault();
                  beginSeek(event.clientX);
                }}
                onKeyDown={(event) => {
                  if (seekDisabled) return;
                  const step = Math.max(0.1, ticks[1] ?? total / 10);
                  let target = time;
                  if (event.key === "ArrowLeft" || event.key === "ArrowDown") target -= step;
                  else if (event.key === "ArrowRight" || event.key === "ArrowUp") target += step;
                  else if (event.key === "Home") target = 0;
                  else if (event.key === "End") target = total;
                  else return;
                  event.preventDefault();
                  onSeek?.(Math.max(0, Math.min(total, target)));
                }}
              >
                {draftProjection ? (
                  <span className="gantt-draft-ruler-text">draft order · sync for timing</span>
                ) : null}
                {ticks.map((tick, index) => (
                  <span
                    key={tick}
                    className={
                      "gantt-ruler-tick" +
                      (index === 0 ? " is-first" : "") +
                      (index === ticks.length - 1 ? " is-last" : "")
                    }
                    style={{ left: pct(tick) }}
                  >
                    <span className="gantt-ruler-mark" />
                    <span className="gantt-ruler-text">{fmt(tick)}</span>
                  </span>
                ))}
              </div>
              {bars.length > 0 && !draftProjection ? (
                <div className="gantt-playhead" style={{ left: pct(time) }} />
              ) : null}
            </div>
      </div>
    </section>
  );
}
