import { describe, expect, it } from "vitest";
import { debugPinHandle, debugPinLabel, serializeDebugPins, type DebugPin } from "./debugPins";

const pin: DebugPin = {
  id: "id-1",
  bodyId: 42,
  bodyName: "apple_1_main",
  xyz: [0.4989976525, -3.8250497411, 1.1546076224],
  attribution: { kind: "object", name: "apple_1" },
};

describe("debug scene pins", () => {
  it("shows stable pN handles and manifest attribution", () => {
    expect(debugPinHandle(0)).toBe("p1");
    expect(debugPinLabel(pin)).toBe("object: apple_1");
  });

  it("exports scene-bound object and coordinate metadata", () => {
    expect(serializeDebugPins("assets/robocasa/layout012_study.mjb", [pin])).toEqual({
      scene: "assets/robocasa/layout012_study.mjb",
      pins: [
        {
          handle: "p1",
          kind: "object",
          name: "apple_1",
          body_name: "apple_1_main",
          body_id: 42,
          xyz: [0.498998, -3.82505, 1.154608],
        },
      ],
    });
  });
});
