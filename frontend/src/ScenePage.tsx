import { useCallback, useEffect, useMemo, useRef, useState, type ComponentRef } from "react";
import { OrbitControls } from "@react-three/drei";
import { DragInteraction, MujocoCanvas, MujocoProvider } from "mujoco-react";
import type { MujocoSimAPI, SceneConfig } from "mujoco-react";
import {
  SelectionHighlight,
  backendUrl,
  defaultSceneConfig,
  defaultScenePath,
  suggestedScenePaths,
  initialStateKeyframe,
  mtMujocoWasmUrl,
  threadedMujocoLoader,
  type SceneSession,
} from "./mujocoBootstrap";
import { SchedulePlayer, type SchedulePlayerHandle } from "./SchedulePlayer";
import { GanttPanel } from "./plan/GanttPanel";
import { PlanOverlay, type StepMarker } from "./plan/PlanOverlay";
import { PlanTaskSubtitles } from "./plan/PlanTaskSubtitles";
import { chassisEndpointForMountDraft } from "./plan/standoffFrames";
import { RobotIdentityLabels, type RobotIdentityLabelPick } from "./RobotIdentityLabels";
import { SceneEntityLabels, type SceneEntityLabelPick } from "./SceneEntityLabels";
import { ReadyPoseController } from "./ReadyPoseController";
import {
  canEnterExplore,
  canUsePlanPlayback,
  planOverlayVisibility,
  shouldEnableExploreTools,
  shouldPauseScene,
} from "./exploreMode";
import {
  aggregateToTaskBars,
  mergeResetBars,
  prettyStepLabel,
  projectTaskAfterBars,
  projectTaskMoveBars,
  toGanttBars,
  type TaskAfterProjectionEdit,
} from "./plan/ganttModel";
import { previewBars, type PreviewMeta } from "./plan/previewSchedule";
import { usePlanCompile } from "./plan/usePlanCompile";
import {
  buildProtectedSet,
  ensureStepIds,
  moveTask,
  setStepAfter,
  setStepAt,
  setStepStandoff,
  setStepViaPoints,
  taskStepBounds,
  type PlanEditDelta,
} from "./plan/planEdits";
import { SAMPLE_PLAN } from "./plan/samplePlan";
import { commitTurnResult, type CommitTrigger, type HistoryNode } from "./plan/turnState";
import {
  captureManualEditBaseline,
  restoreManualEditBaseline,
  type ManualEditBaseline,
} from "./plan/manualEditBaseline";
import {
  EMPTY_VERSION_HISTORY,
  appendPlanVersion,
  checkoutPlanVersion,
  snapshotForVersion,
  versionDisplayLabel,
  type PlanVersionSource,
} from "./plan/versionHistory";
import { createEditTailScheduler, type EditTailScheduler } from "./plan/editTail";
import { type AugmentedAction } from "./plan/authorPlan";
import type { AuthoredPlan, CompileResponse, RobotName } from "./plan/planTypes";
import { matchesStudyTarget } from "./plan/studyTargetMatch";
import { formatStudyPlanSummary } from "./plan/studyPlanSummary";
import {
  listStudyCheckpoints,
  loadStudyCheckpoint,
  saveStudyCheckpoint,
  type SavedCheckpointSummary,
} from "./plan/studyCheckpoints";
import type {
  ConversationMessage,
  IntentHint,
  TurnOutcome,
  UserMessageDisplayPart,
} from "./conversation/conversationTypes";
import { streamConversationTurn, type StreamEvent } from "./conversation/conversationStreamClient";
import { applySyncProgress, createSyncChatMessages } from "./conversation/syncChat";
import { turnResultBubbleContent, turnResultDetails } from "./conversation/turnResultPresentation";
import { AssistantMessage } from "./conversation/AssistantMessage";
import { UserMessage } from "./conversation/UserMessage";
import {
  assignPinHandles,
  buildBodyIndex,
  refFromPick,
  refLabel,
  refText,
  type SceneContextRef,
  type ScenePositionRef,
} from "./conversation/sceneContext";
import {
  assignPlanTaskHandles,
  planTaskRefFromAction,
  type PlanTaskRef,
} from "./conversation/planContext";
import { RefComposer, type ComposerHandle, type ComposerPart } from "./conversation/RefComposer";
import { ScenePickController, type ScenePick } from "./ScenePickController";
import { ScenePathPicker } from "./ScenePathPicker";
import { loadSceneManifest } from "./authoring/grounding";
import type { SceneManifest } from "./authoring/types";
import { resetCameraToScenePresentation, scenePresentationFor } from "./scenePresentation";
import { robotIdsFromManifest } from "./robotRegistry";

const PLAN_SUBTITLES_STORAGE_KEY = "mujoco-plan-task-subtitles";
const SHOW_SCENE_OBJECT_LABELS = import.meta.env.VITE_SHOW_SCENE_OBJECT_LABELS !== "false";
const EXPLORE_ENABLED = import.meta.env.VITE_ENABLE_EXPLORE !== "false";
const SHOW_READ_ONLY_STANDOFFS = import.meta.env.VITE_SHOW_READ_ONLY_STANDOFFS === "true";
const CHECKPOINT_SAVE_ENABLED = import.meta.env.VITE_ENABLE_CHECKPOINT_SAVE === "true";
const CHECKPOINT_LOAD_ENABLED = import.meta.env.VITE_ENABLE_CHECKPOINT_LOAD === "true";
const STUDY_TARGET_LOAD_ENABLED = import.meta.env.VITE_ENABLE_STUDY_TARGET_LOAD === "true";

type StudyCompoundOutputMode = "normal" | "v1" | "v2";

function studyCompoundOutputMode(value: string | undefined): StudyCompoundOutputMode {
  if (value === "v2") return "v2";
  if (value === "true" || value === "v1") return "v1";
  return "normal";
}

const STUDY_COMPOUND_OUTPUT_MODE = studyCompoundOutputMode(
  import.meta.env.VITE_STUDY_SIMPLE_COMPOUND_OUTPUT,
);

function initialPlanSubtitlesEnabled(): boolean {
  try {
    return window.localStorage.getItem(PLAN_SUBTITLES_STORAGE_KEY) !== "false";
  } catch {
    return true;
  }
}

function applyInitialSceneState(api: MujocoSimAPI) {
  api.reset();
  if (api.getKeyframeNames().includes(initialStateKeyframe)) {
    api.applyKeyframe(initialStateKeyframe);
  }
}

function isTopologyRejected(details: unknown): boolean {
  if (!details || typeof details !== "object") return false;
  const code = (details as { code?: unknown }).code;
  return code === "dependency_cycle"
    || code === "container_serialization_cycle"
    || code === "container_serialization_incomplete";
}

type VersionCommitInfo = {
  source: PlanVersionSource;
  title: string;
  messages: ConversationMessage[];
  lastResolverReport: unknown | null;
};

type StudyTarget = {
  id: string;
  plan: AuthoredPlan;
  actions: AugmentedAction[];
};

function patchedMessages(
  messages: ConversationMessage[],
  id: string,
  patch: Partial<ConversationMessage>,
): ConversationMessage[] {
  return messages.map((message) => (message.id === id ? { ...message, ...patch } : message));
}

function compactVersionTitle(text: string, fallback: string): string {
  const normalized = text.replace(/\s+/g, " ").trim();
  if (!normalized) return fallback;
  return normalized.length > 44 ? `${normalized.slice(0, 41)}…` : normalized;
}

function studyTurnResultContent(
  outcome: Extract<TurnOutcome, { kind: "turn_result" }>,
  fallbackPlan: AuthoredPlan | null,
  fallbackActions: AugmentedAction[],
): string {
  if (STUDY_COMPOUND_OUTPUT_MODE === "v2") {
    const plan = outcome.plan ?? fallbackPlan;
    if (plan) {
      const summary = formatStudyPlanSummary({
        plan,
        actions: outcome.actions ?? fallbackActions,
      });
      return outcome.resolveWarning ? `${summary}\n\n${outcome.resolveWarning}` : summary;
    }
  }
  return turnResultBubbleContent(outcome, STUDY_COMPOUND_OUTPUT_MODE === "v1");
}

export default function ScenePage() {
  const [sceneConfig, setSceneConfig] = useState<SceneConfig>(defaultSceneConfig);
  const [sceneInput, setSceneInput] = useState(defaultScenePath);
  const [sceneStatus, setSceneStatus] = useState("Select or type a scene");
  const [sceneConfirmed, setSceneConfirmed] = useState(false);
  const [selectedBodyId, setSelectedBodyId] = useState<number | null>(null);
  const [simReady, setSimReady] = useState(false);
  const [readyPoseRevision, setReadyPoseRevision] = useState(0);
  const apiRef = useRef<MujocoSimAPI | null>(null);
  const orbitControlsRef = useRef<ComponentRef<typeof OrbitControls> | null>(null);
  const scenePresentation = useMemo(
    () => scenePresentationFor(sceneConfig.sceneFile),
    [sceneConfig.sceneFile],
  );

  // The Node bridge owns the active scene. Populate the launcher from that
  // source instead of assuming the front-end fallback (layout042) is current.
  useEffect(() => {
    let cancelled = false;
    fetch(`${backendUrl}/api/scene/current?t=${Date.now()}`)
      .then((response) => {
        if (!response.ok) {
          throw new Error(`scene session fetch failed: ${response.status}`);
        }
        return response.json() as Promise<SceneSession>;
      })
      .then((session) => {
        if (cancelled || !session.ok) return;
        setSceneInput(session.sceneFile);
        setSceneConfig({ src: session.src, sceneFile: session.sceneFile });
        setSceneStatus("Select or type a scene");
      })
      .catch((error) => {
        if (!cancelled) {
          setSceneStatus(
            `using fallback scene; current-scene lookup failed: ${
              error instanceof Error ? error.message : String(error)
            }`,
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const applyAndCaptureInitialSceneState = (api: MujocoSimAPI) => {
    applyInitialSceneState(api);
    setReadyPoseRevision((revision) => revision + 1);
  };

  // --- plan state (D1, compound_turn_integration_spec.md): the frontend
  // never holds "the authored plan" — decompose() is backend-only and
  // reruns from `semanticActions` every turn. What's left on the frontend is
  // exactly: `livePlan` (the only user-visible plan — every turn_result
  // replaces it directly), `draftPlan` (a purely local, OPTIMISTIC preview
  // for manual Gantt/scene edits), `edits` (the delta list those edits append
  // to, D2 — the backend replays it and derives the pin set from it), and
  // committed version history (the original one-click revert, generalized).
  //
  // `draftPlan` is never a committable state. Since S3 there is no Compile
  // press at all: an edit appends a delta and updates `draftPlan` for instant
  // feedback, but arms NO tail (item 17, revised) — a tail run is several
  // full verification compiles, so the timing of when to pay for one is the
  // user's to choose, via the sync button, not an auto-fired timer. Sync's
  // `turn_result` replaces `livePlan` with a verified, resolved plan.
  // `draftPlan` covers the gap between the gesture and that reply.
  const [livePlan, setLivePlan] = useState<AuthoredPlan | null>(null);
  const [draftPlan, setDraftPlan] = useState<AuthoredPlan | null>(null);
  // Spatial edits affect generated tracks but not task/schedule structure. They
  // may replay the last successful compile as an explicitly stale preview;
  // allocation, ordering, and semantic changes must recompile before playback.
  const [draftEditImpact, setDraftEditImpact] = useState<"none" | "spatial" | "structural">("none");

  // Monotone revision (integration spec §3): bumped on every commit AND every
  // local edit. Deliberately a ref, not state — nothing renders it, and
  // in-flight closures (applyOutcome, captured at send-time) must read the
  // CURRENT value to detect a stale artifact, which is exactly what state
  // would not give them. Making it state would also re-render the scene and
  // Gantt on every drag for no visible change.
  const baseRevisionRef = useRef(0);
  const bumpBaseRevision = () => {
    baseRevisionRef.current += 1;
  };
  // Session-scoped version tree. Each successful compound commit appends a
  // full snapshot below the currently displayed version. Checking out an old
  // version moves `currentId`; the next commit branches without deleting the
  // old future. Persistence across a page reload is deliberately out of scope.
  const [versionHistory, setVersionHistory] = useState(EMPTY_VERSION_HISTORY);
  const [savedCheckpoints, setSavedCheckpoints] = useState<SavedCheckpointSummary[]>([]);
  const [checkpointSaving, setCheckpointSaving] = useState(false);
  const [checkpointActivity, setCheckpointActivity] = useState<string | null>(null);
  const [studyTarget, setStudyTarget] = useState<StudyTarget | null>(null);
  const [targetLoading, setTargetLoading] = useState(false);
  const [targetError, setTargetError] = useState<string | null>(null);

  const refreshSavedCheckpoints = useCallback(async () => {
    if (!CHECKPOINT_LOAD_ENABLED && !STUDY_TARGET_LOAD_ENABLED) return;
    try {
      setSavedCheckpoints(await listStudyCheckpoints());
    } catch (error) {
      setCheckpointActivity(error instanceof Error ? error.message : String(error));
    }
  }, []);

  useEffect(() => {
    void refreshSavedCheckpoints();
  }, [refreshSavedCheckpoints, sceneConfig.sceneFile]);

  useEffect(() => {
    setStudyTarget(null);
    setTargetLoading(false);
    setTargetError(null);
  }, [sceneConfig.sceneFile]);

  const sceneCheckpoints = savedCheckpoints.filter(
    (checkpoint) => checkpoint.sceneFile === sceneConfig.sceneFile,
  );

  // D2b (revises S3/D2's item 16-17 "full replay ledger"): the ordered
  // manual-edit deltas made since the last commit and NOT YET applied
  // anywhere. A ref, not state -- several edits in a row must see the
  // immediately-preceding push synchronously, which a `useState` update
  // cannot guarantee (its setter is batched/async).
  //
  // This used to be two things: `editLedgerRef` (a ledger that accumulated
  // every delta forever, replayed onto every future turn's fresh
  // `decompose(actions)`) and `pendingEditCount` (a display-only counter of
  // edits since the last commit). D2's ledger existed only because every
  // turn rebuilt the plan from scratch; D2b bakes a delta into the plan the
  // moment it is applied, so there is nothing left to replay next turn --
  // one list, meaning literally "pending, not yet sent-and-consumed",
  // collapses both. `pendingEditCount` survives as a plain derived number
  // (`pendingEditsRef.current.length`, mirrored into state so the Gantt
  // toolbar re-renders on each push) purely for UI reactivity — see
  // `commitOutcome` for how the list actually shrinks.
  const pendingEditsRef = useRef<PlanEditDelta[]>([]);
  const [pendingEditCount, setPendingEditCount] = useState(0);
  // The history node captured when the CURRENTLY OPEN edit episode began, or
  // null when no episode is open (item 18: an episode is "open" between the
  // first edit-triggered commit and the next chat turn or sync press).
  const editEpisodeRef = useRef<HistoryNode | null>(null);
  // Checkpoint from immediately before the first UNSYNCED manual edit on the
  // current authored plan. A successful Sync is a commit boundary: the
  // checkpoint is cleared so Undo edits can never remove a committed version.
  const manualEditBaselineRef = useRef<ManualEditBaseline | null>(null);
  const [editTailStatus, setEditTailStatus] = useState<"idle" | "running">("idle");
  const [editActivity, setEditActivity] = useState<string | null>(null);
  // The run-now/abort mechanics live in editTail.ts (unit-tested there,
  // independent of the whole scene mounting) — `runSyncTailRef` exists only
  // so the scheduler (created once, below) always calls the LATEST render's
  // `runSyncTail` closure rather than the one captured at construction time
  // (which would close over stale `livePlan`/`edits`).
  const runSyncTailRef = useRef<(signal: AbortSignal) => Promise<void>>(async () => {});
  const editTailSchedulerRef = useRef<EditTailScheduler | null>(null);
  if (!editTailSchedulerRef.current) {
    editTailSchedulerRef.current = createEditTailScheduler((signal) => runSyncTailRef.current(signal));
  }

  const base = usePlanCompile(livePlan);
  const { items, schedule, warnings, conflicts, completed, status, error, epoch, compiledPlan, compileId } = base;
  const draftPlanChanged = draftPlan !== livePlan;
  const dirty = draftPlanChanged || pendingEditCount > 0;
  const hasPlan = draftPlan !== null;
  // The compiled results correspond to the live plan only once the compile
  // that consumed it has landed. Until then (dirty OR mid-compile) keep showing
  // the local preview — otherwise the Gantt flashes a stale layout between the
  // edit gesture and the edit tail's verified reply.
  const inSync = compiledPlan === livePlan;

  // --- unified conversation turn (Phase 1, non-streaming): NL -> one turn ->
  // Author or Resolver via runConversationTurn -> terminal outcome applied.
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [semanticActions, setSemanticActions] = useState<AugmentedAction[]>([]);
  const targetMatched = useMemo(
    () => studyTarget !== null
      && livePlan !== null
      && !dirty
      && matchesStudyTarget(
        { plan: livePlan, actions: semanticActions },
        { plan: studyTarget.plan, actions: studyTarget.actions },
      ),
    [dirty, livePlan, semanticActions, studyTarget],
  );
  // Inline Cursor-style scene references attached to the next turn: an object
  // (double-click a body) is an object ref (highlight only); a surface/floor
  // point is a position ref (pin marker). Ordered; each has a stable id linking
  // to an inline token in the composer.
  const [contextRefs, setContextRefs] = useState<SceneContextRef[]>([]);
  // Semantic plan-task references are deliberately separate from scene refs:
  // their stable anchor is an AugmentedAction id, not a decomposed step id.
  const [planRefs, setPlanRefs] = useState<PlanTaskRef[]>([]);
  // Reference-pick mode. OFF by default so plain double-click keeps the scene's
  // native select/deselect + drag; turning it ON routes double-clicks to
  // reference capture (avoids clashing with the native double-click gesture).
  const [pickMode, setPickMode] = useState(false);
  const [planRefMode, setPlanRefMode] = useState(false);
  const composerRef = useRef<ComposerHandle | null>(null);
  const [manifest, setManifest] = useState<SceneManifest | null>(null);
  const robotIds = useMemo(() => robotIdsFromManifest(manifest), [manifest]);
  const bodyIndex = useMemo(
    () => (manifest ? buildBodyIndex(manifest) : new Map()),
    [manifest],
  );
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const activeTurnRef = useRef<string | null>(null);
  const [versionSwitching, setVersionSwitching] = useState(false);
  const turnBusy = activeTurnId !== null || versionSwitching;
  const [chatError, setChatError] = useState<string | null>(null);
  // Phase 4: last resolver report, retained for Explain context (threaded
  // into the unified stream body's plan_state.last_resolver_report).
  const [lastResolverReport, setLastResolverReport] = useState<unknown | null>(null);
  const chatBodyRef = useRef<HTMLDivElement | null>(null);
  const followLatestChatRef = useRef(true);

  useEffect(() => {
    const body = chatBodyRef.current;
    if (!body || !followLatestChatRef.current) return;
    body.scrollTop = body.scrollHeight;
  }, [messages, turnBusy, chatError]);

  // Scene manifest (object/facility names + bodies) for attributing picks.
  useEffect(() => {
    if (!sceneConfirmed) return;
    let cancelled = false;
    setManifest(null);
    loadSceneManifest()
      .then((m) => {
        if (!cancelled) setManifest(m);
      })
      .catch(() => {
        /* manifest unavailable → picks fall back to raw positions */
      });
    return () => {
      cancelled = true;
    };
  }, [sceneConfirmed, sceneConfig.sceneFile]);

  const [schedulePlaying, setSchedulePlaying] = useState(false);
  const [scheduleSpeed, setScheduleSpeed] = useState(1);
  const [scheduleTime, setScheduleTime] = useState(0);
  const [planSubtitlesEnabled, setPlanSubtitlesEnabled] = useState(initialPlanSubtitlesEnabled);
  useEffect(() => {
    try {
      window.localStorage.setItem(PLAN_SUBTITLES_STORAGE_KEY, String(planSubtitlesEnabled));
    } catch {
      // Storage can be unavailable in private/locked-down browser contexts.
    }
  }, [planSubtitlesEnabled]);
  // A compiled plan normally owns qpos through SchedulePlayer. Explore is an
  // explicit temporary hand-off to live physics/dragging; it never discards
  // the plan or its compiled result.
  const [exploreMode, setExploreMode] = useState(false);
  const schedulePlayerRef = useRef<SchedulePlayerHandle | null>(null);
  const [resetNonce, setResetNonce] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"task" | "step">("task");
  const [markerDragging, setMarkerDragging] = useState(false);

  // Per-step meta from the last compile — reused by the local preview so a draft
  // edit re-lays out instantly without a round-trip.
  const metaById = useMemo(() => {
    const m = new Map<string, PreviewMeta>();
    for (const e of schedule) {
      m.set(e.id, {
        duration: e.duration,
        label: prettyStepLabel({ op: e.op, facility: e.facility, repairKind: e.repair_kind }),
        facility: e.facility,
        group: e.group,
      });
    }
    return m;
  }, [schedule]);

  // Bars: local preview while the compiled result doesn't match the draft
  // (never-compiled plan, uncompiled edit, or a compile still in flight), else
  // the compiled schedule. A never-compiled plan has no durations, so previewBars
  // falls back to nominal-width bars — a structural placeholder shown only
  // until the edit tail's verified schedule lands.
  // A spatial draft can replay the preceding compile. Keep its Gantt on that
  // compiled schedule too, so the playhead never appears to describe the
  // uncompiled route/drop-point edit.
  const showPreview = !!draftPlan
    && (!inSync || (draftPlanChanged && draftEditImpact === "structural"));
  // Exact set of step ids in any conflict — drives per-step bar tinting.
  const warnedStepIds = useMemo(
    () => new Set(conflicts.flatMap((c) => c.steps)),
    [conflicts],
  );
  const delegableConflictCount = useMemo(
    () => conflicts.filter((c) => c.kind === "path" || c.kind === "facility").length,
    [conflicts],
  );
  // The sync button's "no pending edits" branch must stay reachable whenever
  // the resolver deferred (compound_turn_integration_spec.md item 19, D3b) --
  // that's the button's other documented job, not just "no delegable
  // conflicts left". `lastResolverReport` is `unknown` (Phase 4 kept it
  // opaque; ScenePage never needed to inspect it before now), so this reads
  // the one field it needs defensively rather than importing a full report type.
  const resolverNotConverged =
    !!lastResolverReport &&
    typeof lastResolverReport === "object" &&
    (lastResolverReport as { converged?: unknown }).converged === false;
  const actionsById = useMemo(
    () => new Map(semanticActions.map((a) => [a.id, a])),
    [semanticActions],
  );
  const pendingMoveEdits = (() => {
    const robotByAction = new Map(
      (livePlan?.tasks ?? []).map((task) => [task.task, task.robot]),
    );
    return pendingEditsRef.current
      .filter((edit): edit is Extract<PlanEditDelta, { op: "move_task" }> => edit.op === "move_task")
      .map((edit) => {
        const sourceRobot = robotByAction.get(edit.target.actionId) ?? edit.robot;
        robotByAction.set(edit.target.actionId, edit.robot);
        return {
          actionId: edit.target.actionId,
          sourceRobot,
          robot: edit.robot,
          afterActionId: edit.afterActionId,
        };
      });
  })();
  // Pending `after` edits, resolved from step ids back up to the semantic tasks
  // the task Gantt draws. The delta is authored at step level, but a task bar is
  // what the user dragged, so the projection has to speak in task ids.
  const pendingAfterEdits = (() => {
    if (viewMode !== "task" || !draftPlan) return [];
    const taskByStep = new Map<string, string>();
    for (const task of draftPlan.tasks) {
      if (!task.task) continue;
      for (const step of task.steps) if (step.id) taskByStep.set(step.id, task.task);
    }
    const projected: TaskAfterProjectionEdit[] = [];
    for (const edit of pendingEditsRef.current) {
      if (edit.op !== "set_step_after") continue;
      const stepId = edit.target.stepId;
      const afterStepId = edit.after[0];
      if (!stepId || !afterStepId) continue;
      const actionId = taskByStep.get(stepId);
      const afterActionId = taskByStep.get(afterStepId);
      if (!actionId || !afterActionId || actionId === afterActionId) continue;
      projected.push({ actionId, afterActionId });
    }
    return projected;
  })();
  const hasPendingDragEdit = pendingEditsRef.current.some(
    (edit) => edit.op === "move_task" || edit.op === "set_step_after",
  );
  const useTaskMoveProjection = viewMode === "task"
    && pendingMoveEdits.length > 0
    && schedule.length > 0;
  const useTaskAfterProjection = viewMode === "task"
    && pendingAfterEdits.length > 0
    && schedule.length > 0;
  const compiledBars = useMemo(
    () => toGanttBars(schedule, warnedStepIds),
    [schedule, warnedStepIds],
  );
  const bars = useMemo(
    () => (showPreview && draftPlan && !(hasPendingDragEdit && schedule.length > 0)
      ? previewBars(draftPlan, metaById)
      : compiledBars),
    [showPreview, draftPlan, hasPendingDragEdit, schedule.length, metaById, compiledBars],
  );
  const shownWarnings = showPreview ? [] : warnings;
  // Reset remains a real scheduled step for playback, dependencies and
  // conflict detection. Step view only folds its tiny visual bar into the
  // preceding operation; task view retains its existing group aggregation.
  const removableTaskIds = useMemo(
    () => new Set(semanticActions.filter((action) => action.op === "move").map((action) => action.id)),
    [semanticActions],
  );
  // Read directly from the ref on every render. Version checkout can replace
  // one pending batch with another of the same length, so count alone is not
  // a safe memoization key for task identity.
  const pendingRemovalIds = new Set(
    pendingEditsRef.current
      .filter((edit): edit is Extract<PlanEditDelta, { op: "remove_task" }> => edit.op === "remove_task")
      .map((edit) => edit.target.actionId),
  );
  const taskBars = aggregateToTaskBars(bars, actionsById);
  const taskMoveProjection = useTaskMoveProjection
    ? projectTaskMoveBars(taskBars, pendingMoveEdits)
    : null;
  const movedTaskBars = taskMoveProjection?.bars ?? taskBars;
  // Moves first, then dependencies: an `after` must delay a task where the
  // pending reassignment actually put it, not where it was compiled.
  const projectedTaskBars = useTaskAfterProjection
    ? projectTaskAfterBars(movedTaskBars, pendingAfterEdits)
    : movedTaskBars;
  const viewBars = viewMode === "task" ? projectedTaskBars : mergeResetBars(bars);
  const ghostBars = taskMoveProjection?.ghostBars ?? [];
  const showDraftProjection = !!taskMoveProjection || useTaskAfterProjection;

  // Escape exits plan-reference picking without affecting normal selection or
  // the independent scene-pin mode.
  useEffect(() => {
    if (!planRefMode) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPlanRefMode(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [planRefMode]);

  // Spatial anchors from the last successful compile, keyed by step id. Persists
  // across a dirty edit (livePlan unchanged) so markers don't blink out
  // while a drag awaits recompile.
  const spatialByStep = useMemo(() => {
    const CORE_OPS = new Set(["navigate", "pick", "place", "reset", "wait"]);
    const m = new Map<string, StepMarker>();
    for (const [robot, steps] of Object.entries(completed)) {
      for (let i = 0; i < steps.length; i++) {
        const s = steps[i];
        if (!s.id) continue;
        // A navigate's standoff is authorable unless it feeds an articulation
        // replay (next step is a non-core skill) — that base pose is baked into
        // the recorded track, so we lock it (matches the backend, which ignores
        // an override there).
        const next = steps[i + 1];
        const standoffEditable = s.op === "navigate" && (!next || CORE_OPS.has(next.op));
        m.set(s.id, {
          id: s.id,
          robot,
          op: s.op,
          isDetour: s.compiler_v2_repair === "detour"
            || s.compiler_v2_repair === "go_away",
          standoff: s.standoff as [number, number] | undefined,
          standoffEditable,
          route: s.route ?? undefined,
          at: s.at as unknown as [number, number, number] | undefined,
        });
      }
    }
    return m;
  }, [completed]);

  // Which steps to visualize: the single selected step in step view, or every
  // step of the selected step's task in task view (req 1 vs req 2).
  const highlightIds = useMemo(() => {
    if (!selectedStepId || !draftPlan) return [] as string[];
    if (viewMode === "step") return [selectedStepId];
    const selectedTaskBar = viewBars.find((bar) => bar.key === selectedStepId);
    if (selectedTaskBar?.memberKeys?.length) {
      return selectedTaskBar.memberKeys;
    }
    for (const task of draftPlan.tasks) {
      if (task.steps.some((s) => s.id === selectedStepId)) {
        return task.steps.map((s) => s.id).filter((id): id is string => !!id);
      }
    }
    return [selectedStepId];
  }, [selectedStepId, viewMode, draftPlan, viewBars]);

  // Markers for the highlighted steps, with any draft override (a pending edit
  // not yet recompiled) taking precedence over the compiled anchors: `at` for
  // the place point, and authored `via_points` spliced into the backend route
  // endpoints are kept from the compiled polyline and the draft middle spliced
  // in) so a drag/insert/delete shows immediately.
  const markers = useMemo(() => {
    const draftAt = new Map<string, [number, number]>();
    const draftVia = new Map<string, [number, number][]>();
    const draftStandoff = new Map<string, [number, number]>();
    if (draftPlan) {
      for (const task of draftPlan.tasks) {
        for (const s of task.steps) {
          if (!s.id) continue;
          if (Array.isArray(s.at)) draftAt.set(s.id, s.at as [number, number]);
          // Only a NON-EMPTY via_points list is an authored constraint. An empty
          // list is not an author edit (deleting the last waypoint commits null,
          // not []) — it only appears because a compiled/resolved step carries
          // via_points:[]. Treating [] as a constraint would splice the route
          // down to its two endpoints and hide the compiler's interior nodes,
          // making a resolved path's waypoints uneditable. Fall back to the
          // compiled route instead.
          if (Array.isArray(s.via_points) && s.via_points.length > 0) {
            draftVia.set(s.id, s.via_points);
          }
          if (Array.isArray(s.standoff)) draftStandoff.set(s.id, s.standoff as [number, number]);
        }
      }
    }
    const out: StepMarker[] = [];
    for (const id of highlightIds) {
      const sp = spatialByStep.get(id);
      if (!sp) continue;
      let marker: StepMarker = sp;
      const at = draftAt.get(id);
      if (at && sp.at) marker = { ...marker, at: [at[0], at[1], sp.at[2]] };
      // The backend's route endpoint is the chassis centre, while `standoff`
      // remains the manipulation mount coordinate. Map only the draft delta
      // across those frames so the draggable chassis endpoint previews locally
      // without changing the backend contract.
      const standoff = marker.standoffEditable ? draftStandoff.get(id) : undefined;
      if (standoff) marker = { ...marker, standoff };
      const viaPoints = draftVia.get(id);
      const compiledEnd = sp.route ? sp.route[sp.route.length - 1] : undefined;
      const endS = standoff && sp.standoff && compiledEnd
        ? chassisEndpointForMountDraft(compiledEnd, sp.standoff, standoff)
        : compiledEnd;
      if (sp.route && sp.route.length >= 2 && (viaPoints || standoff)) {
        marker = {
          ...marker,
          route: [sp.route[0], ...(viaPoints ?? sp.route.slice(1, -1)), endS!],
        };
      }
      out.push(marker);
    }
    return out;
  }, [highlightIds, spatialByStep, draftPlan]);

  // S3 item 17 (revised): the sync button replaced the Compile-press gate, so
  // a dirty draft is no longer "waiting for the user" in the old sense — it
  // is either staged and waiting for a sync press, or already syncing
  // (`editTailStatus === "running"`, in which case the tail's own compact
  // activity line — `editActivity` — is more informative than a generic
  // "updating…"). Unlike the auto-tail this replaced, a dirty draft can sit
  // indefinitely; the count is what tells the user it's still theirs to apply.
  const statusText = checkpointActivity ?? (!draftPlan
    ? "no plan loaded"
    : editTailStatus === "running"
      ? (editActivity ?? "syncing…")
      : dirty
        ? `edited — press sync to apply (${pendingEditCount})`
        : status === "compiling"
          ? "compiling…"
          : status === "error"
            ? error ?? "compile failed"
            : undefined);

  const exploreAvailable = canEnterExplore({
    hasPlan,
    simReady,
  });
  const playbackAvailable = canUsePlanPlayback({
    hasPlan,
    inSync,
    dirty,
    editImpact: draftEditImpact === "spatial" ? "spatial" : "structural",
    itemCount: items.length,
    status,
  });
  const exploreToolsEnabled = shouldEnableExploreTools(hasPlan, exploreMode);

  // Clearing the plan or replacing the scene ends the explicit Explore mode.
  // Draft edits and recompiles do not: direct manipulation is independent of
  // whether a playable compiled schedule exists.
  useEffect(() => {
    if (exploreMode && !exploreAvailable) {
      setSchedulePlaying(false);
      setMarkerDragging(false);
      setExploreMode(false);
    }
  }, [exploreAvailable, exploreMode]);

  // Stage a fresh plan as the draft, resetting playback + scene state. When
  // `compile` is true the draft is also committed so it compiles immediately (a
  // sample plan should just appear); when false it stays a draft the user
  // reviews as a structural preview and compiles on demand. Item 20: with
  // author turns now landing via `applyOutcome`'s turn_result branch (never
  // here), the only caller left is the sample-plan seed.
  const stageSeededPlan = (seed: AuthoredPlan, { compile }: { compile: boolean }) => {
    if (exploreMode) returnToPlan();
    else setSchedulePlaying(false);
    setScheduleTime(0);
    setSelectedStepId(null);
    const api = apiRef.current;
    if (api) applyAndCaptureInitialSceneState(api);
    bumpBaseRevision();
    manualEditBaselineRef.current = null;
    setDraftPlan(seed);
    setDraftEditImpact(compile ? "none" : "structural");
    if (compile) setLivePlan(seed);
  };

  const stagePlan = (plan: AuthoredPlan, options: { compile: boolean }) => {
    stageSeededPlan(ensureStepIds(plan), options);
  };

  const loadSamplePlan = () => stagePlan(SAMPLE_PLAN, { compile: true });

  const clearPlan = () => {
    cancelPendingEditTail();
    editEpisodeRef.current = null;
    manualEditBaselineRef.current = null;
    pendingEditsRef.current = [];
    setPendingEditCount(0);
    setEditTailStatus("idle");
    setEditActivity(null);
    setDraftPlan(null);
    setLivePlan(null);
    setDraftEditImpact("none");
    setSchedulePlaying(false);
    setExploreMode(false);
    setScheduleTime(0);
    setSelectedStepId(null);
    baseRevisionRef.current = 0;
    setVersionHistory(EMPTY_VERSION_HISTORY);
    setMessages([]);
    setSemanticActions([]);
    setStudyTarget(null);
    setTargetLoading(false);
    setTargetError(null);
    setChatError(null);
    setLastResolverReport(null);
    setContextRefs([]);
    composerRef.current?.clear();
    followLatestChatRef.current = true;
    const api = apiRef.current;
    if (api) applyAndCaptureInitialSceneState(api);
  };

  const finalizeMessage = (id: string, patch: Partial<ConversationMessage>) =>
    setMessages((cur) => cur.map((m) => (m.id === id ? { ...m, ...patch } : m)));

  // Adopt a turn_result's verified plan+compile as the new `livePlan` — the
  // ONLY user-visible plan (D1). `adoptCompileResult` is the existing
  // "loadScheduleTracks + adopt without reposting" mechanism (previously used
  // by the resolve overlay; reused unchanged here per item 15) — it fills in
  // usePlanCompile's own state directly, so the subsequent `setLivePlan(seed)`
  // below sees `adoptedPlanRef.current === seed` and skips a redundant
  // /compile_plan POST. `draftPlan` is reseeded too: it is the substrate
  // manual edits work from, and there is no other place a fresh one comes
  // from now that authoring never stages a separate draft (item 15).
  const adoptLivePlan = async (plan: AuthoredPlan, compile: CompileResponse) => {
    const seed = ensureStepIds(plan);
    setSchedulePlaying(false);
    setScheduleTime(0);
    setSelectedStepId(null);
    const api = apiRef.current;
    if (api) applyAndCaptureInitialSceneState(api);
    await base.adoptCompileResult(seed, compile);
    setLivePlan(seed);
    setDraftPlan(seed);
    setDraftEditImpact("none");
  };

  // The CompileResponse currently backing `livePlan`, reconstructed from
  // `base` — valid only while `base` is in sync with `livePlan` (true right
  // after any turn_result adoption). This is what a history node snapshots
  // (item 18): it must reflect whichever compile is ACTUALLY paired with the
  // current `livePlan`, regardless of whether that pairing came from a chat
  // turn or an edit-tail run.
  const liveCompileSnapshot = (): CompileResponse | null =>
    livePlan && compiledPlan === livePlan
      ? { schedule, warnings, conflicts, completed, compile_id: compileId ?? undefined }
      : null;

  const saveCurrentCheckpoint = async () => {
    const compile = liveCompileSnapshot();
    if (!livePlan || !compile || dirty || checkpointSaving) return;
    setCheckpointSaving(true);
    setCheckpointActivity("Saving checkpoint…");
    try {
      const saved = await saveStudyCheckpoint({
        plan: livePlan,
        compile,
        semanticActions,
      });
      setCheckpointActivity(`Saved checkpoint ${saved.id}`);
      await refreshSavedCheckpoints();
    } catch (error) {
      setCheckpointActivity(`Checkpoint save failed: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setCheckpointSaving(false);
    }
  };

  const loadSavedCheckpoint = async (checkpointId: string) => {
    if (turnBusy) return;
    if (
      pendingEditsRef.current.length > 0
      && !window.confirm("Load this checkpoint and discard the current unsynced edits?")
    ) {
      return;
    }
    setVersionSwitching(true);
    setCheckpointActivity(`Loading checkpoint ${checkpointId}…`);
    try {
      const checkpoint = await loadStudyCheckpoint(checkpointId);
      setStudyTarget(null);
      setTargetLoading(false);
      setTargetError(null);
      cancelPendingEditTail();
      editEpisodeRef.current = null;
      manualEditBaselineRef.current = null;
      bumpBaseRevision();
      await adoptLivePlan(checkpoint.snapshot.plan, checkpoint.snapshot.compile);
      pendingEditsRef.current = [];
      setPendingEditCount(0);
      setSemanticActions(checkpoint.snapshot.semanticActions);
      setMessages([]);
      setLastResolverReport(null);
      setChatError(null);
      setContextRefs([]);
      setPlanRefs([]);
      setSelectedStepId(null);
      composerRef.current?.clear();
      followLatestChatRef.current = true;
      setVersionHistory(
        appendPlanVersion(EMPTY_VERSION_HISTORY, {
          id: crypto.randomUUID(),
          createdAt: Date.now(),
          source: "sync",
          title: checkpoint.id,
          snapshot: {
            plan: checkpoint.snapshot.plan,
            compile: checkpoint.snapshot.compile,
            semanticActions: checkpoint.snapshot.semanticActions,
            edits: [],
            messages: [],
            lastResolverReport: null,
          },
        }),
      );
      setCheckpointActivity(`Loaded checkpoint ${checkpoint.id} as V1`);
    } catch (error) {
      setCheckpointActivity(`Checkpoint load failed: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setVersionSwitching(false);
    }
  };

  const loadTargetCheckpoint = async (checkpointId: string) => {
    if (!checkpointId || turnBusy || targetLoading) return;
    setTargetLoading(true);
    setTargetError(null);
    try {
      const checkpoint = await loadStudyCheckpoint(checkpointId);
      setStudyTarget({
        id: checkpoint.id,
        plan: checkpoint.snapshot.plan,
        actions: checkpoint.snapshot.semanticActions,
      });
    } catch (error) {
      setTargetError(
        `Target load failed: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      setTargetLoading(false);
    }
  };

  const ensureManualEditBaseline = () => {
    if (manualEditBaselineRef.current || !livePlan) return;
    manualEditBaselineRef.current = captureManualEditBaseline({
      plan: livePlan,
      compile: liveCompileSnapshot(),
      semanticActions,
      messages,
      lastResolverReport,
      versionHistory,
    });
  };

  // Shared commit path for every `turn_result` — chat turns and the sync
  // button (both its pending-batch and plain-re-resolve branches) all fund
  // through here so item 18's episode bookkeeping (`editEpisodeRef`) and
  // D2b's pending-edit clearing live in exactly one place. Returns whether
  // the artifact was applied (false ⇒ a stale `base_revision`, discarded per
  // integration spec §3).
  //
  // `sentEdits` is the batch that went out with THIS turn's request — read
  // by the caller synchronously at request-issue time, before its `await` —
  // never `pendingEditsRef.current` read here (which is AFTER the await, and
  // may already hold a NEWER edit made while this turn was in flight; see
  // `commitTurnResult`'s `sentEdits` doc comment for why the two must not be
  // conflated).
  const commitOutcome = async (
    outcome: Extract<TurnOutcome, { kind: "turn_result" }>,
    trigger: CommitTrigger,
    sentEdits: PlanEditDelta[],
    versionInfo: VersionCommitInfo,
  ): Promise<boolean> => {
    const commit = commitTurnResult(
      {
        baseRevision: baseRevisionRef.current,
        semanticActions,
        plan: livePlan,
        compile: liveCompileSnapshot(),
        edits: pendingEditsRef.current,
      },
      {
        plan: outcome.plan,
        compile: outcome.compile,
        actions: outcome.actions,
        baseRevision: outcome.baseRevision,
      },
      { trigger, sentEdits, openEpisode: editEpisodeRef.current },
    );
    if (!commit.applied) return false;
    // Every successful commit establishes a new plan/version baseline. Undo
    // edits is only for local unsynced changes; keeping this checkpoint after
    // an Edit Sync would restore the old versionHistory snapshot and silently
    // remove the version that was just committed.
    manualEditBaselineRef.current = null;
    baseRevisionRef.current = commit.state.baseRevision;
    editEpisodeRef.current = commit.episode;
    pendingEditsRef.current = commit.state.edits;
    // D2b: every delta in `sentEdits` was consumed one way or another this
    // turn (baked into the plan, or dropped with a visible warning the
    // stream already surfaced) — none of it is "pending" any more. Anything
    // left in `commit.state.edits` is strictly newer (pushed mid-flight), so
    // the badge reflects that, not zero.
    setPendingEditCount(pendingEditsRef.current.length);
    setSemanticActions(commit.state.semanticActions);
    if (outcome.plan && outcome.compile) {
      await adoptLivePlan(outcome.plan, outcome.compile);
    }
    if (commit.state.plan && commit.state.compile) {
      setVersionHistory((history) =>
        appendPlanVersion(history, {
          id: crypto.randomUUID(),
          createdAt: Date.now(),
          source: versionInfo.source,
          title: versionInfo.title,
          snapshot: {
            plan: commit.state.plan!,
            compile: commit.state.compile!,
            semanticActions: commit.state.semanticActions,
            edits: commit.state.edits,
            messages: versionInfo.messages,
            lastResolverReport: versionInfo.lastResolverReport,
          },
        }),
      );
    }
    return true;
  };

  // Terminal artifact application — the ONLY place plan state is mutated for
  // a CHAT turn. Per D5/§7 a turn_result commits automatically: there is no
  // more draft/review gate to pass through (item 15 deletes it), and this
  // runs whether or not the resolver converged (item 18).
  const applyOutcome = async (
    assistantId: string,
    outcome: TurnOutcome,
    sentEdits: PlanEditDelta[],
    versionMessages: ConversationMessage[],
    versionTitle: string,
  ) => {
    if (outcome.kind === "turn_result") {
      // A chat turn is a visible user action that always closes any open
      // edit episode (item 18), whether or not this particular reply lands.
      const finalMessagePatch: Partial<ConversationMessage> = {
        status: "done",
        source: outcome.actions !== undefined ? "authoring" : "resolver",
        content: studyTurnResultContent(outcome, livePlan, semanticActions),
        details: turnResultDetails(outcome),
        excludeFromModel: STUDY_COMPOUND_OUTPUT_MODE === "v2",
      };
      const applied = await commitOutcome(outcome, "chat", sentEdits, {
        source: "chat",
        title: compactVersionTitle(versionTitle, "Chat update"),
        messages: patchedMessages(versionMessages, assistantId, finalMessagePatch),
        lastResolverReport: outcome.report ?? null,
      });
      if (!applied) {
        // Stale base_revision (integration spec §3): a local edit/commit
        // already moved the live state on since this turn was sent. Discard
        // the reply verbatim rather than apply it.
        finalizeMessage(assistantId, {
          status: "done",
          content: `${turnResultBubbleContent(outcome)}\n\n(Discarded — a newer change already applied.)`,
        });
        return;
      }
      setLastResolverReport(outcome.report ?? null);
      // Absent-actions means this turn skipped Author; the source tag keeps
      // that distinction in both the visible chat and the version snapshot.
      finalizeMessage(assistantId, finalMessagePatch);
    } else if (outcome.kind === "no_change_result") {
      // This response was verified against the revision sent with the turn,
      // but intentionally carries no replacement artifact.  Keep the live
      // plan, compile, revision, and history exactly as they are.
      if (
        outcome.baseRevision !== undefined
        && outcome.baseRevision !== baseRevisionRef.current
      ) {
        finalizeMessage(assistantId, {
          status: "done",
          source: "authoring",
          content: `${outcome.message}\n\n(Discarded — a newer change already applied.)`,
        });
        return;
      }
      editEpisodeRef.current = null;
      finalizeMessage(assistantId, {
        status: "done",
        source: "authoring",
        content: outcome.authorMessage ?? outcome.message,
        details: outcome.authoringSummary || undefined,
      });
    } else if (outcome.kind === "answer") {
      finalizeMessage(assistantId, { status: "done", source: "explain", content: outcome.content });
    } else {
      // author_result/resolve_result never reach here in practice —
      // conversationStreamClient throws an actionable error for either
      // before returning (S2 rollback rule) — this is only the generic
      // error path.
      finalizeMessage(assistantId, { status: "error", content: outcome.message });
    }
  };

  const switchPlanVersion = async (versionId: string) => {
    if (versionId === versionHistory.currentId || turnBusy) return;
    if (
      pendingEditsRef.current.length > 0
      && !window.confirm("Switch versions and discard the current unsynced edits?")
    ) {
      return;
    }
    const restored = snapshotForVersion(versionHistory, versionId);
    if (!restored) return;

    // Version checkout is a visible state change: abort stale work and mint a
    // newer runtime revision. The historical revision itself is never reused.
    setVersionSwitching(true);
    cancelPendingEditTail();
    editEpisodeRef.current = null;
    manualEditBaselineRef.current = null;
    bumpBaseRevision();
    pendingEditsRef.current = restored.edits;
    setPendingEditCount(pendingEditsRef.current.length);
    setSemanticActions(restored.semanticActions);
    setMessages(restored.messages);
    setLastResolverReport(restored.lastResolverReport);
    setVersionHistory((history) => checkoutPlanVersion(history, versionId));
    setChatError(null);
    setContextRefs([]);
    setPlanRefs([]);
    composerRef.current?.clear();
    try {
      await adoptLivePlan(restored.plan, restored.compile);
    } finally {
      setVersionSwitching(false);
    }
  };

  // Position refs render as free-standing pin markers (via PlanOverlay).
  const pins = useMemo(
    () =>
      contextRefs
        .filter((r): r is ScenePositionRef => r.kind === "position")
        .map((r) => ({ id: r.id, at: r.xyz })),
    [contextRefs],
  );
  const overlayVisibility = planOverlayVisibility({
    exploreMode,
    markerCount: markers.length,
    pinCount: pins.length,
  });

  // A double-click pick becomes a ref + an inline composer token. Object hits
  // highlight the body (no marker); surface/floor hits drop a pin marker.
  // Stable identity (empty deps): ScenePickController attaches one listener.
  const handleScenePick = useCallback((pick: ScenePick) => {
    const id = crypto.randomUUID();
    const ref = refFromPick(id, pick.bodyId, pick.bodyName, pick.point, pick.attribution);
    setContextRefs((refs) => [...refs, ref]);
    if (ref.kind === "object") setSelectedBodyId(pick.bodyId);
    composerRef.current?.insertToken(id, refLabel(ref), ref.kind);
    composerRef.current?.focus();
  }, []);

  // Labels are semantic references: unlike a surface pick, a facility label
  // adds only its exact manifest name and therefore never creates a place pin.
  const handleEntityLabelPick = useCallback((pick: SceneEntityLabelPick) => {
    const id = crypto.randomUUID();
    const ref: SceneContextRef = pick.kind === "facility"
      ? { kind: "facility", id, name: pick.name }
      : {
          kind: "object",
          id,
          name: pick.name,
          body: pick.bodyName,
          bodyId: pick.bodyId,
          worldPos: pick.worldPos,
        };
    setContextRefs((refs) => [...refs, ref]);
    if (pick.kind === "object") setSelectedBodyId(pick.bodyId);
    composerRef.current?.insertToken(id, refLabel(ref), ref.kind);
    composerRef.current?.focus();
  }, []);

  const handleRobotLabelPick = useCallback((pick: RobotIdentityLabelPick) => {
    const id = crypto.randomUUID();
    const ref: SceneContextRef = {
      kind: "robot",
      id,
      name: pick.name,
      bodyId: pick.bodyId,
    };
    setContextRefs((refs) => [...refs, ref]);
    setSelectedBodyId(pick.bodyId);
    composerRef.current?.insertToken(id, refLabel(ref), ref.kind);
    composerRef.current?.focus();
  }, []);

  // The composer is the source of truth for which tokens still exist; prune refs
  // a backspace/cut removed, preserving order.
  const handleRefsPresent = useCallback((ids: string[]) => {
    const present = new Set(ids);
    setContextRefs((refs) =>
      refs.length === present.size && refs.every((r) => present.has(r.id))
        ? refs
        : refs.filter((r) => present.has(r.id)),
    );
    setPlanRefs((refs) =>
      refs.length === present.size && refs.every((r) => present.has(r.id))
        ? refs
        : refs.filter((r) => present.has(r.id)),
    );
  }, []);

  // Clicking a token focuses its 3D anchor so the user can adjust it (drag the
  // marker). Object → highlight the body; position → marker is already shown.
  const handleTokenClick = useCallback(
    (refId: string) => {
      const ref = contextRefs.find((r) => r.id === refId);
      if (ref?.kind === "object") setSelectedBodyId(ref.bodyId);
      if (ref?.kind === "robot") setSelectedBodyId(ref.bodyId);
      if (ref?.kind === "facility") {
        const facility = manifest?.facilities?.[ref.name];
        const bodyName = facility?.body ?? facility?.place?.surface_body;
        const api = apiRef.current;
        if (typeof bodyName === "string" && api) {
          const body = api.getBodies().find((candidate) => candidate.name === bodyName);
          if (body) setSelectedBodyId(body.id);
        }
      }
      const planRef = planRefs.find((r) => r.id === refId);
      if (planRef && draftPlan) {
        const task = draftPlan.tasks.find((candidate) => candidate.task === planRef.actionId);
        const stepId = task?.steps.find((step) => step.id)?.id;
        if (stepId) setSelectedStepId(stepId);
      }
    },
    [contextRefs, planRefs, draftPlan, manifest],
  );

  const handlePlanReferenceBar = useCallback(
    (actionId: string | null | undefined) => {
      if (!actionId) return;
      const action = actionsById.get(actionId);
      // Resolver-created task bars (e.g. go_to_rest) have no semantic action
      // and therefore remain unreferenceable. Every bar backed by a stable
      // AugmentedAction id is safe to send as a semantic plan reference,
      // including open/close/go_to actions.
      if (!action) return;
      const id = crypto.randomUUID();
      const ref = planTaskRefFromAction(id, action);
      setPlanRefs((refs) => [...refs, ref]);
      composerRef.current?.insertToken(id, `${action.robot} · ${ref.label}`, "plan_task", action.robot);
      composerRef.current?.focus();
    },
    [actionsById],
  );

  // Best-effort: clicking a [[ref:NAME]] chip in an assistant message selects
  // the Gantt bar for the task that references it, if one can be found.
  // Never throws — a miss (no matching action, or no step in the current
  // draftPlan) is simply a no-op.
  const handleMessageRefClick = useCallback(
    (name: string, kind: "object" | "facility", actionId?: string) => {
      // Highlight the referenced scene body (works even before any plan/compile):
      // manifest name -> mujoco body name -> live body id via the sim API.
      const bodyName =
        kind === "object"
          ? manifest?.objects?.[name]?.body
          : (manifest?.facilities?.[name]?.body ?? manifest?.facilities?.[name]?.place?.surface_body);
      const api = apiRef.current;
      if (typeof bodyName === "string" && api) {
        const body = api.getBodies().find((b) => b.name === bodyName);
        if (body) setSelectedBodyId(body.id);
      }
      // Select the matching Gantt task bar. The tag's action id pins the chip
      // to ONE task (two moves to the same sink are distinct); fall back to
      // first-name-match for id-less tags (note lines, removals, old messages).
      const action =
        (actionId ? semanticActions.find((a) => a.id === actionId) : undefined) ??
        semanticActions.find((a) =>
          kind === "object" ? a.object === name : a.dest === name || a.facility === name,
        );
      if (!action || !draftPlan) return;
      const task = draftPlan.tasks.find((t) => t.task === action.id);
      const stepId = task?.steps.find((s) => s.id)?.id;
      if (stepId) setSelectedStepId(stepId);
    },
    [semanticActions, draftPlan, manifest],
  );

  const handleDragPin = useCallback(
    (pinId: string, xy: [number, number]) =>
      setContextRefs((refs) =>
        refs.map((r) =>
          r.kind === "position" && r.id === pinId ? { ...r, xyz: [xy[0], xy[1], r.xyz[2]] } : r,
        ),
      ),
    [],
  );

  // Item 18/§3: any in-flight sync run is aborted the moment a chat turn (or
  // revert) starts — a visible user action always closes the current edit
  // episode, and letting the sync tail's reply land after a chat turn's
  // would just get discarded by the stale base_revision guard anyway (see
  // `commitOutcome`), so there is nothing to gain from letting it keep
  // running.
  const cancelPendingEditTail = () => {
    editTailSchedulerRef.current?.cancel();
  };

  const sendConversation = (
    text: string,
    intentHint: IntentHint = null,
    refs: SceneContextRef[] = [],
    refHandles?: Map<string, string>,
    referencedPlanTasks: PlanTaskRef[] = [],
    planRefHandles?: Map<string, string>,
    displayParts?: UserMessageDisplayPart[],
  ) => {
    const content = text.trim();
    if (!content || turnBusy) return;
    cancelPendingEditTail();
    const turnId = crypto.randomUUID();
    const userMsg: ConversationMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content,
      ...(displayParts?.length ? { displayParts } : {}),
    };
    const assistantId = crypto.randomUUID();
    const placeholder: ConversationMessage = {
      id: assistantId,
      role: "assistant",
      status: "working",
      // Free text isn't classified yet — the real intent arrives with the
      // intent_selected event; show a neutral "Thinking…" until then.
      // (There is no more Resolve button, so `sendConversation` is only ever
      // reached from typed chat text — `intentHint` stays `null` here; the
      // sync button's own "edit"/"resolve" hints go straight through
      // `runSyncTail`/`streamConversationTurn`, bypassing this chat path
      // entirely, per item 19's "no synthetic conversational turn".)
      intent: undefined,
      content: "Thinking…",
    };
    const nextMessages = [...messages, userMsg];
    const visibleMessages = [...nextMessages, placeholder];
    followLatestChatRef.current = true;
    setMessages(visibleMessages);
    composerRef.current?.clear();
    setContextRefs([]);
    setPlanRefs([]);
    setPlanRefMode(false);
    setChatError(null);
    setActiveTurnId(turnId);
    activeTurnRef.current = turnId;
    setSchedulePlaying(false);

    const onEvent = (event: StreamEvent) => {
      if (activeTurnRef.current !== turnId) return;
      if (event.type === "message_started") return;
      if (event.type !== "progress" && event.type !== "intent_selected" && event.type !== "warning") return;
      const text = event.text;
      if (text === undefined) return;
      setMessages((cur) =>
        cur.map((m) =>
          m.id === assistantId
            ? {
                ...m,
                content: text,
                intent:
                  event.type === "intent_selected" && event.intent
                    ? (event.intent as ConversationMessage["intent"])
                    : m.intent,
                activities: [...(m.activities ?? []), { seq: event.seq ?? 0, stage: event.stage, text }],
              }
            : m,
        ),
      );
    };

    // Read synchronously, before the request goes out — see `commitOutcome`'s
    // `sentEdits` doc comment for why this must be captured now rather than
    // re-read from `pendingEditsRef.current` after the `await` below (an edit
    // made while this request is in flight must not be swept up as "sent").
    const sentEdits = pendingEditsRef.current;
    streamConversationTurn(
      {
        text: content,
        intentHint,
        messages: nextMessages,
        semanticActions,
        draftPlan,
        previousPlan: livePlan,
        completed,
        compileId,
        gate: { status, dirty, inSync, delegableConflictCount },
        conflicts,
        warnings,
        lastResolverReport,
        sceneRefs: refs,
        sceneRefHandles: refHandles,
        planRefs: referencedPlanTasks,
        planRefHandles,
        turnId,
        baseRevision: baseRevisionRef.current,
        // D2b: sent with every turn, chat included — an author turn applies
        // this batch onto the plan it lands on (the pure-append graft, or
        // dropped-with-warning on a clean recompute; see
        // stream_compound_turn's docstring).
        edits: sentEdits,
        protected: buildProtectedSet(sentEdits, livePlan),
        // In the baseline condition, v2 means each chat prompt authors a
        // complete plan from scratch. The stream client keeps scene refs but
        // removes transcript history and all prior-plan state.
        statelessAuthoring: STUDY_COMPOUND_OUTPUT_MODE === "v2",
      },
      onEvent,
    )
      .then(async (outcome) => {
        // turn_id guard: drop results from a superseded turn.
        if (activeTurnRef.current !== turnId) return;
        await applyOutcome(assistantId, outcome, sentEdits, visibleMessages, content);
      })
      .catch((err) => {
        if (activeTurnRef.current !== turnId) return;
        const msg = err instanceof Error ? err.message : String(err);
        finalizeMessage(assistantId, { status: "error", content: msg });
        // The finalized assistant bubble is the canonical visible failure.
        // Mirroring the same text into chatError rendered every compile error
        // twice in the conversation rail.
      })
      .finally(() => {
        if (activeTurnRef.current === turnId) {
          activeTurnRef.current = null;
          setActiveTurnId(null);
        }
      });
  };

  // Serialize the composer (text + inline ref tokens) into a plain user message
  // (tokens → their manifest name / readable phrase) plus the structured refs.
  const submitFromComposer = (parts: ComposerPart[]) => {
    const byId = new Map(contextRefs.map((r) => [r.id, r]));
    const planById = new Map(planRefs.map((r) => [r.id, r]));
    // Handles are assigned over refs in document order (the order they appear
    // in `parts`), so gather them before building the text so the same
    // assignment drives both the inline "(pin pK)" marker and serialization.
    const usedRefs: SceneContextRef[] = [];
    const usedPlanRefs: PlanTaskRef[] = [];
    for (const part of parts) {
      if (part.type === "ref") {
        const ref = byId.get(part.refId);
        if (ref) usedRefs.push(ref);
        const planRef = planById.get(part.refId);
        if (planRef) usedPlanRefs.push(planRef);
      }
    }
    const handles = assignPinHandles(usedRefs);
    const planHandles = assignPlanTaskHandles(usedPlanRefs);
    let text = "";
    const displayParts: UserMessageDisplayPart[] = [];
    for (const part of parts) {
      if (part.type === "text") {
        text += part.value;
        displayParts.push(part);
      } else {
        const ref = byId.get(part.refId);
        if (ref) {
          const handle = handles.get(ref.id);
          text += handle ? `${refText(ref)} (pin ${handle})` : refText(ref);
          displayParts.push({ type: "ref", label: refLabel(ref), kind: ref.kind });
          continue;
        }
        const planRef = planById.get(part.refId);
        if (planRef) {
          const handle = planHandles.get(planRef.id);
          text += `${planRef.robot} · ${planRef.label}${handle ? ` (plan task ${handle})` : ""}`;
          displayParts.push({
            type: "ref",
            label: `${planRef.robot} · ${planRef.label}`,
            kind: "plan_task",
            robot: planRef.robot,
          });
        }
      }
    }
    sendConversation(
      text.trim(),
      null,
      usedRefs,
      handles,
      usedPlanRefs,
      planHandles,
      displayParts,
    );
  };

  // The merged sync button (item 19, replacing the separate Compile/Resolve
  // buttons): ONE entry point for both of its jobs, chosen by whether a batch
  // is pending -- never two code paths.
  //   - Pending edits: `intent_hint: "edit"` applies the whole batch (same
  //     backend path the old debounced tail used, item 17 revised).
  //   - Nothing pending: `intent_hint: "resolve"`, i.e. what the old Resolve
  //     button sent -- the backend's own gate (`_resolve_gate_fails`) rewrites
  //     it into a plain re-resolve of the already-committed plan (D3 in
  //     compound_turn_integration_spec.md). This is the sync button's other
  //     documented job: the recovery path for a D3b deferral.
  // Both branches are visible conversation turns: Sync appends a compact
  // synthetic user message (`Apply edits` / `Re-check plan`) and streams the same assistant
  // progress events as a typed chat turn. `editActivity` remains the compact
  // Gantt-toolbar status for the same run.
  const runSyncTail = async (signal: AbortSignal) => {
    if (turnBusy) return;
    // D2b collapsed the old ledger/count split into one list, so this is now
    // just "is there anything pending" -- but the distinction it decides
    // still matters just as much: "edit" applies this batch onto the
    // previous resolved plan, whereas "resolve" continues from the last
    // VERIFIED plan with no batch at all. A D3b deferral-recovery press needs
    // the latter; sending it down the edit path with an empty batch would
    // still re-derive nothing usefully and skip the resume semantics
    // `_resolve_gate_fails`/D3 give the button.
    const sentEdits = pendingEditsRef.current;
    const hasPendingEdits = sentEdits.length > 0;
    const turnId = crypto.randomUUID();
    const assistantId = crypto.randomUUID();
    const syncChat = createSyncChatMessages(
      messages,
      { userId: crypto.randomUUID(), assistantId },
      hasPendingEdits,
    );
    const nextMessages = syncChat.requestMessages;
    followLatestChatRef.current = true;
    setMessages(syncChat.visibleMessages);
    setChatError(null);
    setActiveTurnId(turnId);
    activeTurnRef.current = turnId;
    setEditTailStatus("running");
    setEditActivity(hasPendingEdits ? "Applying your edits…" : "Re-checking conflicts…");

    const onEvent = (event: StreamEvent) => {
      if (activeTurnRef.current !== turnId) return;
      if ((event.type === "progress" || event.type === "warning") && event.text) {
        setEditActivity(event.text);
      }
      setMessages((cur) => applySyncProgress(cur, assistantId, event));
    };

    try {
      const outcome = await streamConversationTurn(
        {
          text: "",
          intentHint: hasPendingEdits ? "edit" : "resolve",
          messages: nextMessages,
          semanticActions,
          draftPlan,
          previousPlan: livePlan,
          completed,
          compileId,
          gate: { status, dirty, inSync, delegableConflictCount },
          conflicts,
          warnings,
          lastResolverReport,
          sceneRefs: [],
          planRefs: [],
          turnId,
          baseRevision: baseRevisionRef.current,
          edits: sentEdits,
          protected: buildProtectedSet(sentEdits, livePlan),
          signal,
        },
        onEvent,
      );
      // Superseded by a newer press while this request was in flight -- that
      // press's own run (or a chat turn's cancelPendingEditTail) owns
      // whatever happens next.
      if (signal.aborted) return;
      if (outcome.kind === "turn_result") {
        const finalMessagePatch: Partial<ConversationMessage> = {
          status: "done",
          source: "resolver",
          content: studyTurnResultContent(outcome, livePlan, semanticActions),
          details: turnResultDetails(outcome),
          excludeFromModel: STUDY_COMPOUND_OUTPUT_MODE === "v2",
        };
        const applied = await commitOutcome(outcome, hasPendingEdits ? "edit" : "sync", sentEdits, {
          source: "sync",
          title: hasPendingEdits ? "Applied manual edits" : "Re-checked conflicts",
          messages: patchedMessages(syncChat.visibleMessages, assistantId, finalMessagePatch),
          lastResolverReport: outcome.report ?? null,
        });
        if (applied) {
          // Keeps `resolverNotConverged` (the sync button's own D3b-deferral
          // reachability gate) current after a sync press, not just after a
          // chat turn -- otherwise a second deferral in a row would look
          // like the button had nothing left to recover.
          setLastResolverReport(outcome.report ?? null);
        }
        setEditActivity(applied ? outcome.message : "Discarded — a newer change already applied.");
        finalizeMessage(
          assistantId,
          applied
            ? finalMessagePatch
            : {
                ...finalMessagePatch,
                content: `${turnResultBubbleContent(outcome)}\n\n(Discarded — a newer change already applied.)`,
                details: undefined,
              },
        );
      } else if (outcome.kind === "answer") {
        // The backend's resolve-eligibility gate (defensive; the button is
        // already disabled client-side when ineligible) — surfaced, not
        // silently dropped.
        setEditActivity(outcome.content);
        finalizeMessage(assistantId, { status: "done", source: "resolver", content: outcome.content });
      } else if (outcome.kind === "error") {
        setEditActivity(outcome.message);
        finalizeMessage(assistantId, { status: "error", content: outcome.message });
        setChatError(outcome.message);
        if (
          hasPendingEdits
          && !sentEdits.some((edit) => edit.op === "remove_task")
          && isTopologyRejected((outcome as { details?: unknown }).details)
        ) {
          // The backend rejected the candidate before MuJoCo compilation.  Do
          // not leave an impossible optimistic Gantt draft on screen: the
          // previous live plan remains the authoritative, verified state.
          setDraftPlan(livePlan);
          pendingEditsRef.current = [];
          setPendingEditCount(0);
          setDraftEditImpact("none");
        }
      }
    } catch (err) {
      if (signal.aborted) return; // cancelled by a newer press
      const message = err instanceof Error ? err.message : String(err);
      setEditActivity(message);
      finalizeMessage(assistantId, { status: "error", content: message });
      setChatError(message);
      const details = (err as { details?: unknown } | null)?.details;
      if (
        hasPendingEdits
        && !sentEdits.some((edit) => edit.op === "remove_task")
        && isTopologyRejected(details)
      ) {
        // streamConversationTurn throws error events before returning an
        // outcome, so mirror the structured-error path above here.
        setDraftPlan(livePlan);
        pendingEditsRef.current = [];
        setPendingEditCount(0);
        setDraftEditImpact("none");
      }
    } finally {
      if (activeTurnRef.current === turnId) {
        activeTurnRef.current = null;
        setActiveTurnId(null);
      }
      setEditTailStatus("idle");
    }
  };
  // Always call through to the LATEST render's runSyncTail (see
  // `runSyncTailRef`'s declaration) — the scheduler instance itself is
  // created exactly once and must never close over a stale render.
  runSyncTailRef.current = runSyncTail;

  const editDraft = (
    impact: "spatial" | "structural",
    next: (p: AuthoredPlan) => AuthoredPlan,
    delta: PlanEditDelta,
  ) => {
    if (turnBusy) return;
    ensureManualEditBaseline();
    if (exploreMode) returnToPlan();
    else setSchedulePlaying(false);
    bumpBaseRevision();
    setDraftEditImpact((current) =>
      current === "structural" || impact === "structural" ? "structural" : "spatial",
    );
    setDraftPlan((p) => (p ? next(p) : p));
    // Synchronous push (a ref, not state — see pendingEditsRef's declaration)
    // so several edits made before the next sync press all land in the same
    // batch. Item 17 (revised): this arms NO tail — a tail run is several
    // full verification compiles, so nothing fires until the user presses
    // sync. `pendingEditCount` mirrors `pendingEditsRef.current.length`
    // purely so the Gantt toolbar re-renders on each push (a ref alone
    // wouldn't); `commitOutcome` is what actually shrinks the list, per D2b.
    pendingEditsRef.current = [...pendingEditsRef.current, delta];
    setPendingEditCount(pendingEditsRef.current.length);
  };

  /**
   * Task view's Shift-drag dependency. Both ends arrive as semantic action ids
   * and are resolved here against the plan the edit will actually apply to, so
   * the queued delta always names real authored steps: the dragged task's first
   * step waits for the target task's last step. Bailing out on an unresolvable
   * id is deliberate — queueing a delta that `setStepAfter` would discard is
   * what made this gesture look like it did nothing.
   */
  const handleSetTaskAfter = (actionId: string, afterActionId: string) => {
    if (!draftPlan) return;
    const dragged = taskStepBounds(draftPlan, actionId);
    const target = taskStepBounds(draftPlan, afterActionId);
    if (!dragged || !target || dragged.first === target.last) return;
    editDraft("structural", (p) => setStepAfter(p, dragged.first, [target.last]), {
      op: "set_step_after",
      target: { stepId: dragged.first },
      after: [target.last],
    });
  };

  const toggleTaskRemoval = (actionId: string) => {
    if (turnBusy || !removableTaskIds.has(actionId)) return;
    ensureManualEditBaseline();
    if (exploreMode) returnToPlan();
    else setSchedulePlaying(false);

    const current = pendingEditsRef.current;
    const alreadyPending = current.some(
      (edit) => edit.op === "remove_task" && edit.target.actionId === actionId,
    );
    const next = alreadyPending
      ? current.filter(
          (edit) => !(edit.op === "remove_task" && edit.target.actionId === actionId),
        )
      : [...current, { op: "remove_task" as const, target: { actionId } }];

    bumpBaseRevision();
    pendingEditsRef.current = next;
    setPendingEditCount(next.length);
    // Per-task Undo before the first Sync can return the plan to a genuinely
    // untouched state. Do not leave a global Undo button for an edit session
    // that never committed and no longer has a draft difference.
    if (
      next.length === 0
      && editEpisodeRef.current === null
      && draftPlan === livePlan
    ) {
      manualEditBaselineRef.current = null;
    }
    setDraftEditImpact(
      next.some((edit) =>
        edit.op === "remove_task" || edit.op === "move_task"
          || edit.op === "set_task_robot" || edit.op === "set_step_after",
      )
        ? "structural"
        : next.length > 0
          ? "spatial"
          : "none",
    );
    if (!alreadyPending) setSelectedStepId(null);
  };

  const undoAllManualEdits = async () => {
    const baseline = manualEditBaselineRef.current;
    if (!baseline || !dirty || turnBusy) return;

    setVersionSwitching(true);
    cancelPendingEditTail();
    const restored = restoreManualEditBaseline(baseline);
    manualEditBaselineRef.current = null;
    editEpisodeRef.current = null;
    bumpBaseRevision();
    pendingEditsRef.current = [];
    setPendingEditCount(0);
    setDraftEditImpact("none");
    setEditTailStatus("idle");
    setEditActivity("Restored the plan to its state before manual edits.");
    setSemanticActions(restored.semanticActions);
    setMessages(restored.messages);
    setLastResolverReport(restored.lastResolverReport);
    setVersionHistory(restored.versionHistory);
    setChatError(null);
    setSelectedStepId(null);
    setSchedulePlaying(false);

    try {
      if (restored.compile) {
        await adoptLivePlan(restored.plan, restored.compile);
      } else {
        const seed = ensureStepIds(restored.plan);
        setLivePlan(seed);
        setDraftPlan(seed);
        setScheduleTime(0);
      }
    } finally {
      setVersionSwitching(false);
    }
  };

  const resetSchedule = () => {
    if (exploreMode) return;
    if (orbitControlsRef.current) {
      resetCameraToScenePresentation(orbitControlsRef.current, scenePresentation);
    }
    setSchedulePlaying(false);
    setScheduleTime(0);
    setResetNonce((n) => n + 1); // remount SchedulePlayer -> keyframe + start
  };

  const confirmScene = () => {
    setSceneStatus("opening scene");
    setSchedulePlaying(false);
    setExploreMode(false);
    fetch(`${backendUrl}/api/scene/open`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenePath: sceneInput }),
    })
      .then((response) => response.json() as Promise<SceneSession>)
      .then((session) => {
        if (!session.ok) {
          throw new Error(session.error || "open scene failed");
        }
        setSceneInput(session.sceneFile);
        setSceneConfig({ src: session.src, sceneFile: session.sceneFile });
        setSelectedBodyId(null);
        setSimReady(false);
        apiRef.current = null;
        setSceneConfirmed(true);
      })
      .catch((err) => {
        setSceneStatus(err instanceof Error ? err.message : String(err));
      });
  };

  const handleError = (err: Error) => {
    console.error("[mujoco-react viewer]", err);
  };

  const resetScene = () => {
    const api = apiRef.current;
    if (!api) return;
    setSelectedBodyId(null);
    if (hasPlan && !exploreMode) {
      resetSchedule();
    } else {
      if (orbitControlsRef.current) {
        resetCameraToScenePresentation(orbitControlsRef.current, scenePresentation);
      }
      applyAndCaptureInitialSceneState(api);
    }
  };

  const restorePlanAtCurrentTime = () => {
    schedulePlayerRef.current?.seek(scheduleTime);
  };

  const enterExplore = () => {
    if (!exploreAvailable) return;
    setSchedulePlaying(false);
    setMarkerDragging(false);
    setExploreMode(true);
  };

  const returnToPlan = () => {
    setSchedulePlaying(false);
    setMarkerDragging(false);
    // Rebuild only when a synchronized compiled player exists. A draft-only
    // plan has no trajectory to restore, so returning simply freezes the live
    // scene until compilation supplies one.
    if (playbackAvailable) restorePlanAtCurrentTime();
    setExploreMode(false);
  };

  const toggleSchedulePlayback = () => {
    if (schedulePlaying) {
      setSchedulePlaying(false);
      return;
    }
    if (exploreMode) {
      // Play is also a safe exit from Explore: restore qpos first, then let
      // SchedulePlayer resume kinematic ownership on the next frame.
      returnToPlan();
    }
    if (!playbackAvailable) return;
    setSchedulePlaying(true);
  };

  if (!sceneConfirmed) {
    return (
      <div className="scene-launcher">
        <section className="scene-launcher-panel">
          <h1>Open Scene</h1>
          <div className="scene-launcher-row">
            <ScenePathPicker
              id="scene-path"
              ariaLabel="Scene path"
              value={sceneInput}
              paths={suggestedScenePaths}
              onChange={setSceneInput}
              onConfirm={confirmScene}
            />
            <button type="button" onClick={confirmScene}>
              Confirm Scene
            </button>
          </div>
          <div className="scene-launcher-status">{sceneStatus}</div>
        </section>
      </div>
    );
  }

  return (
    <MujocoProvider
      mtWasmUrl={mtMujocoWasmUrl}
      threadedLoader={threadedMujocoLoader}
      wasmVariant="auto"
      timeout={90000}
      onError={handleError}
    >
      <div className="scene-shell-v2">
        <div className="scene-top">
          <main className="viewer-pane">
            <MujocoCanvas
              key={`${sceneConfig.src}${sceneConfig.sceneFile}`}
              config={sceneConfig}
              camera={scenePresentation.camera}
              // A compiled plan normally pauses physics for kinematic playback.
              // Explore explicitly hands control back to live physics/dragging.
              paused={shouldPauseScene({ hasPlan, exploreMode, simReady })}
              onReady={({ api }) => {
                apiRef.current = api;
                applyAndCaptureInitialSceneState(api);
                setSimReady(true);
              }}
              onError={handleError}
              onSelection={({ bodyId }) => {
                // Native double-click select/deselect (highlight toggle). In pick
                // mode the ScenePickController owns the double-click instead.
                if (pickMode) return;
                setSelectedBodyId((current) => (bodyId === current ? null : bodyId));
              }}
              style={{ width: "100%", height: "100%" }}
            >
              <SelectionHighlight bodyId={selectedBodyId} />
              <ScenePickController bodyIndex={bodyIndex} enabled={pickMode} onPick={handleScenePick} />
              {planSubtitlesEnabled || pickMode ? (
                <RobotIdentityLabels
                  robots={manifest?.robots}
                  pickEnabled={pickMode}
                  onLabelPick={handleRobotLabelPick}
                />
              ) : null}
              {(planSubtitlesEnabled || pickMode) && manifest ? (
                <SceneEntityLabels
                  manifest={manifest}
                  showObjects={SHOW_SCENE_OBJECT_LABELS}
                  pickEnabled={pickMode}
                  onLabelPick={handleEntityLabelPick}
                />
              ) : null}
              <ReadyPoseController enabled={exploreToolsEnabled} captureRevision={readyPoseRevision} />
              {exploreToolsEnabled ? <DragInteraction /> : null}
              {playbackAvailable ? (
                <SchedulePlayer
                  ref={schedulePlayerRef}
                  key={`sched-${epoch}-${resetNonce}`}
                  apiRef={apiRef}
                  items={items}
                  playing={schedulePlaying}
                  speed={scheduleSpeed}
                  initialKeyframe={initialStateKeyframe}
                  onTime={(t) => setScheduleTime(t)}
                  onComplete={() => setSchedulePlaying(false)}
                />
              ) : null}
              {overlayVisibility.showOverlay ? (
                <PlanOverlay
                  markers={overlayVisibility.showPlanMarkers ? markers : []}
                  showReadOnlyStandoffs={SHOW_READ_ONLY_STANDOFFS}
                  pins={pins}
                  onDragPin={handleDragPin}
                  onDragStateChange={setMarkerDragging}
                  onDragAt={(stepId, xy) =>
                    editDraft("spatial", (p) => setStepAt(p, stepId, xy), {
                      op: "set_step_at",
                      target: { stepId },
                      at: xy,
                    })
                  }
                  onSetViaPoints={(stepId, viaPoints) =>
                    editDraft("spatial", (p) => setStepViaPoints(p, stepId, viaPoints), {
                      op: "set_step_via",
                      target: { stepId },
                      via: viaPoints ?? [],
                    })
                  }
                  onSetStandoff={(stepId, xy) =>
                    editDraft("spatial", (p) => setStepStandoff(p, stepId, xy), {
                      op: "set_step_standoff",
                      target: { stepId },
                      standoff: xy,
                    })
                  }
                />
              ) : null}
              <OrbitControls
                ref={orbitControlsRef}
                makeDefault
                enabled={!markerDragging}
                target={scenePresentation.target}
              />
              <ambientLight intensity={0.7} />
              <directionalLight position={[1, 2, 5]} intensity={1.2} />
            </MujocoCanvas>
            {planSubtitlesEnabled && schedulePlaying ? (
              <PlanTaskSubtitles bars={taskBars} time={scheduleTime} robotIds={robotIds} />
            ) : null}
            <button
              type="button"
              className="scene-reset-button"
              onClick={resetScene}
              disabled={!simReady}
            >
              Reset
            </button>
            {EXPLORE_ENABLED ? (
              <button
                type="button"
                className={`scene-explore-button${exploreMode ? " is-active" : ""}`}
                onClick={exploreMode ? returnToPlan : enterExplore}
                disabled={!exploreMode && !exploreAvailable}
                title={
                  exploreMode
                    ? playbackAvailable
                      ? "Restore the compiled plan at the current timeline time"
                      : "Return to the draft plan and pause live physics"
                    : !draftPlan
                      ? "The scene is already directly interactive until a plan is generated"
                      : "Explore the scene with live physics and dragging"
                }
              >
                {exploreMode ? "Return to plan" : "Explore"}
              </button>
            ) : null}
          </main>

          <aside className="chat-rail" aria-label="Assistant">
            <div
              className="chat-rail-body"
              aria-live="polite"
              ref={chatBodyRef}
              onScroll={(event) => {
                const body = event.currentTarget;
                followLatestChatRef.current =
                  body.scrollHeight - body.scrollTop - body.clientHeight < 32;
              }}
            >
              {messages.length === 0 ? (
                <p className="chat-rail-hint">
                  Describe the task in natural language — e.g. “Put the two cups in the sink.”
                  Use + to refer a plan task, or 📍 to select a scene object or location.
                </p>
              ) : (
                messages.map((message) =>
                  message.role === "user" ? (
                    <UserMessage message={message} key={message.id} />
                  ) : (
                    <AssistantMessage
                      message={message}
                      manifest={manifest}
                      onRefClick={handleMessageRefClick}
                      key={message.id}
                    />
                  ),
                )
              )}
              {chatError ? (
                <p className="chat-rail-error" role="alert">
                  {chatError}
                </p>
              ) : null}
              {targetError ? (
                <p className="chat-rail-error" role="alert">
                  {targetError}
                </p>
              ) : null}
              {targetMatched ? (
                <p className="chat-target-match" role="status">
                  Target matched — round complete.
                </p>
              ) : null}
            </div>
            <div className="chat-rail-composer">
              <div className={`composer-box${pickMode || planRefMode ? " is-picking" : ""}`}>
                <RefComposer
                  ref={composerRef}
                  disabled={turnBusy}
                  placeholder="Instruct…"
                  onSend={submitFromComposer}
                  onRefsPresent={handleRefsPresent}
                  onTokenClick={handleTokenClick}
                />
                <div className="composer-actions">
                  <button
                    type="button"
                    className={`composer-icon-btn plan-ref${planRefMode ? " is-active" : ""}`}
                    aria-pressed={planRefMode}
                    aria-label="Pick a plan task reference"
                    title="Pick a plan task reference from the task timeline"
                    onClick={() => {
                      setPlanRefMode((enabled) => {
                        const next = !enabled;
                        if (next) {
                          setPickMode(false);
                          setViewMode("task");
                          setSelectedStepId(null);
                        }
                        return next;
                      });
                    }}
                  >
                    +
                  </button>
                  <button
                    type="button"
                    className={`composer-icon-btn pin${pickMode ? " is-active" : ""}`}
                    aria-pressed={pickMode}
                    aria-label="Pick a scene reference"
                    title="Pick a scene reference: double-click a label, object, or spot in the scene"
                    onClick={() => {
                      setPickMode((enabled) => {
                        const next = !enabled;
                        if (next) setPlanRefMode(false);
                        return next;
                      });
                    }}
                  >
                    📍
                  </button>
                  <button
                    type="button"
                    className="composer-icon-btn send"
                    aria-label="Send"
                    title="Send (Enter)"
                    onClick={() => composerRef.current?.submit()}
                    disabled={turnBusy}
                  >
                    <svg
                      width="16"
                      height="16"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      aria-hidden="true"
                    >
                      <polyline points="9 10 4 15 9 20" />
                      <path d="M20 4v7a4 4 0 0 1-4 4H4" />
                    </svg>
                  </button>
                </div>
              </div>
            </div>
          </aside>
        </div>

        {/* Sync already reports live activity in Chat; do not repeat it beside
            the playback-speed control. */}
        <GanttPanel
          bars={viewBars}
          ghostBars={ghostBars}
          draftProjection={showDraftProjection}
          lanes={robotIds}
          warnings={shownWarnings}
          conflicts={showPreview ? [] : conflicts}
          time={scheduleTime}
          playing={schedulePlaying}
          status={editTailStatus === "running" ? undefined : statusText}
          collapsed={collapsed}
          selectedKey={selectedStepId}
          transportDisabled={!playbackAvailable || exploreMode}
          viewMode={viewMode}
          removableTaskIds={removableTaskIds}
          pendingRemovalIds={pendingRemovalIds}
          onToggleTaskRemoval={toggleTaskRemoval}
          draggableTaskIds={new Set(semanticActions.map((action) => action.id))}
          speed={scheduleSpeed}
          onSetSpeed={setScheduleSpeed}
          onToggleView={() => {
            if (!planRefMode) setViewMode((m) => (m === "task" ? "step" : "task"));
          }}
          headerActions={
            <>
              <button hidden type="button" className="gantt-icon-btn" onClick={loadSamplePlan}>
                Load sample plan
              </button>
              {/* Merged sync button (item 19, revised) — replaces the old
                  separate Compile and Resolve buttons; neither exists any
                  more. One entry point (`runSyncTail`) for both jobs: pending
                  edits -> apply the batch; nothing pending -> plain
                  re-resolve (also the D3b deferral recovery path, so it must
                  stay reachable while `resolverNotConverged`). Prominent
                  (primary styling) when edits are pending, quiet otherwise --
                  since item 17 stopped auto-running, THIS button is the only
                  way a manual edit ever reaches the backend. Deliberately NO
                  count: "you have unapplied changes" is the whole signal a
                  number would add nothing to (item 19). */}
              <button
                type="button"
                className={`gantt-icon-btn subtitle-toggle${planSubtitlesEnabled ? " is-active" : ""}`}
                aria-pressed={planSubtitlesEnabled}
                onClick={() => setPlanSubtitlesEnabled((enabled) => !enabled)}
                title={`${planSubtitlesEnabled ? "Hide" : "Show"} scene labels and robot task subtitles`}
              >
                Labels
              </button>
              <button
                type="button"
                className={pendingEditCount > 0 ? "gantt-btn-primary" : "gantt-icon-btn"}
                onClick={() => editTailSchedulerRef.current?.run()}
                disabled={
                  !draftPlan ||
                  turnBusy ||
                  editTailStatus === "running" ||
                  (pendingEditCount === 0 &&
                    (status !== "ready" ||
                      dirty ||
                      !inSync ||
                      (delegableConflictCount === 0 && !resolverNotConverged)))
                }
                title={
                  !draftPlan
                    ? "Create a plan before applying edits"
                    : pendingEditCount > 0
                      ? "Apply your pending edits and re-verify"
                      : resolverNotConverged
                        ? "Re-run resolve after a deferral"
                        : delegableConflictCount === 0
                          ? "No delegable path or facility conflicts"
                          : `Resolve ${delegableConflictCount} scheduling conflict(s)`
                }
              >
                {editTailStatus === "running" ? "Applying…" : "Apply edits"}
              </button>
              {draftPlan ? (
                <>
                {manualEditBaselineRef.current && dirty ? (
                  <button
                    type="button"
                    className="gantt-icon-btn"
                    onClick={() => void undoAllManualEdits()}
                    disabled={turnBusy || editTailStatus === "running"}
                    title="Discard all unsynced manual edits"
                  >
                    ↶ Undo edits
                  </button>
                ) : null}
                <button
                  type="button"
                  className="gantt-icon-btn"
                  onClick={clearPlan}
                  disabled={turnBusy}
                >
                  Clear
                </button>
                {CHECKPOINT_SAVE_ENABLED ? (
                  <button
                    type="button"
                    className="gantt-icon-btn"
                    onClick={() => void saveCurrentCheckpoint()}
                    disabled={turnBusy || checkpointSaving || dirty || !liveCompileSnapshot()}
                    title={
                      dirty
                        ? "Sync pending edits before saving a checkpoint"
                        : "Archive this playable plan and its compiled tracks"
                    }
                  >
                    {checkpointSaving ? "Saving…" : "Save"}
                  </button>
                ) : null}
                </>
              ) : null}
              {versionHistory.versions.length > 0 || CHECKPOINT_LOAD_ENABLED ? (
                <select
                  className="gantt-version-select"
                  aria-label="Plan version"
                  value={versionHistory.currentId ?? ""}
                  onFocus={() => void refreshSavedCheckpoints()}
                  onChange={(event) => {
                    const value = event.target.value;
                    if (value.startsWith("checkpoint:")) {
                      void loadSavedCheckpoint(value.slice("checkpoint:".length));
                    } else if (value) {
                      void switchPlanVersion(value);
                    }
                  }}
                  disabled={turnBusy}
                  title="Switch session versions or load a saved checkpoint"
                >
                  {versionHistory.currentId === null ? <option value="">Select plan…</option> : null}
                  {versionHistory.versions.map((version) => (
                    <option key={version.id} value={version.id}>
                      {versionDisplayLabel(versionHistory, version)}
                    </option>
                  ))}
                  {CHECKPOINT_LOAD_ENABLED && sceneCheckpoints.length > 0 ? (
                    <optgroup label="Saved checkpoints">
                      {sceneCheckpoints.map((checkpoint) => (
                        <option key={checkpoint.id} value={`checkpoint:${checkpoint.id}`}>
                          {checkpoint.id}
                        </option>
                      ))}
                    </optgroup>
                  ) : null}
                </select>
              ) : null}
              {STUDY_TARGET_LOAD_ENABLED ? (
                <select
                  className="gantt-version-select"
                  aria-label="Target checkpoint"
                  value={studyTarget?.id ?? ""}
                  onFocus={() => void refreshSavedCheckpoints()}
                  onChange={(event) => {
                    const checkpointId = event.target.value;
                    if (!checkpointId) {
                      setStudyTarget(null);
                      setTargetError(null);
                      return;
                    }
                    void loadTargetCheckpoint(checkpointId);
                  }}
                  disabled={turnBusy || targetLoading}
                  title="Load a checkpoint as the hidden study target"
                >
                  <option value="">{targetLoading ? "Loading target…" : "Load target…"}</option>
                  {sceneCheckpoints.map((checkpoint) => (
                    <option key={checkpoint.id} value={checkpoint.id}>
                      Target: {checkpoint.id}
                    </option>
                  ))}
                </select>
              ) : null}
            </>
          }
          onToggleCollapse={() => setCollapsed((c) => !c)}
          onPlayToggle={toggleSchedulePlayback}
          onReset={resetSchedule}
          onSeek={(time) => {
            const player = schedulePlayerRef.current;
            if (!player) return;
            setSchedulePlaying(false);
            player.seek(time);
          }}
          onReferenceBar={
            planRefMode && viewMode === "task"
              ? (bar) => handlePlanReferenceBar(bar.group)
              : undefined
          }
          onSelectBar={(bar) => setSelectedStepId(bar.key)}
          onMoveTask={(actionId, robot, afterActionId) =>
            editDraft("structural", (p) => moveTask(p, actionId, robot as RobotName, afterActionId), {
              op: "move_task",
              target: { actionId },
              robot: robot as RobotName,
              afterActionId,
            })
          }
          onSetAfter={(stepId, afterId) =>
            editDraft("structural", (p) => setStepAfter(p, stepId, afterId ? [afterId] : []), {
              op: "set_step_after",
              target: { stepId },
              after: afterId ? [afterId] : [],
            })
          }
          onSetTaskAfter={handleSetTaskAfter}
        />
      </div>
    </MujocoProvider>
  );
}
