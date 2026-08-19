import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";

import { deriveSceneRuntime, inspectSkillRuntime } from "./sceneRuntime.js";

test("derives the 012 runtime from its compiled MJB", () => {
  const publicDir = path.resolve("frontend/public");
  const runtime = deriveSceneRuntime(
    publicDir,
    path.join(publicDir, "assets/robocasa/layout012_study.mjb"),
  );

  assert.equal(runtime.sceneXmlPath, path.join(publicDir, "assets/robocasa/layout012_study.xml"));
  assert.equal(runtime.studyName, "layout012_study");
  assert.equal(runtime.manifestPath, path.join(publicDir, "trajectories/layout012_study/skills_manifest.json"));
  assert.equal(runtime.standoffsPath, path.join(publicDir, "trajectories/layout012_study/standoffs.json"));
  assert.equal(runtime.tracksPath, path.join(publicDir, "trajectories/layout012_study/tracks"));
});

test("rejects an unsupported active-scene extension", () => {
  assert.throws(
    () => deriveSceneRuntime("frontend/public", "frontend/public/assets/scene.json"),
    /\.xml or \.mjb/,
  );
});

test("marks an incomplete scene runtime as viewer-only", async () => {
  const runtime = deriveSceneRuntime(
    "frontend/public",
    "frontend/public/assets/robocasa/layout024_sorting.mjb",
  );
  const existing = new Set([runtime.sceneXmlPath]);

  const status = await inspectSkillRuntime(runtime, async (filePath) => existing.has(filePath));

  assert.equal(status.available, false);
  assert.deepEqual(
    status.missing.map((artifact) => artifact.kind),
    ["manifest", "standoffs", "tracks"],
  );
});

test("marks a complete scene runtime as skill-enabled", async () => {
  const runtime = deriveSceneRuntime(
    "frontend/public",
    "frontend/public/assets/robocasa/layout012_study.xml",
  );

  const status = await inspectSkillRuntime(runtime, async () => true);

  assert.deepEqual(status, { available: true, missing: [] });
});
