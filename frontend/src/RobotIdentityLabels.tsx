import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { Vector3, type Group } from "three";
import { useMujoco } from "mujoco-react";
import { discoverRobotIdentityAnchors, type RobotIdentityAnchor } from "./robotIdentities";
import { colorForRobot } from "./robotVisuals";
import { cappedLabelZoomScale } from "./labelZoom";
const LABEL_CLEARANCE = 0.28;
const DISCOVERY_RETRY_SECONDS = 0.5;

type RobotIdentityLabelsProps = {
  pickEnabled?: boolean;
  onLabelPick?: (pick: RobotIdentityLabelPick) => void;
};

export type RobotIdentityLabelPick = {
  kind: "robot";
  name: string;
  bodyId: number;
};

export function RobotIdentityLabels({
  pickEnabled = false,
  onLabelPick,
}: RobotIdentityLabelsProps) {
  const mujoco = useMujoco();
  const api = mujoco.isReady ? mujoco.api : null;
  const [anchors, setAnchors] = useState<RobotIdentityAnchor[]>([]);
  const labelRefs = useRef(new Map<string, Group>());
  const labelElementRefs = useRef(new Map<string, HTMLDivElement>());
  const worldPositionRef = useRef(new Vector3());
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

  useFrame(({ camera, clock }) => {
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
      const element = labelElementRefs.current.get(anchor.robot);
      if (element) {
        label.getWorldPosition(worldPositionRef.current);
        const scale = cappedLabelZoomScale(camera.position.distanceTo(worldPositionRef.current));
        element.style.setProperty("--label-zoom-scale", scale.toFixed(3));
      }
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
            <Html center sprite zIndexRange={[20, 0]}>
              <div
                ref={(node) => {
                  if (node) labelElementRefs.current.set(anchor.robot, node);
                  else labelElementRefs.current.delete(anchor.robot);
                }}
                className={`robot-identity-label${pickEnabled ? " is-pickable" : ""}`}
                style={{ "--robot-color": color } as CSSProperties}
                onPointerDown={pickEnabled ? (event) => event.stopPropagation() : undefined}
                onDoubleClick={pickEnabled ? (event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  onLabelPick?.({
                    kind: "robot",
                    name: anchor.robot,
                    bodyId: anchor.trackingBodyId,
                  });
                } : undefined}
                title={pickEnabled ? `Reference ${anchor.robot}` : undefined}
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
