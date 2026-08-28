export type XY = [number, number];

/** Apply a dragged chassis-centre delta to the backend's mount-frame standoff. */
export function mountStandoffForChassisDrag(
  mountStart: XY,
  chassisStart: XY,
  chassisEnd: XY,
): XY {
  return [
    mountStart[0] + chassisEnd[0] - chassisStart[0],
    mountStart[1] + chassisEnd[1] - chassisStart[1],
  ];
}

/** Preview a mount-frame draft edit at the corresponding chassis route endpoint. */
export function chassisEndpointForMountDraft(
  chassisStart: XY,
  mountStart: XY,
  mountDraft: XY,
): XY {
  return [
    chassisStart[0] + mountDraft[0] - mountStart[0],
    chassisStart[1] + mountDraft[1] - mountStart[1],
  ];
}
