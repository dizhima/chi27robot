import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentRef,
  type RefObject,
} from "react";
import { Link } from "react-router-dom";
import { OrbitControls } from "@react-three/drei";
import {
  DragInteraction,
  MujocoCanvas,
  MujocoProvider,
  TrajectoryPlayer,
  useBeforePhysicsStep,
} from "mujoco-react";
import type {
  MujocoSimAPI,
  SceneConfig,
  TrajectoryFrame,
} from "mujoco-react";
import { ChannelTrackPlayer } from "./ChannelTrackPlayer";
import type { SkillTrack } from "./ChannelTrackPlayer";
import { SchedulePlayer } from "./SchedulePlayer";
import type { ScheduledItem, SchedulePlayerHandle } from "./SchedulePlayer";
import { compileAndLoad } from "./plan/compilePlan";
import {
  SAMPLE_PLAN,
  SAMPLE_PLAN_CABINET,
  SAMPLE_PLAN_DRAWER,
  SAMPLE_PLAN_FRIDGE,
  SAMPLE_PLAN_FRIDGE_PREPLACE,
  SAMPLE_PLAN_FRIDGE_TWO_ROBOT,
  SAMPLE_PLAN_HORIZONTAL_PICK,
} from "./plan/samplePlan";
import type { AuthoredPlan, CompileResponse } from "./plan/planTypes";
import { RightWorkspace } from "./authoring/RightWorkspace";
import { loadSceneManifest } from "./authoring/grounding";
import type { SceneManifest } from "./authoring/types";
import { ScenePickController, type ScenePick } from "./ScenePickController";
import { ScenePathPicker } from "./ScenePathPicker";
import { PlanOverlay } from "./plan/PlanOverlay";
import { buildBodyIndex } from "./conversation/sceneContext";
import {
  debugPinHandle,
  debugPinLabel,
  serializeDebugPins,
  type DebugPin,
} from "./debugPins";
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
import { resetCameraToScenePresentation, scenePresentationFor } from "./scenePresentation";
import { robotIdsFromManifest } from "./robotRegistry";

const COMPILE_PLAN_OPTIONS = [
  { id: "sample", label: "Sample plan", plan: SAMPLE_PLAN },
  { id: "drawer", label: "Drawer plan", plan: SAMPLE_PLAN_DRAWER },
  { id: "cabinet", label: "Cabinet plan", plan: SAMPLE_PLAN_CABINET },
  { id: "fridge", label: "Fridge plan", plan: SAMPLE_PLAN_FRIDGE },
  {
    id: "fridge-preplace",
    label: "Fridge pre-place standoff",
    plan: SAMPLE_PLAN_FRIDGE_PREPLACE,
  },
  {
    id: "fridge-two-robot",
    label: "Two-robot fridge plan",
    plan: SAMPLE_PLAN_FRIDGE_TWO_ROBOT,
  },
  {
    id: "horizontal-pick",
    label: "Horizontal pick plan",
    plan: SAMPLE_PLAN_HORIZONTAL_PICK,
  },
] as const;
type CompilePlanOptionId = (typeof COMPILE_PLAN_OPTIONS)[number]["id"];

/** One skill placed on a robot's timeline (start time is derived on layout). */
type TimelineSkill = { key: string; skill: string; track: SkillTrack; duration: number };

/** One skill entry as served by GET /api/skills (from skills_manifest.json). */
type SkillManifestEntry = {
  name: string;
  facility: string | null;
  robots: string[];
  tracks: Record<string, string>;
  precondition?: { facility_state?: string };
  effect?: { facility_state?: string };
  duration: number;
};

const defaultTrajectoryPath = "/trajectories/arm_c_pick_red_physical.json";

type PlaybackFrame = TrajectoryFrame & { phase?: string };
type TrajectoryListResponse = {
  ok: boolean;
  trajectories?: string[];
  error?: string;
};

type CameraExportPayload = {
  format: "mujoco-react-camera-preset-v1";
  scene: string;
  camera: {
    position: number[];
    target: number[];
    up: number[];
    fov: number | null;
  };
};

/**
 * Keep torque-controlled Panda arms and torso columns at their current pose
 * while leaving free objects under normal gravity for DragInteraction.
 */
function DebugRobotGravityCompensation({
  apiRef,
}: {
  apiRef: RefObject<MujocoSimAPI | null>;
}) {
  const compensatedDofsRef = useRef<number[]>([]);

  useBeforePhysicsStep(({ data }) => {
    if (compensatedDofsRef.current.length === 0) {
      const api = apiRef.current;
      if (!api) return;
      compensatedDofsRef.current = api
        .getJoints()
        .filter(
          ({ name }) =>
            /^robot\d+_joint[1-7]$/.test(name) ||
            /^mobilebase\d+_joint_torso_height$/.test(name),
        )
        .map(({ dofAdr }) => dofAdr);
    }
    for (const dof of compensatedDofsRef.current) {
      data.qfrc_applied[dof] += data.qfrc_bias[dof];
    }
  });

  return null;
}

export default function DebugApp() {
  const [status, setStatus] = useState("loading");
  const [error, setError] = useState<string | null>(null);
  const [resourceCheck, setResourceCheck] = useState<string[]>([]);
  const [trajectory, setTrajectory] = useState<TrajectoryFrame[]>([]);
  const [trajectoryStatus, setTrajectoryStatus] = useState("loading");
  const [trajectoryPath, setTrajectoryPath] = useState(defaultTrajectoryPath);
  const [trajectoryFiles, setTrajectoryFiles] = useState<string[]>([]);
  const [phase, setPhase] = useState("ready");
  const [sceneConfig, setSceneConfig] = useState<SceneConfig>(defaultSceneConfig);
  const [sceneSession, setSceneSession] = useState<SceneSession | null>(null);
  const [sceneInput, setSceneInput] = useState(defaultScenePath);
  const [sceneStatus, setSceneStatus] = useState("Select or type a scene");
  const [sceneConfirmed, setSceneConfirmed] = useState(false);
  const [sceneLoadRevision, setSceneLoadRevision] = useState(0);
  const [livePhysics, setLivePhysics] = useState(true);
  const [selectedBodyId, setSelectedBodyId] = useState<number | null>(null);
  const [selectedBodyName, setSelectedBodyName] = useState<string | null>(null);
  const [manifest, setManifest] = useState<SceneManifest | null>(null);
  const timelineRobots = useMemo(() => robotIdsFromManifest(manifest), [manifest]);
  const bodyIndex = useMemo(
    () => (manifest ? buildBodyIndex(manifest) : new Map()),
    [manifest],
  );
  const [pinMode, setPinMode] = useState(false);
  const [debugPins, setDebugPins] = useState<DebugPin[]>([]);
  const [pinStatus, setPinStatus] = useState("pin mode off");
  const [cameraExportStatus, setCameraExportStatus] = useState("adjust the view, then export");
  const [cameraExport, setCameraExport] = useState<CameraExportPayload | null>(null);
  const [pinDragging, setPinDragging] = useState(false);
  const scenePresentation = useMemo(
    () => scenePresentationFor(sceneConfig.sceneFile),
    [sceneConfig.sceneFile],
  );
  const [debugPanelCollapsed, setDebugPanelCollapsed] = useState(false);
  const [previewStandoff, setPreviewStandoff] = useState<{
    robot: string;
    xy: [number, number];
  } | null>(null);
  // Sim API captured on canvas ready; drives keyframe + channel-track playback.
  const apiRef = useRef<MujocoSimAPI | null>(null);
  const schedulePlayerRef = useRef<SchedulePlayerHandle | null>(null);
  const orbitControlsRef = useRef<ComponentRef<typeof OrbitControls> | null>(null);
  const [skills, setSkills] = useState<SkillManifestEntry[]>([]);
  const [skillsBase, setSkillsBase] = useState("");
  const [selectedRobot, setSelectedRobot] = useState("robot0");
  const [activeSkillTrack, setActiveSkillTrack] = useState<SkillTrack | null>(null);
  const [skillStatus, setSkillStatus] = useState("no skill loaded");
  const [skillPlaying, setSkillPlaying] = useState(false);
  // Manifest-driven multi-robot timeline (multi-track schedule).
  const [timeline, setTimeline] = useState<Record<string, TimelineSkill[]>>({});
  const [schedulePlaying, setSchedulePlaying] = useState(false);
  const [scheduleSpeed, setScheduleSpeed] = useState(1);
  const [scheduleEpoch, setScheduleEpoch] = useState(0);
  const [scheduleTime, setScheduleTime] = useState({ t: 0, total: 0 });
  const keyCounterRef = useRef(0);
  // --- P0 verification harness: compile_plan pipeline (temporary) ----------
  // Feeds compile_plan's ScheduledItems (with backend-resolved ABSOLUTE starts)
  // straight into SchedulePlayer, bypassing the manual timeline's sequential
  // start recompute. Safe to delete once the scene-page UI consumes the pipeline.
  const [compiledItems, setCompiledItems] = useState<ScheduledItem[]>([]);
  const [compileWarnings, setCompileWarnings] = useState<string[]>([]);
  const [compileStatus, setCompileStatus] = useState("sample plan not compiled");
  const [selectedCompilePlanId, setSelectedCompilePlanId] =
    useState<CompilePlanOptionId>("sample");
  const [compiledResponse, setCompiledResponse] = useState<CompileResponse | null>(null);
  const [standoffExport, setStandoffExport] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    const urls = [`${sceneConfig.src}${sceneConfig.sceneFile}`];

    void Promise.all(
      urls.map(async (url) => {
        try {
          const response = await fetch(url);
          return `${response.ok ? "OK" : "FAIL"} ${response.status} ${url}`;
        } catch (err) {
          return `FAIL ${url}: ${err instanceof Error ? err.message : String(err)}`;
        }
      }),
    ).then(setResourceCheck);
  }, [sceneConfig]);

  const loadTrajectory = (path: string) => {
    const normalizedPath = path.startsWith("/") ? path : `/${path}`;
    setTrajectoryStatus(`loading ${normalizedPath}`);
    fetch(`${normalizedPath}?t=${Date.now()}`)
      .then((response) => {
        if (!response.ok) {
          throw new Error(`trajectory fetch failed: ${response.status}`);
        }
        return response.json() as Promise<TrajectoryFrame[]>;
      })
      .then((frames) => {
        setActiveSkillTrack(null); // legacy full-frame playback takes over
        setSkillPlaying(false);
        setTrajectory(frames);
        setTrajectoryPath(normalizedPath);
        setPhase("ready");
        setTrajectoryStatus(`${frames.length} frames from ${normalizedPath}`);
      })
      .catch((err) => {
        setTrajectory([]);
        setTrajectoryStatus(err instanceof Error ? err.message : String(err));
      });
  };

  const refreshTrajectoryFiles = () => {
    fetch(`${backendUrl}/api/trajectories?t=${Date.now()}`)
      .then((response) => {
        if (!response.ok) {
          throw new Error(`trajectory list failed: ${response.status}`);
        }
        return response.json() as Promise<TrajectoryListResponse>;
      })
      .then((payload) => {
        if (!payload.ok) {
          throw new Error(payload.error || "trajectory list failed");
        }
        setTrajectoryFiles(payload.trajectories || []);
      })
      .catch((err) => {
        setTrajectoryStatus(err instanceof Error ? err.message : String(err));
      });
  };

  const fetchSkills = () => {
    fetch(`${backendUrl}/api/skills?t=${Date.now()}`)
      .then((response) => response.json())
      .then((payload) => {
        if (!payload.ok) {
          setSkills([]);
          setSkillsBase("");
          setSkillStatus(payload.reason || "no skills for current scene");
          return;
        }
        setSkills(payload.manifest.skills || []);
        setSkillsBase(payload.base);
        setSkillStatus(
          `${payload.manifest.skills?.length ?? 0} skills for ${payload.base}`,
        );
      })
      .catch((err) =>
        setSkillStatus(err instanceof Error ? err.message : String(err)),
      );
  };

  const fetchSkillTrack = (skillName: string, robot: string): Promise<SkillTrack> => {
    const entry = skills.find((s) => s.name === skillName);
    if (!entry) return Promise.reject(new Error(`unknown skill: ${skillName}`));
    const rel = entry.tracks[robot];
    if (!rel) return Promise.reject(new Error(`${skillName} has no track for ${robot}`));
    const url = `/trajectories/${skillsBase}/${rel}`;
    return fetch(`${url}?t=${Date.now()}`).then((response) => {
      if (!response.ok) throw new Error(`skill fetch failed: ${response.status}`);
      return response.json() as Promise<SkillTrack>;
    });
  };

  const loadSkill = (skillName: string, robot: string) => {
    setSkillStatus(`loading ${skillName} (${robot})`);
    fetchSkillTrack(skillName, robot)
      .then((skillTrack) => {
        setTrajectory([]); // stop the legacy full-frame player
        setActiveSkillTrack(skillTrack);
        setSkillPlaying(true);
      })
      .catch((err) => {
        setActiveSkillTrack(null);
        setSkillStatus(err instanceof Error ? err.message : String(err));
      });
  };

  useEffect(() => {
    setTimeline((previous) => Object.fromEntries(
      timelineRobots.map((robot) => [robot, previous[robot] ?? []]),
    ));
    if (timelineRobots.length > 0 && !timelineRobots.includes(selectedRobot)) {
      setSelectedRobot(timelineRobots[0]);
    }
  }, [timelineRobots, selectedRobot]);

  // --- manifest-driven timeline (multi-track schedule) -------------------
  const addToTimeline = (robot: string, skillName: string) => {
    fetchSkillTrack(skillName, robot)
      .then((track) => {
        const key = `t${keyCounterRef.current++}`;
        setTimeline((prev) => ({
          ...prev,
          [robot]: [
            ...(prev[robot] ?? []),
            { key, skill: skillName, track, duration: track.meta.duration },
          ],
        }));
        // the schedule owns the viewer once it has items
        setActiveSkillTrack(null);
        setTrajectory([]);
        setScheduleEpoch((e) => e + 1);
      })
      .catch((err) => setSkillStatus(err instanceof Error ? err.message : String(err)));
  };

  const removeFromTimeline = (robot: string, key: string) => {
    setSchedulePlaying(false);
    setTimeline((prev) => ({
      ...prev,
      [robot]: (prev[robot] ?? []).filter((item) => item.key !== key),
    }));
    setScheduleEpoch((e) => e + 1);
  };

  const clearTimeline = () => {
    setSchedulePlaying(false);
    setTimeline(Object.fromEntries(timelineRobots.map((robot) => [robot, []])));
    setScheduleEpoch((e) => e + 1);
    const api = apiRef.current;
    if (api?.getKeyframeNames().includes(initialStateKeyframe)) {
      api.applyKeyframe(initialStateKeyframe);
    }
  };

  const resetSchedule = () => {
    if (orbitControlsRef.current) {
      resetCameraToScenePresentation(orbitControlsRef.current, scenePresentation);
    }
    setSchedulePlaying(false);
    setScheduleEpoch((e) => e + 1); // remount SchedulePlayer -> reset to start
  };

  // Flatten the timeline into absolute-start scheduled items (sequential per robot).
  const scheduleItems = useMemo<ScheduledItem[]>(() => {
    const items: ScheduledItem[] = [];
    for (const robot of timelineRobots) {
      let t = 0;
      for (const entry of timeline[robot] ?? []) {
        items.push({
          key: entry.key,
          robot,
          skill: entry.skill,
          track: entry.track,
          start: t,
          duration: entry.duration,
        });
        t += entry.duration;
      }
    }
    return items;
  }, [timeline, timelineRobots]);

  // Compiled pipeline items take over the player when present; otherwise the
  // manual timeline drives it (existing debug behaviour).
  const scheduleForPlayer = compiledItems.length > 0 ? compiledItems : scheduleItems;

  const runCompile = (label: string, plan: AuthoredPlan) => {
    setCompileStatus(`compiling ${label}…`);
    setSchedulePlaying(false);
    compileAndLoad(plan)
      .then(({ items, warnings, response }) => {
        setTimeline(Object.fromEntries(timelineRobots.map((robot) => [robot, []]))); // manual timeline steps aside
        setActiveSkillTrack(null);
        setTrajectory([]);
        setCompiledItems(items);
        setCompiledResponse(response);
        setStandoffExport(null);
        setPreviewStandoff(null);
        setCompileWarnings(warnings);
        setScheduleEpoch((e) => e + 1);
        setCompileStatus(`compiled ${label}: ${items.length} items, ${warnings.length} warning(s)`);
      })
      .catch((err) => {
        // A failed compile must not leave the viewer at the previous compiled
        // plan's terminal pose: that looks like a skipped sequence on retry.
        setSchedulePlaying(false);
        setCompiledItems([]);
        setCompiledResponse(null);
        setStandoffExport(null);
        setPreviewStandoff(null);
        setCompileWarnings([]);
        setActiveSkillTrack(null);
        setTrajectory([]);
        const api = apiRef.current;
        if (api?.getKeyframeNames().includes(initialStateKeyframe)) {
          api.applyKeyframe(initialStateKeyframe);
        }
        setCompileStatus(err instanceof Error ? err.message : String(err));
      });
  };

  const compileSelectedPlan = () => {
    const selected =
      COMPILE_PLAN_OPTIONS.find((option) => option.id === selectedCompilePlanId) ??
      COMPILE_PLAN_OPTIONS[0];
    runCompile(selected.label.toLowerCase(), selected.plan);
  };

  const clearCompiledPlan = () => {
    setSchedulePlaying(false);
    setCompiledItems([]);
    setCompiledResponse(null);
    setStandoffExport(null);
    setPreviewStandoff(null);
    setCompileWarnings([]);
    setScheduleEpoch((e) => e + 1);
    setCompileStatus("sample plan not compiled");
    const api = apiRef.current;
    if (api?.getKeyframeNames().includes(initialStateKeyframe)) {
      api.applyKeyframe(initialStateKeyframe);
    }
  };

  const previewPrePlaceStandoff = () => {
    if (!compiledResponse || compiledItems.length === 0) return;
    const candidates = Object.entries(compiledResponse.completed).flatMap(([robot, steps]) =>
      steps
        .filter((step) => step.op === "navigate" && step.target === "fridge")
        .map((step) => ({ robot, step })),
    );
    const selected = candidates.at(-1);
    if (!selected) {
      setCompileStatus("compiled plan has no navigate-to-fridge standoff");
      return;
    }
    const total = compiledItems.reduce(
      (end, item) => Math.max(end, item.start + item.duration),
      0,
    );
    setSchedulePlaying(false);
    schedulePlayerRef.current?.seek(total);
    const standoff = Array.isArray(selected.step.standoff)
      ? (selected.step.standoff as [number, number])
      : null;
    if (standoff) {
      setPreviewStandoff({ robot: selected.robot, xy: standoff });
      const controls = orbitControlsRef.current;
      if (controls) {
        controls.target.set(standoff[0], standoff[1], 0.85);
        controls.update();
      }
    }
    const terminalItem = compiledItems.reduce((latest, item) =>
      item.start + item.duration > latest.start + latest.duration ? item : latest,
    );
    const robotIndex = selected.robot.replace("robot", "");
    const baseJointQpos = Object.fromEntries(
      Object.entries(terminalItem.track.channels)
        .filter(([name]) => name.startsWith(`mobilebase${robotIndex}_joint_`))
        .map(([name, frames]) => [name, frames.at(-1) ?? null]),
    );
    const payload = {
      scene: sceneSession?.sceneFile || sceneConfig.sceneFile,
      pose: "immediately_before_fridge_place",
      robot: selected.robot,
      step_id: selected.step.id ?? null,
      target: selected.step.target,
      standoff,
      base_joint_qpos: baseJointQpos,
      route: selected.step.route ?? null,
      schedule_time: Number(total.toFixed(4)),
    };
    setStandoffExport(payload);
    setCompileStatus(
      `showing ${selected.robot} pre-place standoff ${JSON.stringify(selected.step.standoff ?? null)}`,
    );
  };

  const copyStandoffExport = () => {
    if (!standoffExport) return;
    navigator.clipboard
      .writeText(JSON.stringify(standoffExport, null, 2))
      .then(() => setCompileStatus("copied pre-place standoff JSON"))
      .catch((err) =>
        setCompileStatus(`copy failed: ${err instanceof Error ? err.message : String(err)}`),
      );
  };

  const exportCurrentCamera = () => {
    const controls = orbitControlsRef.current;
    if (!controls) {
      setCameraExportStatus("camera controls are not ready");
      return;
    }

    const round = (value: number) => Number(value.toFixed(6));
    const vector = (values: number[]) => values.slice(0, 3).map(round);
    const camera = controls.object;
    const scene = sceneSession?.sceneFile || sceneConfig.sceneFile;
    const payload: CameraExportPayload = {
      format: "mujoco-react-camera-preset-v1",
      scene,
      camera: {
        position: vector(camera.position.toArray()),
        target: vector(controls.target.toArray()),
        up: vector(camera.up.toArray()),
        fov: "fov" in camera && typeof camera.fov === "number" ? round(camera.fov) : null,
      },
    };
    setCameraExport(payload);
    const sceneName = scene.split(/[\\/]/).at(-1)?.replace(/\.[^.]+$/, "") || "scene";
    const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${sceneName}.camera.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
    setCameraExportStatus(`exported ${link.download}`);
  };

  const clearSkill = () => {
    setActiveSkillTrack(null);
    setSkillPlaying(false);
    const api = apiRef.current;
    if (api?.getKeyframeNames().includes(initialStateKeyframe)) {
      api.applyKeyframe(initialStateKeyframe);
    }
  };

  useEffect(() => {
    refreshTrajectoryFiles();
  }, []);

  // Reload the skill list whenever the active scene changes.
  useEffect(() => {
    setActiveSkillTrack(null);
    setSkillPlaying(false);
    fetchSkills();
    setManifest(null);
    setDebugPins([]);
    setPinMode(false);
    setPinStatus("pin mode off");
    loadSceneManifest()
      .then(setManifest)
      .catch((err) =>
        setPinStatus(`manifest unavailable: ${err instanceof Error ? err.message : String(err)}`),
      );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sceneConfig]);

  const handleScenePin = (pick: ScenePick) => {
    const pin: DebugPin = {
      id: crypto.randomUUID(),
      bodyId: pick.bodyId,
      bodyName: pick.bodyName,
      xyz: pick.point,
      attribution: pick.attribution,
    };
    setDebugPins((pins) => [...pins, pin]);
    setSelectedBodyId(pick.bodyId);
    setSelectedBodyName(pick.bodyName);
    setPinStatus(`added ${debugPinLabel(pin)} at ${pick.point.map((v) => v.toFixed(4)).join(", ")}`);
  };

  const updateDebugPin = (pinId: string, xy: [number, number]) => {
    setDebugPins((pins) =>
      pins.map((pin) =>
        pin.id === pinId ? { ...pin, xyz: [xy[0], xy[1], pin.xyz[2]] } : pin,
      ),
    );
  };

  const copyDebugPins = () => {
    const sceneFile = sceneSession?.sceneFile || sceneConfig.sceneFile;
    const text = JSON.stringify(serializeDebugPins(sceneFile, debugPins), null, 2);
    navigator.clipboard
      .writeText(text)
      .then(() => setPinStatus(`copied ${debugPins.length} pin(s) as JSON`))
      .catch((err) =>
        setPinStatus(`copy failed: ${err instanceof Error ? err.message : String(err)}`),
      );
  };

  useEffect(() => {
    fetch(`${backendUrl}/api/scene/current?t=${Date.now()}`)
      .then((response) => {
        if (!response.ok) {
          throw new Error(`scene session fetch failed: ${response.status}`);
        }
        return response.json() as Promise<SceneSession>;
      })
      .then((session) => {
        if (!session.ok) {
          throw new Error(session.error || "scene session fetch failed");
        }
        setSceneSession(session);
        setSceneInput(session.sceneFile);
        setSceneConfig({ src: session.src, sceneFile: session.sceneFile });
        setSceneStatus("Select or type a scene");
      })
      .catch((err) => {
        setSceneStatus(
          `using default scene path; backend current-scene lookup failed: ${
            err instanceof Error ? err.message : String(err)
          }`,
        );
      });
  }, []);

  const confirmScene = () => {
    setSceneStatus("opening scene");
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
        setSceneSession(session);
        setSceneInput(session.sceneFile);
        setSceneConfig({ src: session.src, sceneFile: session.sceneFile });
        const compileNote = session.compile
          ? session.compile.cached
            ? " (using cached .mjb)"
            : " (compiled .mjb)"
          : "";
        const viewerOnlyNote = session.skillRuntime && !session.skillRuntime.available
          ? ` (viewer only: missing ${session.skillRuntime.missing
              .map((artifact) => artifact.kind)
              .join(", ")})`
          : "";
        setSceneStatus(`opened ${session.sceneFile}${compileNote}${viewerOnlyNote}`);
        setStatus("loading");
        setError(null);
        setTrajectory([]);
        setTrajectoryStatus("no trajectory loaded for current scene");
        setPhase("ready");
        setSelectedBodyId(null);
        setSelectedBodyName(null);
        setSceneConfirmed(true);
        setLivePhysics(true);
        setSceneLoadRevision((revision) => revision + 1);
      })
      .catch((err) => {
        setSceneStatus(err instanceof Error ? err.message : String(err));
      });
  };

  const handleError = (err: Error) => {
    setStatus("error");
    setError(err.message || String(err));
    console.error("[mujoco-react viewer]", err);
  };

  const applyDebugInitialState = (api = apiRef.current) => {
    if (!api) return;
    api.reset();
    if (api.getKeyframeNames().includes(initialStateKeyframe)) {
      api.applyKeyframe(initialStateKeyframe);
    }
  };

  const reloadDebugScene = () => {
    setLivePhysics(true);
    apiRef.current = null;
    setStatus("loading");
    setError(null);
    setSceneLoadRevision((revision) => revision + 1);
  };

  if (!sceneConfirmed) {
    return (
      <div className="scene-launcher">
        <section className="scene-launcher-panel">
          <div className="scene-launcher-nav">
            <Link to="/">Scene UI</Link>
          </div>
          <div>
            <h1>Open Scene (Debug)</h1>
            <p>
              Enter a scene path relative to public/. XML scenes are compiled to .mjb on the
              backend before loading in the browser.
            </p>
          </div>
          <label htmlFor="initial-scene-path">Scene path (relative to public/)</label>
          <div className="scene-launcher-row">
            <ScenePathPicker
              id="initial-scene-path"
              ariaLabel="Scene path (relative to public/)"
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
          {sceneSession ? (
            <div className="scene-launcher-meta">
              Manifest: {sceneSession.manifest.exists ? "exists" : "missing"} | Task plan:{" "}
              {sceneSession.taskPlan.exists ? "exists" : "missing"}
            </div>
          ) : null}
        </section>
      </div>
    );
  }

  const selectedBody =
    selectedBodyId !== null && selectedBodyName
      ? { bodyId: selectedBodyId, name: selectedBodyName }
      : null;

  return (
    <MujocoProvider
      mtWasmUrl={mtMujocoWasmUrl}
      threadedLoader={threadedMujocoLoader}
      wasmVariant="auto"
      timeout={90000}
      onError={handleError}
    >
      <div className="app-shell">
        <main className="viewer-pane">
          <MujocoCanvas
            key={`${sceneConfig.src}${sceneConfig.sceneFile}:${sceneLoadRevision}`}
            config={sceneConfig}
            camera={scenePresentation.camera}
            // Live physics stays available for Ctrl/Cmd+drag. Robot-only
            // gravity compensation below prevents the torque-controlled arms
            // from sagging without making free objects float.
            paused={!livePhysics || activeSkillTrack !== null || scheduleForPlayer.length > 0}
            onReady={({ api }) => {
              apiRef.current = api;
              setStatus("ready");
              setError(null);
              // Apply the scene's initial session state (cabinet left open,
              // etc.) when declared; scenes without this keyframe are untouched.
              applyDebugInitialState(api);
            }}
            onError={handleError}
            onSelection={({ bodyId, name }) => {
              if (bodyId === selectedBodyId) {
                setSelectedBodyId(null);
                setSelectedBodyName(null);
                return;
              }
              setSelectedBodyId(bodyId);
              setSelectedBodyName(name || `(unnamed body ${bodyId})`);
            }}
            style={{ width: "100%", height: "100%" }}
          >
            <SelectionHighlight bodyId={selectedBodyId} />
            <DebugRobotGravityCompensation apiRef={apiRef} />
            <ScenePickController
              bodyIndex={bodyIndex}
              enabled={pinMode}
              onPick={handleScenePin}
            />
            <PlanOverlay
              markers={
                previewStandoff
                  ? [
                      {
                        id: "debug-pre-place-standoff",
                        robot: previewStandoff.robot,
                        op: "navigate",
                        standoff: previewStandoff.xy,
                      },
                    ]
                  : []
              }
              pins={debugPins.map((pin) => ({ id: pin.id, at: pin.xyz }))}
              onDragPin={updateDebugPin}
              onDragStateChange={setPinDragging}
            />
            <DragInteraction />
            {scheduleForPlayer.length > 0 ? (
              <SchedulePlayer
                ref={schedulePlayerRef}
                key={`schedule-${scheduleEpoch}`}
                apiRef={apiRef}
                items={scheduleForPlayer}
                playing={schedulePlaying}
                speed={scheduleSpeed}
                initialKeyframe={initialStateKeyframe}
                onTime={(t, total) => setScheduleTime({ t, total })}
                onComplete={() => setSchedulePlaying(false)}
              />
            ) : null}
            {scheduleForPlayer.length === 0 && activeSkillTrack ? (
              <ChannelTrackPlayer
                apiRef={apiRef}
                track={activeSkillTrack}
                initialKeyframe={initialStateKeyframe}
                playing={skillPlaying}
                loop
                onPhase={setPhase}
                onStatus={setSkillStatus}
              />
            ) : null}
            {scheduleForPlayer.length === 0 && !activeSkillTrack && trajectory.length > 0 ? (
              <TrajectoryPlayer
                trajectory={trajectory}
                fps={30}
                speed={1}
                loop
                playing={status === "ready"}
                mode="kinematic"
                onFrame={({ frame }) => {
                  const playbackFrame = frame as PlaybackFrame | number[] | undefined;
                  if (
                    playbackFrame &&
                    !Array.isArray(playbackFrame) &&
                    typeof playbackFrame.phase === "string"
                  ) {
                    setPhase(playbackFrame.phase);
                  }
                }}
              />
            ) : null}
            <OrbitControls
              ref={orbitControlsRef}
              makeDefault
              enabled={!pinDragging}
              target={scenePresentation.target}
            />
            <ambientLight intensity={0.7} />
            <directionalLight position={[1, 2, 5]} intensity={1.2} />
          </MujocoCanvas>
          <div className={`status-panel${debugPanelCollapsed ? " is-collapsed" : ""}`}>
            <button
              type="button"
              className="debug-panel-toggle"
              onClick={() => setDebugPanelCollapsed((collapsed) => !collapsed)}
            >
              {debugPanelCollapsed ? "Show debug controls" : "Hide debug controls"}
            </button>
            <div className="scene-status-nav">
              <Link to="/">Scene UI</Link>
            </div>
            <strong>MuJoCo React: {status}</strong>
            <div className="scene-controls">
              <div>Scene: {sceneSession?.sceneFile || sceneConfig.sceneFile}</div>
              {sceneSession ? (
                <div>
                  Manifest: {sceneSession.manifest.exists ? "exists" : "missing"} | Task plan:{" "}
                  {sceneSession.taskPlan.exists ? "exists" : "missing"}
                </div>
              ) : null}
              <div className="camera-export-row">
                <button type="button" onClick={reloadDebugScene} disabled={status === "loading"}>
                  Reload Scene
                </button>
                <button
                  type="button"
                  aria-pressed={livePhysics}
                  className={livePhysics ? "is-active" : ""}
                  onClick={() => setLivePhysics((enabled) => !enabled)}
                  disabled={status !== "ready"}
                >
                  {livePhysics ? "Pause Physics" : "Run Physics"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setLivePhysics(true);
                    applyDebugInitialState();
                  }}
                  disabled={status !== "ready"}
                >
                  Reset Initial State
                </button>
                <button type="button" onClick={exportCurrentCamera} disabled={status !== "ready"}>
                  Export Camera JSON
                </button>
                <span>{cameraExportStatus}</span>
              </div>
              {cameraExport ? (
                <pre className="camera-export-preview">{JSON.stringify(cameraExport, null, 2)}</pre>
              ) : null}
            </div>
            <div>
              Selected:{" "}
              {selectedBodyName
                ? `${selectedBodyName} (id ${selectedBodyId}) — double-click again to clear`
                : "none — double-click a body to select"}
            </div>
            <div className="debug-pin-panel">
              <div className="debug-pin-head">
                <strong>Scene pins</strong>
                <button
                  type="button"
                  className={pinMode ? "is-active" : ""}
                  aria-pressed={pinMode}
                  onClick={() => {
                    setPinMode((enabled) => {
                      const next = !enabled;
                      setPinStatus(next ? "pin mode on — double-click a surface or object" : "pin mode off");
                      return next;
                    });
                  }}
                >
                  {pinMode ? "Stop pinning" : "Add pin"}
                </button>
                <button type="button" onClick={copyDebugPins} disabled={debugPins.length === 0}>
                  Copy JSON
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setDebugPins([]);
                    setPinStatus("pins cleared");
                  }}
                  disabled={debugPins.length === 0}
                >
                  Clear
                </button>
              </div>
              <div className="debug-pin-status">{pinStatus}</div>
              {debugPins.length > 0 ? (
                <div className="debug-pin-list">
                  {debugPins.map((pin, index) => (
                    <div className="debug-pin-row" key={pin.id}>
                      <code>{debugPinHandle(index)}</code>
                      <span className="debug-pin-entity" title={pin.bodyName}>
                        {debugPinLabel(pin)}
                      </span>
                      <code>{pin.xyz.map((value) => value.toFixed(4)).join(", ")}</code>
                      <button
                        type="button"
                        aria-label={`Remove ${debugPinHandle(index)}`}
                        onClick={() => setDebugPins((pins) => pins.filter((item) => item.id !== pin.id))}
                      >
                        ×
                      </button>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
            <div>Trajectory: {trajectoryStatus}</div>
            <div>Phase: {phase}</div>
            <div className="trajectory-controls">
              <select
                value={trajectoryPath}
                onChange={(event) => loadTrajectory(event.target.value)}
              >
                <option value={trajectoryPath}>{trajectoryPath}</option>
                {trajectoryFiles
                  .filter((file) => file !== trajectoryPath)
                  .map((file) => (
                    <option key={file} value={file}>
                      {file}
                    </option>
                  ))}
              </select>
              <input
                value={trajectoryPath}
                onChange={(event) => setTrajectoryPath(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    loadTrajectory(trajectoryPath);
                  }
                }}
              />
              <button type="button" onClick={() => loadTrajectory(trajectoryPath)}>
                Reload
              </button>
              <button type="button" onClick={refreshTrajectoryFiles}>
                Refresh list
              </button>
            </div>
            <div>Skill: {skillStatus}</div>
            <div className="trajectory-controls skill-controls">
              <select
                value={selectedRobot}
                onChange={(event) => setSelectedRobot(event.target.value)}
              >
                {timelineRobots.map((robot) => (
                  <option key={robot} value={robot}>{robot}</option>
                ))}
              </select>
              <select
                value={activeSkillTrack?.meta.skill ?? ""}
                onChange={(event) => {
                  if (event.target.value) loadSkill(event.target.value, selectedRobot);
                }}
              >
                <option value="">— select skill —</option>
                {skills.map((skill) => (
                  <option key={skill.name} value={skill.name}>
                    {skill.name}
                    {skill.facility ? ` (${skill.facility})` : ""}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => setSkillPlaying((playing) => !playing)}
                disabled={!activeSkillTrack}
              >
                {skillPlaying ? "Pause" : "Play"}
              </button>
              <button type="button" onClick={clearSkill} disabled={!activeSkillTrack}>
                Clear
              </button>
              <button type="button" onClick={fetchSkills}>
                Refresh skills
              </button>
            </div>
            <div className="timeline-panel">
              <div className="timeline-head">
                <strong>compile_plan harness (P0 verify)</strong>
                <label>
                  Plan{" "}
                  <select
                    aria-label="Compile plan"
                    value={selectedCompilePlanId}
                    onChange={(event) =>
                      setSelectedCompilePlanId(event.target.value as CompilePlanOptionId)
                    }
                  >
                    {COMPILE_PLAN_OPTIONS.map((option) => (
                      <option key={option.id} value={option.id}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
                <button type="button" onClick={compileSelectedPlan}>
                  Compile selected plan
                </button>
                <button
                  type="button"
                  onClick={previewPrePlaceStandoff}
                  disabled={compiledItems.length === 0 || !compiledResponse}
                >
                  Preview pre-place standoff
                </button>
                <button
                  type="button"
                  onClick={copyStandoffExport}
                  disabled={!standoffExport}
                >
                  Copy standoff JSON
                </button>
                <label>
                  Speed{" "}
                  <select
                    aria-label="Plan playback speed"
                    value={scheduleSpeed}
                    onChange={(event) => setScheduleSpeed(Number(event.target.value))}
                  >
                    <option value={0.25}>0.25×</option>
                    <option value={0.5}>0.5×</option>
                    <option value={1}>1×</option>
                    <option value={2}>2×</option>
                    <option value={4}>4×</option>
                    <option value={8}>8×</option>
                  </select>
                </label>
                <button
                  type="button"
                  onClick={() => setSchedulePlaying((p) => !p)}
                  disabled={compiledItems.length === 0}
                >
                  {schedulePlaying ? "Pause" : "Play"}
                </button>
                <button
                  type="button"
                  onClick={resetSchedule}
                  disabled={compiledItems.length === 0}
                >
                  Reset
                </button>
                <button
                  type="button"
                  onClick={clearCompiledPlan}
                  disabled={compiledItems.length === 0}
                >
                  Clear
                </button>
              </div>
              <div className="compile-status">{compileStatus}</div>
              {standoffExport ? (
                <pre className="standoff-export">{JSON.stringify(standoffExport, null, 2)}</pre>
              ) : null}
              {compileWarnings.length > 0 ? (
                <ul className="compile-warnings">
                  {compileWarnings.map((w) => (
                    <li key={w}>⚠ {w}</li>
                  ))}
                </ul>
              ) : null}
            </div>
            <div className="timeline-panel">
              <div className="timeline-head">
                <strong>Timeline (2-robot schedule)</strong>
                <button
                  type="button"
                  onClick={() => setSchedulePlaying((p) => !p)}
                  disabled={scheduleItems.length === 0}
                >
                  {schedulePlaying ? "Pause" : "Play"}
                </button>
                <button
                  type="button"
                  onClick={resetSchedule}
                  disabled={scheduleItems.length === 0}
                >
                  Reset
                </button>
                <button
                  type="button"
                  onClick={clearTimeline}
                  disabled={scheduleItems.length === 0}
                >
                  Clear
                </button>
                <span className="timeline-clock">
                  {scheduleTime.t.toFixed(1)} / {scheduleTime.total.toFixed(1)}s
                </span>
              </div>
              {timelineRobots.map((robot) => {
                let acc = 0;
                const pxPerSec = 22;
                return (
                  <div key={robot} className="timeline-row">
                    <span className="timeline-label">{robot}</span>
                    <div className="timeline-lane">
                      {(timeline[robot] ?? []).map((item) => {
                        const left = acc * pxPerSec;
                        const width = Math.max(item.duration * pxPerSec, 40);
                        acc += item.duration;
                        return (
                          <div
                            key={item.key}
                            className="timeline-chip"
                            style={{ left, width }}
                            title={`${item.skill} (${item.duration.toFixed(1)}s)`}
                          >
                            <span className="timeline-chip-name">{item.skill}</span>
                            <button
                              type="button"
                              className="timeline-chip-x"
                              onClick={() => removeFromTimeline(robot, item.key)}
                            >
                              ×
                            </button>
                          </div>
                        );
                      })}
                      {schedulePlaying || scheduleTime.t > 0 ? (
                        <div
                          className="timeline-playhead"
                          style={{ left: scheduleTime.t * pxPerSec }}
                        />
                      ) : null}
                    </div>
                    <select
                      value=""
                      onChange={(event) => {
                        if (event.target.value) addToTimeline(robot, event.target.value);
                        event.currentTarget.selectedIndex = 0;
                      }}
                    >
                      <option value="">+ add skill</option>
                      {skills
                        .filter((skill) => skill.tracks[robot])
                        .map((skill) => (
                          <option key={skill.name} value={skill.name}>
                            {skill.name}
                          </option>
                        ))}
                    </select>
                  </div>
                );
              })}
            </div>
            {error ? <pre>{error}</pre> : null}
            <ul>
              {resourceCheck.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </div>
        </main>
        <RightWorkspace
          selectedBody={selectedBody}
          sceneFile={sceneSession?.sceneFile || sceneConfig.sceneFile}
        />
      </div>
    </MujocoProvider>
  );
}
