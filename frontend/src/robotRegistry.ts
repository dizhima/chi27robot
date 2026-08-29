import type { SceneManifest } from "./authoring/types";

/** Robot ids in the stable order declared by the active scene manifest. */
export function robotIdsFromManifest(manifest: SceneManifest | null): string[] {
  if (!manifest?.robots) return [];
  return Object.entries(manifest.robots)
    .map(([id, descriptor], position) => ({
      id,
      position,
      index: Number.isInteger(descriptor.index) ? descriptor.index as number : position,
    }))
    .sort((left, right) => left.index - right.index || left.position - right.position)
    .map(({ id }) => id);
}
