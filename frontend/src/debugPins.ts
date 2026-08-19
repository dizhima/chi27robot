import type { BodyAttribution } from "./conversation/sceneContext";

export type DebugPin = {
  id: string;
  bodyId: number;
  bodyName: string;
  xyz: [number, number, number];
  attribution: BodyAttribution | null;
};

export function debugPinHandle(index: number): string {
  return `p${index + 1}`;
}

export function debugPinLabel(pin: DebugPin): string {
  if (pin.attribution) return `${pin.attribution.kind}: ${pin.attribution.name}`;
  return pin.bodyName || `body ${pin.bodyId}`;
}

export function serializeDebugPins(sceneFile: string, pins: DebugPin[]) {
  return {
    scene: sceneFile,
    pins: pins.map((pin, index) => ({
      handle: debugPinHandle(index),
      kind: pin.attribution?.kind ?? null,
      name: pin.attribution?.name ?? null,
      body_name: pin.bodyName,
      body_id: pin.bodyId,
      xyz: pin.xyz.map((value) => Number(value.toFixed(6))),
    })),
  };
}
