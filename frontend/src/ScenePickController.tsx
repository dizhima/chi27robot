import { useEffect, useRef } from "react";
import { useThree } from "@react-three/fiber";
import { Raycaster, Vector2, type Camera, type Object3D, type Scene } from "three";
import { useMujoco } from "mujoco-react";
import { resolveBody, type BodyAttribution } from "./conversation/sceneContext";

export type ScenePick = {
  bodyId: number;
  bodyName: string;
  point: [number, number, number];
  attribution: BodyAttribution | null;
};

/**
 * Double-click reference picker. Mirrors mujoco-react's own DragInteraction
 * picking (the proven path): unproject the pointer with the R3F camera, raycast
 * the scene meshes, walk up to the mesh carrying `userData.bodyID`, and read the
 * intersection's world `point`. This yields the exact surface hit point plus the
 * hit body in one gesture; the body is resolved against the scene manifest so
 * the caller can decide object-vs-position.
 *
 * The dblclick listener is attached ONCE per canvas; the live camera/scene/api/
 * callback are read from refs so a per-frame context update (mujoco provider)
 * can't detach the listener. Rendered as a child of <MujocoCanvas>; draws nothing.
 */
export function ScenePickController({
  bodyIndex,
  enabled,
  onPick,
}: {
  bodyIndex: Map<string, BodyAttribution>;
  enabled: boolean;
  onPick: (pick: ScenePick) => void;
}) {
  const { camera, gl, scene } = useThree();
  const mujoco = useMujoco();

  const raycasterRef = useRef(new Raycaster());
  const mouseRef = useRef(new Vector2());
  // Latest values, read inside the stable listener.
  const latest = useRef({ camera, scene, mujoco, bodyIndex, enabled, onPick });
  latest.current = { camera, scene, mujoco, bodyIndex, enabled, onPick };

  useEffect(() => {
    const canvas = gl.domElement;
    const handle = (event: MouseEvent) => {
      const { camera: cam, scene: scn, mujoco: mj, bodyIndex: index, enabled: on, onPick: cb } =
        latest.current;
      if (!on || !mj.isReady) return;
      const rect = canvas.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      mouseRef.current.set(
        ((event.clientX - rect.left) / rect.width) * 2 - 1,
        -((event.clientY - rect.top) / rect.height) * 2 + 1,
      );
      raycasterRef.current.setFromCamera(mouseRef.current, cam as Camera);
      const hits = raycasterRef.current.intersectObjects((scn as Scene).children, true);
      for (const hit of hits) {
        // Walk up to the mesh tagged with a MuJoCo body id (overlay meshes have
        // none, so transparent markers fall through to the real body behind).
        let obj: Object3D | null = hit.object;
        while (obj && obj.userData.bodyID === undefined && obj.parent) {
          obj = obj.parent;
        }
        const bid = obj?.userData.bodyID as number | undefined;
        if (typeof bid !== "number" || bid <= 0) continue;

        const bodyMeta = new Map(
          mj.api.getBodies().map((b) => [b.id, { name: b.name, parentId: b.parentId }]),
        );
        const { attribution, bodyName } = resolveBody(bid, bodyMeta, index);
        cb({
          bodyId: bid,
          bodyName,
          point: [hit.point.x, hit.point.y, hit.point.z],
          attribution,
        });
        return;
      }
    };
    canvas.addEventListener("dblclick", handle);
    return () => canvas.removeEventListener("dblclick", handle);
  }, [gl]);

  return null;
}
