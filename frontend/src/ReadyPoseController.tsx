import { useEffect, useRef } from "react";
import { useBeforePhysicsStep, useMujoco } from "mujoco-react";
import { discoverReadyPoseJoints, readyPoseTorque, type ReadyPoseJoint } from "./readyPoseControl";

export type ReadyPoseControllerProps = {
  enabled: boolean;
  captureRevision: number;
};

/** Holds the current pose of every complete `robotN` Panda arm while exploring. */
export function ReadyPoseController({ enabled, captureRevision }: ReadyPoseControllerProps) {
  const { api, isReady } = useMujoco();
  const jointsRef = useRef<ReadyPoseJoint[]>([]);
  const targetsRef = useRef<number[] | null>(null);
  const modelRef = useRef<object | null>(null);
  const pendingDiscoveryRef = useRef(true);
  const pendingCaptureRef = useRef(true);
  const enabledRef = useRef(enabled);
  const lastRevisionRef = useRef(captureRevision);
  const wasEnabledRef = useRef(enabled);
  const latestDataRef = useRef<{ ctrl: Float64Array } | null>(null);
  const warnedRef = useRef(new Set<string>());

  enabledRef.current = enabled;
  if (lastRevisionRef.current !== captureRevision) {
    lastRevisionRef.current = captureRevision;
    pendingDiscoveryRef.current = true;
    pendingCaptureRef.current = true;
  }
  if (enabled && !wasEnabledRef.current) pendingCaptureRef.current = true;
  wasEnabledRef.current = enabled;

  useEffect(() => {
    jointsRef.current = [];
    targetsRef.current = null;
    modelRef.current = null;
    pendingDiscoveryRef.current = true;
    pendingCaptureRef.current = true;
    if (!isReady || !api) return;
    return () => {
      const data = latestDataRef.current;
      if (!data) return;
      for (const joint of jointsRef.current) {
        if (joint.ctrlAdr < data.ctrl.length) data.ctrl[joint.ctrlAdr] = 0;
      }
    };
  }, [api, isReady]);

  useBeforePhysicsStep(({ model, data }) => {
    latestDataRef.current = data;
    if (modelRef.current !== model) {
      modelRef.current = model;
      jointsRef.current = [];
      targetsRef.current = null;
      pendingDiscoveryRef.current = true;
      pendingCaptureRef.current = true;
    }
    let joints = jointsRef.current;
    if (!enabledRef.current) {
      for (const joint of joints) {
        if (joint.ctrlAdr < data.ctrl.length) data.ctrl[joint.ctrlAdr] = 0;
      }
      targetsRef.current = null;
      pendingCaptureRef.current = true;
      return;
    }
    if (pendingDiscoveryRef.current) {
      if (!api) return;
      try {
        const discovered = discoverReadyPoseJoints(api.getActuatedJoints());
        jointsRef.current = discovered.joints;
        joints = discovered.joints;
        pendingDiscoveryRef.current = false;
        targetsRef.current = null;
        pendingCaptureRef.current = true;
        for (const warning of discovered.warnings) {
          if (!warnedRef.current.has(warning)) {
            warnedRef.current.add(warning);
            console.warn(warning);
          }
        }
      } catch (error) {
        const warning = `[ready-pose] arm discovery failed: ${error instanceof Error ? error.message : String(error)}`;
        if (!warnedRef.current.has(warning)) {
          warnedRef.current.add(warning);
          console.warn(warning);
        }
        return;
      }
    }
    if (pendingCaptureRef.current) {
      const nextTargets: number[] = [];
      for (const joint of joints) {
        if (joint.qposAdr >= data.qpos.length || !Number.isFinite(data.qpos[joint.qposAdr])) {
          targetsRef.current = null;
          return;
        }
        nextTargets.push(data.qpos[joint.qposAdr]);
      }
      targetsRef.current = nextTargets;
      pendingCaptureRef.current = false;
    }
    const targets = targetsRef.current;
    if (!targets || targets.length !== joints.length) return;
    for (let i = 0; i < joints.length; i++) {
      const joint = joints[i];
      if (joint.qposAdr >= data.qpos.length || joint.dofAdr >= data.qvel.length || joint.dofAdr >= data.qfrc_bias.length || joint.ctrlAdr >= data.ctrl.length) continue;
      data.ctrl[joint.ctrlAdr] = readyPoseTorque(
        joint,
        targets[i],
        data.qpos[joint.qposAdr],
        data.qvel[joint.dofAdr],
        data.qfrc_bias[joint.dofAdr],
      );
    }
  });

  return null;
}
