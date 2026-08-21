import { describe, expect, it } from "vitest";
import {
  LABEL_ZOOM_MAX_SCALE,
  cappedLabelZoomScale,
} from "./labelZoom";

describe("cappedLabelZoomScale", () => {
  it("keeps the base size at normal/far distances", () => {
    expect(cappedLabelZoomScale(5)).toBe(1);
    expect(cappedLabelZoomScale(20)).toBe(1);
  });

  it("grows nearby labels but never exceeds the maximum", () => {
    expect(cappedLabelZoomScale(4)).toBe(1.25);
    expect(cappedLabelZoomScale(2)).toBe(LABEL_ZOOM_MAX_SCALE);
    expect(cappedLabelZoomScale(0)).toBe(LABEL_ZOOM_MAX_SCALE);
  });
});
