/** Labels keep their authored CSS size until the camera is this close. */
export const LABEL_ZOOM_REFERENCE_DISTANCE = 5;

/** Hard cap: labels can grow by at most 35% while zooming in. */
export const LABEL_ZOOM_MAX_SCALE = 1.35;

export function cappedLabelZoomScale(distance: number): number {
  if (!Number.isFinite(distance) || distance <= 0) return LABEL_ZOOM_MAX_SCALE;
  return Math.min(
    LABEL_ZOOM_MAX_SCALE,
    Math.max(1, LABEL_ZOOM_REFERENCE_DISTANCE / distance),
  );
}
