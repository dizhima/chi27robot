import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useRef, useState } from "react";
import { Vector3, type Group } from "three";
import { useMujoco } from "mujoco-react";
import type { SceneManifest } from "./authoring/types";
import {
  discoverSceneEntityLabelAnchors,
  type SceneEntityLabelAnchor,
} from "./sceneEntityLabelModel";
import { cappedLabelZoomScale } from "./labelZoom";

const OBJECT_LABEL_CLEARANCE = 0.22;
const DISCOVERY_RETRY_SECONDS = 0.5;

type SceneEntityLabelsProps = {
  manifest: SceneManifest;
  showObjects: boolean;
  pickEnabled?: boolean;
  onLabelPick?: (pick: SceneEntityLabelPick) => void;
};

export type SceneEntityLabelPick =
  | {
      kind: "object";
      name: string;
      bodyId: number;
      bodyName: string;
      worldPos: [number, number, number];
    }
  | { kind: "facility"; name: string };

export function SceneEntityLabels({
  manifest,
  showObjects,
  pickEnabled = false,
  onLabelPick,
}: SceneEntityLabelsProps) {
  const mujoco = useMujoco();
  const api = mujoco.isReady ? mujoco.api : null;
  const [anchors, setAnchors] = useState<SceneEntityLabelAnchor[]>([]);
  const labelRefs = useRef(new Map<string, Group>());
  const labelElementRefs = useRef(new Map<string, HTMLDivElement>());
  const worldPositionRef = useRef(new Vector3());
  const modelRef = useRef<unknown>(null);
  const nextDiscoveryAtRef = useRef(0);

  const pickLabel = (anchor: SceneEntityLabelAnchor) => {
    if (!pickEnabled || !onLabelPick) return;
    if (anchor.kind === "facility") {
      onLabelPick({ kind: "facility", name: anchor.name });
      return;
    }
    const group = labelRefs.current.get(`object:${anchor.name}`);
    if (!group) return;
    onLabelPick({
      kind: "object",
      name: anchor.name,
      bodyId: anchor.bodyId,
      bodyName: anchor.bodyName,
      worldPos: [group.position.x, group.position.y, group.position.z - OBJECT_LABEL_CLEARANCE],
    });
  };

  const discoverAnchors = () => {
    if (!api) return [];
    return discoverSceneEntityLabelAnchors(manifest, api.getBodies(), showObjects);
  };

  useEffect(() => {
    modelRef.current = mujoco.isReady ? mujoco.mjModelRef.current : null;
    nextDiscoveryAtRef.current = 0;
    setAnchors(discoverAnchors());
    // A MujocoCanvas remount supplies a new API; manifest and the env-derived
    // object toggle are the other inputs that affect discovery.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, manifest, showObjects]);

  useFrame(({ camera, clock }) => {
    if (!mujoco.isReady) return;
    const model = mujoco.mjModelRef.current;
    if (model !== modelRef.current) {
      modelRef.current = model;
      setAnchors(discoverSceneEntityLabelAnchors(manifest, mujoco.api.getBodies(), showObjects));
      nextDiscoveryAtRef.current = clock.elapsedTime + DISCOVERY_RETRY_SECONDS;
    } else if (anchors.length === 0 && clock.elapsedTime >= nextDiscoveryAtRef.current) {
      const discovered = discoverSceneEntityLabelAnchors(manifest, mujoco.api.getBodies(), showObjects);
      if (discovered.length > 0) setAnchors(discovered);
      nextDiscoveryAtRef.current = clock.elapsedTime + DISCOVERY_RETRY_SECONDS;
    }

    const xpos = mujoco.mjDataRef.current?.xpos;
    if (!xpos) return;
    for (const anchor of anchors) {
      const key = `${anchor.kind}:${anchor.name}`;
      const label = labelRefs.current.get(key);
      if (!label) continue;
      if (anchor.kind === "object") {
        const offset = anchor.bodyId * 3;
        label.position.set(
          xpos[offset],
          xpos[offset + 1],
          xpos[offset + 2] + OBJECT_LABEL_CLEARANCE,
        );
      }
      const element = labelElementRefs.current.get(key);
      if (element) {
        label.getWorldPosition(worldPositionRef.current);
        const scale = cappedLabelZoomScale(camera.position.distanceTo(worldPositionRef.current));
        element.style.setProperty("--label-zoom-scale", scale.toFixed(3));
      }
    }
  });

  return (
    <group>
      {anchors.map((anchor) => (
        <group
          key={`${anchor.kind}:${anchor.name}`}
          position={anchor.kind === "facility" ? anchor.position : undefined}
          ref={(node) => {
            const key = `${anchor.kind}:${anchor.name}`;
            if (node) labelRefs.current.set(key, node);
            else labelRefs.current.delete(key);
          }}
        >
          <Html center sprite zIndexRange={[19, 0]}>
            <div
              ref={(node) => {
                const key = `${anchor.kind}:${anchor.name}`;
                if (node) labelElementRefs.current.set(key, node);
                else labelElementRefs.current.delete(key);
              }}
              className={`scene-entity-label is-${anchor.kind}${pickEnabled ? " is-pickable" : ""}`}
              onPointerDown={pickEnabled ? (event) => event.stopPropagation() : undefined}
              onDoubleClick={pickEnabled ? (event) => {
                event.preventDefault();
                event.stopPropagation();
                pickLabel(anchor);
              } : undefined}
              title={pickEnabled ? `Reference ${anchor.name}` : undefined}
            >
              {anchor.text}
            </div>
          </Html>
        </group>
      ))}
    </group>
  );
}
