export const DEFAULT_SCENE_LABEL_SCALE = 1;

export function resolveSceneLabelScale(value: unknown): number {
  const scale = typeof value === "number"
    ? value
    : typeof value === "string" && value.trim() !== ""
      ? Number(value)
      : Number.NaN;

  return Number.isFinite(scale) && scale > 0
    ? scale
    : DEFAULT_SCENE_LABEL_SCALE;
}

export const sceneLabelScale = resolveSceneLabelScale(
  import.meta.env.VITE_SCENE_LABEL_SCALE,
);
