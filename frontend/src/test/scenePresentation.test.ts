import { describe, expect, it, vi } from "vitest";
import {
  defaultScenePresentation,
  resetCameraToScenePresentation,
  scenePresentationFor,
} from "../scenePresentation";

describe("scenePresentationFor", () => {
  it("uses the exported camera preset for layout042 sorting", () => {
    expect(scenePresentationFor("assets/robocasa/layout042_sorting.mjb")).toEqual({
      camera: {
        position: [2.254573, -9.959503, 4.525],
        up: [0, 0, 1],
        fov: 52,
      },
      target: [2.254573, -0.351902, -0.995851],
    });
  });

  it("straightens the reused preset for layout012 preparing", () => {
    expect(scenePresentationFor("assets/robocasa/layout012_preparing.mjb")).toEqual({
      camera: {
        position: [2.254573, -9.959503, 4.525],
        up: [0, 0, 1],
        fov: 52,
      },
      target: [2.254573, -0.351902, -0.995851],
    });
  });

  it("uses the layout012 preparing view as the global fallback", () => {
    expect(scenePresentationFor("C:\\tmp\\layout042_sorting.mjb?cache=1")).not.toBe(
      defaultScenePresentation,
    );
    const fallback = scenePresentationFor("assets/robocasa/layout042_study.xml");
    expect(fallback).toBe(defaultScenePresentation);
    expect(fallback).toEqual(scenePresentationFor("layout012_preparing.mjb"));
  });

  it("restores camera and orbit target to the scene preset", () => {
    const position = { set: vi.fn() };
    const up = { set: vi.fn() };
    const target = { set: vi.fn() };
    const updateProjectionMatrix = vi.fn();
    const update = vi.fn();
    const controls = {
      object: { position, up, fov: 35, updateProjectionMatrix },
      target,
      update,
    };
    const presentation = scenePresentationFor("layout012_preparing.mjb");

    resetCameraToScenePresentation(controls, presentation);

    expect(position.set).toHaveBeenCalledWith(...presentation.camera.position);
    expect(up.set).toHaveBeenCalledWith(...presentation.camera.up);
    expect(controls.object.fov).toBe(52);
    expect(updateProjectionMatrix).toHaveBeenCalledOnce();
    expect(target.set).toHaveBeenCalledWith(...presentation.target);
    expect(update).toHaveBeenCalledOnce();
  });
});
