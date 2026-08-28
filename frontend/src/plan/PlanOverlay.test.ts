import { describe, expect, it } from "vitest";
import { shouldRenderStandoff } from "./PlanOverlay";

describe("PlanOverlay standoff visibility", () => {
  it("always shows editable navigation standoffs", () => {
    expect(shouldRenderStandoff({ standoff: [1, 2], standoffEditable: true }, false)).toBe(true);
  });

  it("hides read-only operation standoffs unless enabled", () => {
    const marker = { standoff: [1, 2] as [number, number], standoffEditable: false };
    expect(shouldRenderStandoff(marker, false)).toBe(false);
    expect(shouldRenderStandoff(marker, true)).toBe(true);
  });

  it("does not render a standoff marker without coordinates", () => {
    expect(shouldRenderStandoff({ standoffEditable: true }, true)).toBe(false);
  });
});
