import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useRef, useState } from "react";
import type { Group } from "three";
import { useMujoco } from "mujoco-react";
import type { SceneManifest } from "./authoring/types";
import {
  discoverSceneEntityLabelAnchors,
  type SceneEntityLabelAnchor,
} from "./sceneEntityLabelModel";

const OBJECT_LABEL_CLEARANCE = 0.22;
const DISCOVERY_RETRY_SECONDS = 0.5;

type SceneEntityLabelsProps = {
  manifest: SceneManifest;
  showObjects: boolean;
};

export function SceneEntityLabels({ manifest, showObjects }: SceneEntityLabelsProps) {
  const mujoco = useMujoco();
  const api = mujoco.isReady ? mujoco.api : null;
  const [anchors, setAnchors] = useState<SceneEntityLabelAnchor[]>([]);
  const labelRefs = useRef(new Map<string, Group>());
  const modelRef = useRef<unknown>(null);
  const nextDiscoveryAtRef = useRef(0);

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

  useFrame(({ clock }) => {
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
      if (anchor.kind !== "object") continue;
      const label = labelRefs.current.get(anchor.name);
      if (!label) continue;
      const offset = anchor.bodyId * 3;
      label.position.set(
        xpos[offset],
        xpos[offset + 1],
        xpos[offset + 2] + OBJECT_LABEL_CLEARANCE,
      );
    }
  });

  return (
    <group>
      {anchors.map((anchor) => (
        <group
          key={`${anchor.kind}:${anchor.name}`}
          position={anchor.kind === "facility" ? anchor.position : undefined}
          ref={(node) => {
            if (anchor.kind !== "object") return;
            if (node) labelRefs.current.set(anchor.name, node);
            else labelRefs.current.delete(anchor.name);
          }}
        >
          <Html center sprite zIndexRange={[19, 0]}>
            <div className={`scene-entity-label is-${anchor.kind}`}>
              {anchor.text}
            </div>
          </Html>
        </group>
      ))}
    </group>
  );
}
