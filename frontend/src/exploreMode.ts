/**
 * State predicates for the hand-off between kinematic plan playback and the
 * live MuJoCo explore simulation.  Keeping these pure makes the two modes
 * explicit and prevents `hasPlan` from accidentally disabling exploration.
 */
export function canEnterExplore({
  hasPlan,
  simReady,
}: {
  hasPlan: boolean;
  simReady: boolean;
}) {
  return hasPlan && simReady;
}

export function canUsePlanPlayback({
  hasPlan,
  inSync,
  dirty,
  editImpact = "structural",
  itemCount,
  status,
}: {
  hasPlan: boolean;
  inSync: boolean;
  dirty: boolean;
  /** Spatial drafts may replay the last compiled tracks; structural drafts may not. */
  editImpact?: "spatial" | "structural";
  itemCount: number;
  status: string;
}) {
  return hasPlan && inSync && (!dirty || editImpact === "spatial") && itemCount > 0 && status === "ready";
}

export function shouldPauseScene({
  hasPlan,
  exploreMode,
  simReady,
}: {
  hasPlan: boolean;
  exploreMode: boolean;
  simReady: boolean;
}) {
  return !simReady || (hasPlan && !exploreMode);
}

export function shouldEnableExploreTools(hasPlan: boolean, exploreMode: boolean) {
  return !hasPlan || exploreMode;
}
