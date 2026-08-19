import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useRef, useState, type CSSProperties } from "react";
import type { Group } from "three";
import { useMujoco } from "mujoco-react";
import { discoverRobotIdentityAnchors, type RobotIdentityAnchor } from "./robotIdentities";
import { colorForRobot } from "./robotVisuals";
const LABEL_CLEARANCE = 0.28;
const DISCOVERY_RETRY_SECONDS = 0.5;

export function RobotIdentityLabels() {
  const mujoco = useMujoco();
  const api = mujoco.isReady ? mujoco.api : null;
  const [anchors, setAnchors] = useState<RobotIdentityAnchor[]>([]);
  const labelRefs = useRef(new Map<string, Group>());
  const modelRef = useRef<unknown>(null);
  const nextDiscoveryAtRef = useRef(0);

  const discoverAnchors = () => {
    if (!api) return [];
    return discoverRobotIdentityAnchors(api.getBodies());
  };

  useEffect(() => {
    modelRef.current = mujoco.isReady ? mujoco.mjModelRef.current : null;
    nextDiscoveryAtRef.current = 0;
    setAnchors(discoverAnchors());
    // `api` is the stable scene API. A MujocoCanvas remount supplies a new one.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  useFrame(({ clock }) => {
    if (!mujoco.isReady) return;
    const model = mujoco.mjModelRef.current;
    const modelChanged = model !== modelRef.current;
    if (modelChanged) {
      modelRef.current = model;
      const discovered = discoverRobotIdentityAnchors(mujoco.api.getBodies());
      setAnchors(discovered);
      nextDiscoveryAtRef.current = clock.elapsedTime + DISCOVERY_RETRY_SECONDS;
    } else if (
      anchors.length === 0 &&
      clock.elapsedTime >= nextDiscoveryAtRef.current
    ) {
      // Scene loading and WASM/HMR transitions can briefly expose an empty body
      // list. Retry until the active model has published its body metadata.
      const discovered = discoverRobotIdentityAnchors(mujoco.api.getBodies());
      if (discovered.length > 0) setAnchors(discovered);
      nextDiscoveryAtRef.current = clock.elapsedTime + DISCOVERY_RETRY_SECONDS;
    }

    const xpos = mujoco.mjDataRef.current?.xpos;
    if (!xpos) return;

    for (const anchor of anchors) {
      const label = labelRefs.current.get(anchor.robot);
      if (!label) continue;
      const trackingOffset = anchor.trackingBodyId * 3;
      let top = xpos[trackingOffset + 2];
      for (const bodyId of anchor.bodyIds) {
        top = Math.max(top, xpos[bodyId * 3 + 2]);
      }
      label.position.set(
        xpos[trackingOffset],
        xpos[trackingOffset + 1],
        top + LABEL_CLEARANCE,
      );
    }
  });

  return (
    <group>
      {anchors.map((anchor) => {
        const color = colorForRobot(anchor.robot);
        return (
          <group
            key={anchor.robot}
            ref={(node) => {
              if (node) labelRefs.current.set(anchor.robot, node);
              else labelRefs.current.delete(anchor.robot);
            }}
          >
            <Html center sprite distanceFactor={8} zIndexRange={[20, 0]}>
              <div
                className="robot-identity-label"
                style={{ "--robot-color": color } as CSSProperties}
              >
                <span className="robot-identity-dot" />
                {anchor.robot}
              </div>
            </Html>
          </group>
        );
      })}
    </group>
  );
}
