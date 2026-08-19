import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

export const CHECKPOINT_FORMAT = "mujoco-study-checkpoint-v1";

function isInsideDirectory(childPath, parentPath) {
  const relative = path.relative(parentPath, childPath);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function portablePath(value) {
  return value.split(path.sep).join("/");
}

function timestampName(date = new Date()) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(
    date.getHours(),
  )}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

function safeFilename(value) {
  const normalized = String(value || "track")
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return normalized || "track";
}

function resolveCheckpointDir(rootDir, id) {
  if (typeof id !== "string" || !id.trim()) throw new Error("checkpoint id is required");
  const normalized = id.trim().replaceAll("\\", "/");
  if (normalized.startsWith("/") || normalized.split("/").some((part) => !part || part === "." || part === "..")) {
    throw new Error("invalid checkpoint id");
  }
  const resolved = path.resolve(rootDir, ...normalized.split("/"));
  if (!isInsideDirectory(resolved, path.resolve(rootDir))) throw new Error("invalid checkpoint id");
  return resolved;
}

async function hashFiles(files) {
  const hash = createHash("sha256");
  for (const file of files) {
    hash.update(path.basename(file));
    hash.update("\0");
    hash.update(await fs.readFile(file));
    hash.update("\0");
  }
  return hash.digest("hex");
}

async function uniqueTimestampDir(rootDir, now) {
  const base = timestampName(now);
  for (let suffix = 0; suffix < 1000; suffix += 1) {
    const name = suffix === 0 ? base : `${base}-${String(suffix).padStart(2, "0")}`;
    const candidate = path.join(rootDir, name);
    try {
      await fs.access(candidate);
    } catch {
      return { name, candidate };
    }
  }
  throw new Error("could not allocate a unique checkpoint name");
}

async function findCheckpointFiles(dir, rootDir, output) {
  let entries;
  try {
    entries = await fs.readdir(dir, { withFileTypes: true });
  } catch (error) {
    if (error && error.code === "ENOENT") return;
    throw error;
  }
  for (const entry of entries) {
    if (!entry.isDirectory() || entry.name.startsWith(".")) continue;
    const child = path.join(dir, entry.name);
    const checkpointFile = path.join(child, "checkpoint.json");
    try {
      const parsed = JSON.parse(await fs.readFile(checkpointFile, "utf8"));
      if (parsed.format === CHECKPOINT_FORMAT) {
        output.push({
          id: portablePath(path.relative(rootDir, child)),
          createdAt: parsed.createdAt,
          sceneFile: parsed.scene?.file,
          taskCount: parsed.snapshot?.plan?.tasks?.length ?? 0,
        });
        continue;
      }
    } catch (error) {
      if (!error || (error.code !== "ENOENT" && !(error instanceof SyntaxError))) throw error;
    }
    await findCheckpointFiles(child, rootDir, output);
  }
}

function sourceTrackPath(trackUrl, frontendPublicDir) {
  if (typeof trackUrl !== "string" || !trackUrl) throw new Error("schedule entry has no track_url");
  const pathname = decodeURIComponent(new URL(trackUrl, "http://checkpoint.local").pathname);
  if (!pathname.startsWith("/trajectories/")) {
    throw new Error(`checkpoint can only archive local trajectory tracks: ${trackUrl}`);
  }
  const resolved = path.resolve(frontendPublicDir, pathname.replace(/^\/+/, ""));
  if (!isInsideDirectory(resolved, path.resolve(frontendPublicDir))) {
    throw new Error(`track path escapes the public directory: ${trackUrl}`);
  }
  return resolved;
}

export function createCheckpointStore({ rootDir, frontendPublicDir, fingerprintFiles, now = () => new Date() }) {
  const resolvedRoot = path.resolve(rootDir);

  async function sceneFingerprint() {
    return hashFiles(fingerprintFiles());
  }

  async function list() {
    const checkpoints = [];
    await findCheckpointFiles(resolvedRoot, resolvedRoot, checkpoints);
    checkpoints.sort((a, b) => a.id.localeCompare(b.id));
    return checkpoints;
  }

  async function save({ sceneFile, snapshot }) {
    if (!snapshot?.plan?.tasks || !snapshot?.compile?.schedule || !Array.isArray(snapshot.semanticActions)) {
      throw new Error("a playable plan snapshot is required");
    }
    await fs.mkdir(resolvedRoot, { recursive: true });
    const { name, candidate } = await uniqueTimestampDir(resolvedRoot, now());
    const temporary = path.join(resolvedRoot, `.tmp-${name}-${process.pid}`);
    const tracksDir = path.join(temporary, "tracks");
    await fs.mkdir(tracksDir, { recursive: true });
    try {
      const schedule = [];
      for (const [index, entry] of snapshot.compile.schedule.entries()) {
        const filename = `${String(index).padStart(3, "0")}-${safeFilename(entry.id)}.track.json`;
        await fs.copyFile(sourceTrackPath(entry.track_url, frontendPublicDir), path.join(tracksDir, filename));
        schedule.push({
          ...entry,
          track_url: `/api/study/checkpoint-tracks/${encodeURIComponent(name)}/${encodeURIComponent(filename)}`,
        });
      }
      const checkpoint = {
        format: CHECKPOINT_FORMAT,
        id: name,
        createdAt: now().toISOString(),
        scene: { file: sceneFile, fingerprint: await sceneFingerprint() },
        snapshot: {
          plan: snapshot.plan,
          compile: { ...snapshot.compile, compile_id: undefined, schedule },
          semanticActions: snapshot.semanticActions,
        },
      };
      await fs.writeFile(path.join(temporary, "checkpoint.json"), `${JSON.stringify(checkpoint, null, 2)}\n`, "utf8");
      await fs.rename(temporary, candidate);
      return { id: name, createdAt: checkpoint.createdAt, sceneFile, taskCount: snapshot.plan.tasks.length };
    } catch (error) {
      await fs.rm(temporary, { recursive: true, force: true });
      throw error;
    }
  }

  async function load(id, activeSceneFile) {
    const dir = resolveCheckpointDir(resolvedRoot, id);
    const checkpoint = JSON.parse(await fs.readFile(path.join(dir, "checkpoint.json"), "utf8"));
    if (checkpoint.format !== CHECKPOINT_FORMAT) throw new Error("unsupported checkpoint format");
    if (checkpoint.scene?.file !== activeSceneFile) {
      throw new Error(`checkpoint scene ${checkpoint.scene?.file} does not match active scene ${activeSceneFile}`);
    }
    if (checkpoint.scene?.fingerprint !== await sceneFingerprint()) {
      throw new Error("checkpoint scene fingerprint does not match the active scene assets");
    }
    const encodedId = encodeURIComponent(id);
    checkpoint.id = id;
    checkpoint.snapshot.compile.schedule = checkpoint.snapshot.compile.schedule.map((entry) => {
      const filename = path.basename(decodeURIComponent(new URL(entry.track_url, "http://checkpoint.local").pathname));
      return {
        ...entry,
        track_url: `/api/study/checkpoint-tracks/${encodedId}/${encodeURIComponent(filename)}`,
      };
    });
    return checkpoint;
  }

  function trackPath(id, filename) {
    if (path.basename(filename) !== filename) throw new Error("invalid checkpoint track name");
    const dir = resolveCheckpointDir(resolvedRoot, id);
    const resolved = path.resolve(dir, "tracks", filename);
    if (!isInsideDirectory(resolved, path.join(dir, "tracks"))) throw new Error("invalid checkpoint track name");
    return resolved;
  }

  return { list, save, load, trackPath };
}
