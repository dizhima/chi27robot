import type { SceneManifest } from "./authoring/types";

type BodyIdentity = {
  id: number;
  name: string;
};

export type ObjectLabelAnchor = {
  kind: "object";
  name: string;
  text: string;
  bodyId: number;
};

export type FacilityLabelAnchor = {
  kind: "facility";
  name: string;
  text: string;
  position: [number, number, number];
};

export type SceneEntityLabelAnchor = ObjectLabelAnchor | FacilityLabelAnchor;

function readableObjectName(name: string): string {
  return name.replace(/_/g, " ");
}

/**
 * Resolve labels exclusively through the active scene manifest. This keeps the
 * overlay portable across layouts whose MuJoCo body ids and semantic inventory
 * differ. Only articulated facilities are included; counters, sinks, and
 * islands otherwise make the scene unnecessarily noisy.
 */
export function discoverSceneEntityLabelAnchors(
  manifest: SceneManifest,
  bodies: readonly BodyIdentity[],
  showObjects: boolean,
): SceneEntityLabelAnchor[] {
  const bodyIdsByName = new Map(bodies.map((body) => [body.name, body.id]));
  const availableBodyIds = new Set(bodies.map((body) => body.id));
  const anchors: SceneEntityLabelAnchor[] = [];

  if (showObjects) {
    for (const [name, spec] of Object.entries(manifest.objects ?? {})) {
      if (spec.show_scene_label === false) continue;
      const bodyId = typeof spec.body === "number"
        ? (availableBodyIds.has(spec.body) ? spec.body : undefined)
        : (spec.body ? bodyIdsByName.get(spec.body) : undefined);
      if (bodyId === undefined) continue;
      anchors.push({ kind: "object", name, text: readableObjectName(name), bodyId });
    }
  }

  for (const [name, spec] of Object.entries(manifest.facilities ?? {})) {
    if (
      spec.show_scene_label === false
      || (!spec.articulation && !spec.scene_label_xy)
    ) continue;
    const standoff = spec.standoff?.standoff_xy;
    const worldPos = spec.world_pos;
    const xy = spec.scene_label_xy
      ?? standoff
      ?? (worldPos && worldPos.length >= 2 ? [worldPos[0], worldPos[1]] as [number, number] : null);
    if (!xy) continue;
    anchors.push({
      kind: "facility",
      name,
      text: spec.label?.trim() || name.replace(/_/g, " "),
      position: [xy[0], xy[1], spec.scene_label_z ?? 2.0],
    });
  }

  return anchors;
}
