import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createCheckpointStore } from "./checkpointStore.js";

test("saves a self-contained checkpoint and discovers it after a nested rename", async () => {
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), "mujoco-checkpoint-"));
  try {
    const publicDir = path.join(temp, "public");
    const sourceTrack = path.join(publicDir, "trajectories", "scene", "tracks", "move.track.json");
    const sceneXml = path.join(publicDir, "assets", "scene.xml");
    const manifest = path.join(publicDir, "trajectories", "scene", "skills_manifest.json");
    await fs.mkdir(path.dirname(sourceTrack), { recursive: true });
    await fs.mkdir(path.dirname(sceneXml), { recursive: true });
    await fs.writeFile(sourceTrack, JSON.stringify({ meta: { skill: "move" }, channels: {} }));
    await fs.writeFile(sceneXml, "<mujoco/>");
    await fs.writeFile(manifest, "{}");

    const rootDir = path.join(temp, "study", "checkpoints");
    const store = createCheckpointStore({
      rootDir,
      frontendPublicDir: publicDir,
      fingerprintFiles: () => [sceneXml, manifest],
      now: () => new Date("2026-08-17T15:30:45.000Z"),
    });
    const saved = await store.save({
      sceneFile: "assets/scene.mjb",
      snapshot: {
        plan: { tasks: [{ task: "move", robot: "robot0", steps: [{ id: "move:1", op: "wait" }] }] },
        compile: {
          compile_id: "ephemeral",
          schedule: [{ id: "move:1", robot: "robot0", track_url: "/trajectories/scene/tracks/move.track.json" }],
          warnings: [],
          conflicts: [],
          completed: {},
        },
        semanticActions: [],
      },
    });

    const renamedParent = path.join(rootDir, "sorting");
    await fs.mkdir(renamedParent);
    await fs.rename(path.join(rootDir, saved.id), path.join(renamedParent, "initial"));

    const listed = await store.list();
    assert.deepEqual(listed.map((entry) => entry.id), ["sorting/initial"]);
    const loaded = await store.load("sorting/initial", "assets/scene.mjb");
    assert.equal(loaded.snapshot.compile.compile_id, undefined);
    assert.match(
      loaded.snapshot.compile.schedule[0].track_url,
      /^\/api\/study\/checkpoint-tracks\/sorting%2Finitial\//,
    );
    const trackName = path.basename(
      decodeURIComponent(new URL(loaded.snapshot.compile.schedule[0].track_url, "http://local").pathname),
    );
    assert.deepEqual(
      JSON.parse(await fs.readFile(store.trackPath("sorting/initial", trackName), "utf8")),
      { meta: { skill: "move" }, channels: {} },
    );
    await assert.rejects(() => store.load("sorting/initial", "assets/other.mjb"), /does not match/);
  } finally {
    await fs.rm(temp, { recursive: true, force: true });
  }
});
