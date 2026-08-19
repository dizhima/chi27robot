import { useEffect, useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import type { MujocoSimAPI } from "mujoco-react";
import {
  buildChannelAddrMap,
  overlayTrackAtTime,
  phaseAtTime,
} from "./skillTrackPlayback";
import type { SkillTrack } from "./skillTrackPlayback";

export type { SkillTrack } from "./skillTrackPlayback";

type ChannelTrackPlayerProps = {
  /** Sim API captured from MujocoCanvas onReady. */
  apiRef: React.RefObject<MujocoSimAPI | null>;
  track: SkillTrack;
  /** Keyframe applied before playback so unmanaged channels start correctly. */
  initialKeyframe?: string;
  playing: boolean;
  speed?: number;
  loop?: boolean;
  onPhase?: (phase: string) => void;
  onStatus?: (status: string) => void;
};

/**
 * Kinematic, name-addressed playback of one skill track. Each render frame it
 * samples the track by wall-clock time and overlays only the track's own
 * channels onto the live qpos (setQpos forwards internally). Physics is paused
 * via the MujocoCanvas `paused` prop while active so the sim can't fight the
 * writes. Multi-track assembly is handled by SchedulePlayer, which shares the
 * same sampling primitives.
 */
export function ChannelTrackPlayer({
  apiRef,
  track,
  initialKeyframe,
  playing,
  speed = 1,
  loop = false,
  onPhase,
  onStatus,
}: ChannelTrackPlayerProps) {
  const localTimeRef = useRef(0);
  const lastPhaseRef = useRef<string | null>(null);
  const validRef = useRef(false);

  const addrMap = useMemo(() => {
    const api = apiRef.current;
    if (!api) return new Map<string, number>();
    return buildChannelAddrMap(api, Object.keys(track.channels));
  }, [apiRef, track]);

  const apply = (t: number) => {
    const api = apiRef.current;
    if (!api || !validRef.current) return;
    const q = Float64Array.from(api.getQpos());
    overlayTrackAtTime(q, track, addrMap, t);
    api.setQpos(q);
    const phase = phaseAtTime(track, t);
    if (phase && phase !== lastPhaseRef.current) {
      lastPhaseRef.current = phase;
      onPhase?.(phase);
    }
  };

  useEffect(() => {
    const api = apiRef.current;
    if (!api) return;
    const nq = api.getQpos().length;
    if (track.meta.scene_nq !== nq) {
      validRef.current = false;
      onStatus?.(
        `track scene_nq ${track.meta.scene_nq} != loaded scene nq ${nq} — refusing to play (stale track?)`,
      );
      return;
    }
    validRef.current = true;
    api.setQvel(new Float64Array(api.getQvel().length));
    localTimeRef.current = 0;
    lastPhaseRef.current = null;
    if (initialKeyframe && api.getKeyframeNames().includes(initialKeyframe)) {
      api.applyKeyframe(initialKeyframe);
    }
    apply(0);
    onStatus?.(
      `loaded ${track.meta.skill} (robot${track.meta.robot_index}), ${track.meta.n_frames} frames, ${track.meta.duration.toFixed(1)}s`,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [track, addrMap]);

  useFrame((_, delta) => {
    if (!playing || !validRef.current) return;
    const duration = track.meta.duration;
    let t = localTimeRef.current + delta * speed;
    if (t >= duration) {
      if (loop) {
        t -= duration;
        const api = apiRef.current;
        if (initialKeyframe && api?.getKeyframeNames().includes(initialKeyframe)) {
          api.applyKeyframe(initialKeyframe);
        }
      } else {
        t = duration;
      }
    }
    localTimeRef.current = t;
    apply(t);
  });

  return null;
}
