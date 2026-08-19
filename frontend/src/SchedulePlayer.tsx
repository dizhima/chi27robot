import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import type { MujocoSimAPI } from "mujoco-react";
import {
  buildChannelAddrMap,
  overlayTrackAtTime,
} from "./skillTrackPlayback";
import type { ChannelAddrMap, SkillTrack } from "./skillTrackPlayback";

/** One skill placed on a robot's timeline at an absolute start time. */
export type ScheduledItem = {
  key: string;
  robot: string;
  skill: string;
  track: SkillTrack;
  start: number;
  duration: number;
  /** Object this step actively owns/carries/places, if any. */
  object?: string | null;
  op?: string | null;
};

/**
 * Select which already-started track owns an object's freejoint at time `t`.
 * An active primary owner (pick/carry/place for that object) beats a later
 * secondary world-state channel embedded in another robot's physics track.
 */
export function selectObjectController(
  items: ScheduledItem[],
  object: string,
  t: number,
): ScheduledItem | undefined {
  const channel = `${object}_joint0`;
  const started = items.filter(
    (item) => t >= item.start && channel in item.track.channels,
  );
  const activePrimary = started.filter(
    (item) => item.object === object && t < item.start + item.duration,
  );
  return activePrimary.at(-1) ?? started.at(-1);
}

type SchedulePlayerProps = {
  apiRef: React.RefObject<MujocoSimAPI | null>;
  items: ScheduledItem[];
  playing: boolean;
  initialKeyframe?: string;
  speed?: number;
  loop?: boolean;
  onTime?: (t: number, total: number) => void;
  onComplete?: () => void;
};

export type SchedulePlayerHandle = {
  /** Reconstruct the full scene at an absolute schedule time. */
  seek: (time: number) => void;
};

/**
 * Multi-track assembly player. Owns a single global clock and, each frame,
 * overlays every scheduled item onto one shared qpos in start-time order:
 * later items win on any shared channel (so a robot's sequential skills chain
 * correctly), while different robots touch disjoint channels and thus run in
 * parallel for free. Finished items are clamped to their last frame, so their
 * effects (a closed door, the arm's end pose) persist. Physics is paused by
 * the caller (MujocoCanvas `paused`) so nothing fights the kinematic writes.
 */
export const SchedulePlayer = forwardRef<SchedulePlayerHandle, SchedulePlayerProps>(
function SchedulePlayer({
    apiRef,
    items,
    playing,
    initialKeyframe,
    speed = 1,
    loop = false,
    onTime,
    onComplete,
  }, ref) {
  const clockRef = useRef(0);
  const lastEmitRef = useRef(-1);
  const completedRef = useRef(false);

  // Items sorted by start time + a per-item channel address map.
  const sorted = useMemo(
    () => [...items].sort((a, b) => a.start - b.start),
    [items],
  );
  const objectNames = useMemo(
    () => [...new Set(items.map((item) => item.object).filter(
      (object): object is string => Boolean(object),
    ))],
    [items],
  );
  const objectChannels = useMemo(
    () => new Set(objectNames.map((object) => `${object}_joint0`)),
    [objectNames],
  );
  const addrMaps = useMemo(() => {
    const api = apiRef.current;
    const maps = new Map<string, ChannelAddrMap>();
    if (!api) return maps;
    for (const item of items) {
      maps.set(item.key, buildChannelAddrMap(
        api,
        Object.keys(item.track.channels).filter(
          (channel) => !objectChannels.has(channel),
        ),
      ));
    }
    return maps;
  }, [apiRef, items, objectChannels]);
  const objectAddrMaps = useMemo(() => {
    const api = apiRef.current;
    const maps = new Map<string, ChannelAddrMap>();
    if (!api) return maps;
    for (const object of objectNames) {
      const channel = `${object}_joint0`;
      maps.set(object, buildChannelAddrMap(api, [channel]));
    }
    return maps;
  }, [apiRef, objectNames]);

  const total = useMemo(
    () => items.reduce((m, it) => Math.max(m, it.start + it.duration), 0),
    [items],
  );

  const applyAt = (t: number) => {
    const api = apiRef.current;
    if (!api) return;
    const q = Float64Array.from(api.getQpos());
    for (const item of sorted) {
      if (t < item.start) continue;
      const localT = Math.min(t - item.start, item.duration);
      const map = addrMaps.get(item.key);
      if (map) overlayTrackAtTime(q, item.track, map, localT);
    }
    // Object freejoints are shared across robot tracks. Resolve them after
    // robot/fixture channels so a later-starting secondary physics track cannot
    // teleport an object away from the robot that is actively carrying it.
    for (const object of objectNames) {
      const item = selectObjectController(sorted, object, t);
      const map = objectAddrMaps.get(object);
      if (!item || !map) continue;
      const localT = Math.min(t - item.start, item.duration);
      overlayTrackAtTime(q, item.track, map, localT);
    }
    api.setQpos(q);
  };

  const restoreInitialState = () => {
    const api = apiRef.current;
    if (!api) return false;
    api.setQvel(new Float64Array(api.getQvel().length));
    if (initialKeyframe && api.getKeyframeNames().includes(initialKeyframe)) {
      api.applyKeyframe(initialKeyframe);
    } else {
      api.reset();
    }
    return true;
  };

  const seekTo = (requestedTime: number) => {
    const t = Math.max(0, Math.min(total, requestedTime));
    if (!restoreInitialState()) return;
    clockRef.current = t;
    lastEmitRef.current = Math.floor(t * 20);
    completedRef.current = t >= total;
    // Always rebuild from the initial state. Applying onto the current qpos
    // would leave channels from later tasks behind when scrubbing backwards.
    applyAt(t);
    onTime?.(t, total);
  };

  useImperativeHandle(ref, () => ({ seek: seekTo }));

  const resetToStart = () => {
    if (!restoreInitialState()) return;
    clockRef.current = 0;
    lastEmitRef.current = -1;
    completedRef.current = false;
    applyAt(0);
    onTime?.(0, total);
  };

  // Rebuild / reset whenever the schedule changes.
  useEffect(() => {
    resetToStart();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [addrMaps, total]);

  // Reset to the beginning each time playback (re)starts from a stopped state.
  const prevPlayingRef = useRef(false);
  useEffect(() => {
    if (playing && !prevPlayingRef.current && clockRef.current >= total) {
      resetToStart();
    }
    prevPlayingRef.current = playing;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing]);

  useFrame((_, delta) => {
    if (!playing || total <= 0) return;
    let t = clockRef.current + delta * speed;
    if (t >= total) {
      if (loop) {
        t -= total;
        const api = apiRef.current;
        if (initialKeyframe && api?.getKeyframeNames().includes(initialKeyframe)) {
          api.applyKeyframe(initialKeyframe);
        }
      } else {
        t = total;
        if (!completedRef.current) {
          completedRef.current = true;
          onComplete?.();
        }
      }
    }
    clockRef.current = t;
    applyAt(t);
    // throttle time reporting to ~20 Hz to avoid per-frame React re-renders
    const tick = Math.floor(t * 20);
    if (tick !== lastEmitRef.current) {
      lastEmitRef.current = tick;
      onTime?.(t, total);
    }
  });

  return null;
});
