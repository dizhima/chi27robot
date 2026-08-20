import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { clearGeneratedTrackCaches } from "./generatedTrackCleanup.js";

test("removes only per-study generated track caches", async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "generated-track-cleanup-"));
  t.after(() => fs.rm(root, { recursive: true, force: true }));

  const trajectories = path.join(root, "trajectories");
  const generatedA = path.join(trajectories, "study_a", "tracks", "_generated");
  const generatedB = path.join(trajectories, "study_b", "tracks", "_generated");
  const canonical = path.join(trajectories, "study_a", "tracks", "robot0");
  const offPattern = path.join(trajectories, "study_a", "other", "_generated");

  await fs.mkdir(generatedA, { recursive: true });
  await fs.mkdir(generatedB, { recursive: true });
  await fs.mkdir(canonical, { recursive: true });
  await fs.mkdir(offPattern, { recursive: true });
  await fs.writeFile(path.join(generatedA, "a.json"), "generated");
  await fs.writeFile(path.join(generatedB, "b.json"), "generated");
  await fs.writeFile(path.join(canonical, "skill.track.json"), "canonical");

  const removed = await clearGeneratedTrackCaches(trajectories);

  assert.deepEqual(removed.sort(), [generatedA, generatedB].sort());
  await assert.rejects(fs.access(generatedA));
  await assert.rejects(fs.access(generatedB));
  await fs.access(path.join(canonical, "skill.track.json"));
  await fs.access(offPattern);
});

test("treats a missing trajectories directory as an empty cache", async () => {
  const removed = await clearGeneratedTrackCaches(
    path.join(os.tmpdir(), `missing-trajectories-${process.pid}-${Date.now()}`),
  );

  assert.deepEqual(removed, []);
});
