/**
 * Inline scene references (Cursor-style "@" tokens, picked from the 3D scene).
 * A single double-click in the viewer raycasts the scene; the hit body decides
 * the reference kind:
 *   - a pickable OBJECT  -> an object ref (highlight only, no marker), token `mug_1`
 *   - a facility SURFACE -> a position ref (pin marker drawn), token `📍 counter_left`
 *   - the floor/wall     -> a position ref with `onFacility: null`
 * Refs are serialized into the unified /conversation/stream body as `scene_refs`.
 * FRONTEND-ONLY for now — the backend receives them via `body.get("scene_refs")`
 * and currently ignores unknown fields, so no prompt wiring is required yet. The
 * shape is nonetheless author-aligned: every ref resolves to a manifest NAME the
 * authoring loop already understands (objects/facilities are name-keyed, coord-
 * free), with the raw world point riding along as a future `at`-level refinement.
 */
import type { SceneManifest } from "../authoring/types";

export type SceneObjectRef = {
  kind: "object";
  id: string;
  /** Manifest object name, e.g. "mug_1". */
  name: string;
  /** MuJoCo body name, e.g. "mug_1_main". */
  body: string;
  bodyId: number;
  /** World hit point [x, y, z] (the object's surface where it was clicked). */
  worldPos: [number, number, number];
};

/** A semantic facility reference created by double-clicking a facility label.
 * Unlike a position ref, this deliberately carries no placement coordinates. */
export type SceneFacilityRef = {
  kind: "facility";
  id: string;
  /** Exact manifest facility name, e.g. "upper_cabinet". */
  name: string;
};

export type ScenePositionRef = {
  kind: "position";
  id: string;
  /** World hit point [x, y, z] on the surface. */
  xyz: [number, number, number];
  /** Placement facility the surface belongs to, or null for floor/wall. */
  onFacility: string | null;
  /** Object the point sits on, if any (unused by the double-click flow). */
  onObject: string | null;
  bodyId: number;
  bodyName: string;
};

export type SceneContextRef = SceneObjectRef | SceneFacilityRef | ScenePositionRef;

/** Attribution of a hit body to a manifest entity. */
export type BodyAttribution =
  | { kind: "object"; name: string }
  | { kind: "facility"; name: string };

/** Body-name -> manifest entity index. Objects win ties; a facility's own body
 *  and its placement `surface_body` both map back to the facility name. */
export function buildBodyIndex(manifest: SceneManifest): Map<string, BodyAttribution> {
  const index = new Map<string, BodyAttribution>();
  for (const [name, spec] of Object.entries(manifest.objects ?? {})) {
    if (typeof spec.body === "string") index.set(spec.body, { kind: "object", name });
  }
  for (const [name, spec] of Object.entries(manifest.facilities ?? {})) {
    if (typeof spec.body === "string" && !index.has(spec.body)) {
      index.set(spec.body, { kind: "facility", name });
    }
    const surfaceBody = spec.place?.surface_body;
    if (typeof surfaceBody === "string" && !index.has(surfaceBody)) {
      index.set(surfaceBody, { kind: "facility", name });
    }
  }
  return index;
}

/** Resolve a hit body to a manifest entity, walking up the parent chain when the
 *  exact hit body isn't a named entity (raycasts often land on a leaf geom body).
 *  Returns the attribution (or null) plus the hit body's own name. */
export function resolveBody(
  bodyId: number,
  bodyMeta: Map<number, { name: string; parentId: number }>,
  index: Map<string, BodyAttribution>,
): { attribution: BodyAttribution | null; bodyName: string } {
  const hit = bodyMeta.get(bodyId);
  const bodyName = hit?.name ?? "";
  let currentId = bodyId;
  const seen = new Set<number>();
  while (!seen.has(currentId)) {
    seen.add(currentId);
    const meta = bodyMeta.get(currentId);
    if (!meta) break;
    const attribution = index.get(meta.name);
    if (attribution) return { attribution, bodyName };
    if (meta.parentId === currentId) break; // world/root is its own parent
    currentId = meta.parentId;
  }
  return { attribution: null, bodyName };
}

/** Build a ref from a resolved pick. An object hit → object ref (no marker);
 *  a surface/floor hit → position ref (marker). */
export function refFromPick(
  id: string,
  bodyId: number,
  bodyName: string,
  point: [number, number, number],
  attribution: BodyAttribution | null,
): SceneContextRef {
  if (attribution?.kind === "object") {
    return { kind: "object", id, name: attribution.name, body: bodyName, bodyId, worldPos: point };
  }
  return {
    kind: "position",
    id,
    xyz: point,
    onFacility: attribution?.kind === "facility" ? attribution.name : null,
    onObject: null,
    bodyId,
    bodyName,
  };
}

/** Short token label (the component adds the 📍 glyph for positions). */
export function refLabel(ref: SceneContextRef): string {
  if (ref.kind === "facility") return ref.name;
  if (ref.kind === "object") return ref.name || ref.body || `body ${ref.bodyId}`;
  return ref.onFacility ?? ref.onObject ?? "spot";
}

/** Readable substitution for the token inside the sent user text (what the
 *  author eventually reads): a manifest name, or a neutral phrase for a
 *  free-floating floor/wall point. */
export function refText(ref: SceneContextRef): string {
  if (ref.kind === "facility") return ref.name;
  if (ref.kind === "object") return ref.name || ref.body;
  return ref.onFacility ?? ref.onObject ?? "the marked location";
}

/** Assign short per-turn handles ("p1", "p2", ...) to bindable position refs —
 *  a position ref counts as bindable only when it sits on a named facility
 *  (`onFacility != null`); free-floating floor/wall pins never get a handle.
 *  Assigned in the given array's order (the order refs appear in the composer
 *  message), so the backend LLM can bind a move to the pin by handle. */
export function assignPinHandles(refs: SceneContextRef[]): Map<string, string> {
  const handles = new Map<string, string>();
  let n = 0;
  for (const ref of refs) {
    if (ref.kind === "position" && ref.onFacility != null) {
      n += 1;
      handles.set(ref.id, `p${n}`);
    }
  }
  return handles;
}

/** Wire form for the request body (snake_case to match backend conventions).
 *  `handles` (from `assignPinHandles`) adds a `handle` field to bindable
 *  position entries; omitted entirely when no handle applies or none is given. */
export function serializeRefs(refs: SceneContextRef[], handles?: Map<string, string>): unknown[] {
  return refs.map((ref) => {
    if (ref.kind === "object") {
      return { kind: "object", name: ref.name, body: ref.body, body_id: ref.bodyId, world_pos: ref.worldPos };
    }
    if (ref.kind === "facility") return { kind: "facility", name: ref.name };
    return {
      kind: "position",
      xyz: ref.xyz,
      on_facility: ref.onFacility,
      on_object: ref.onObject,
      body_id: ref.bodyId,
      body_name: ref.bodyName,
      ...(handles?.has(ref.id) ? { handle: handles.get(ref.id) } : {}),
    };
  });
}
