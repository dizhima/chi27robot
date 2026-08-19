import path from "node:path";

/**
 * Derive every scene-scoped runtime artifact from the viewer's active scene.
 * The browser normally loads an MJB, while the Python compiler needs the
 * sibling XML, so both extensions intentionally resolve to the same runtime.
 */
export function deriveSceneRuntime(frontendPublicDir, activeScenePath) {
  const publicDir = path.resolve(frontendPublicDir);
  const resolvedScenePath = path.resolve(activeScenePath);
  const extension = path.extname(resolvedScenePath).toLowerCase();
  if (extension !== ".xml" && extension !== ".mjb") {
    throw new Error("Active scene must point to an .xml or .mjb file");
  }

  const sceneXmlPath =
    extension === ".mjb" ? resolvedScenePath.slice(0, -extension.length) + ".xml" : resolvedScenePath;
  const studyName = path.basename(sceneXmlPath, ".xml");
  const studyDir = path.join(publicDir, "trajectories", studyName);

  return {
    sceneXmlPath,
    studyName,
    studyDir,
    tracksPath: path.join(studyDir, "tracks"),
    standoffsPath: path.join(studyDir, "standoffs.json"),
    manifestPath: path.join(studyDir, "skills_manifest.json"),
  };
}

export async function inspectSkillRuntime(runtime, pathExists) {
  const artifacts = [
    { kind: "scene_xml", path: runtime.sceneXmlPath },
    { kind: "manifest", path: runtime.manifestPath },
    { kind: "standoffs", path: runtime.standoffsPath },
    { kind: "tracks", path: runtime.tracksPath },
  ];
  const missing = [];
  for (const artifact of artifacts) {
    if (!(await pathExists(artifact.path))) {
      missing.push(artifact);
    }
  }
  return {
    available: missing.length === 0,
    missing,
  };
}
