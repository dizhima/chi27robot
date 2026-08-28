import { describe, expect, it } from "vitest";
import {
  chassisEndpointForMountDraft,
  mountStandoffForChassisDrag,
} from "./standoffFrames";

describe("chassis and mount standoff translation", () => {
  it("maps a chassis drag delta back to the mount-frame standoff", () => {
    const mapped = mountStandoffForChassisDrag([1.2, 2.1], [1, 2], [1.4, 1.7]);
    expect(mapped[0]).toBeCloseTo(1.6);
    expect(mapped[1]).toBeCloseTo(1.8);
  });

  it("maps a pending mount edit forward to the displayed chassis endpoint", () => {
    const mapped = chassisEndpointForMountDraft([1, 2], [1.2, 2.1], [1.6, 1.8]);
    expect(mapped[0]).toBeCloseTo(1.4);
    expect(mapped[1]).toBeCloseTo(1.7);
  });
});
