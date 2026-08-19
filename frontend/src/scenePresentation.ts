export type ScenePresentation = {
  camera: {
    position: [number, number, number];
    up: [number, number, number];
    fov: number;
  };
  target: [number, number, number];
};

type SceneCameraControls = {
  object: {
    position: { set: (x: number, y: number, z: number) => void };
    up: { set: (x: number, y: number, z: number) => void };
    fov?: number;
    updateProjectionMatrix?: () => void;
  };
  target: { set: (x: number, y: number, z: number) => void };
  update: () => void;
};

export const defaultScenePresentation: ScenePresentation = {
  camera: {
    position: [2.254573, -9.959503, 4.525],
    up: [0, 0, 1],
    fov: 52,
  },
  target: [2.254573, -0.351902, -0.995851],
};

const scenePresentations: Record<string, ScenePresentation> = {
  "layout042_sorting.mjb": {
    camera: {
      position: [2.254573, -9.959503, 4.525],
      up: [0, 0, 1],
      fov: 52,
    },
    target: [2.254573, -0.351902, -0.995851],
  },
  "layout012_preparing.mjb": {
    camera: {
      position: [2.254573, -9.959503, 4.525],
      up: [0, 0, 1],
      fov: 52,
    },
    target: [2.254573, -0.351902, -0.995851],
  },
};

export function scenePresentationFor(sceneFile: string): ScenePresentation {
  const basename = sceneFile.split(/[\\/]/).at(-1)?.split(/[?#]/, 1)[0].toLowerCase() ?? "";
  return scenePresentations[basename] ?? defaultScenePresentation;
}

export function resetCameraToScenePresentation(
  controls: SceneCameraControls,
  presentation: ScenePresentation,
) {
  controls.object.position.set(...presentation.camera.position);
  controls.object.up.set(...presentation.camera.up);
  if (typeof controls.object.fov === "number") {
    controls.object.fov = presentation.camera.fov;
    controls.object.updateProjectionMatrix?.();
  }
  controls.target.set(...presentation.target);
  controls.update();
}
