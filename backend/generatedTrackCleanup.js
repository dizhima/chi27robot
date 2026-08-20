import fs from "node:fs/promises";
import path from "node:path";

/**
 * Remove compiler output caches matching trajectories/<study>/tracks/_generated.
 * Canonical tracks and similarly named directories elsewhere are untouched.
 */
export async function clearGeneratedTrackCaches(trajectoriesDir) {
  let studies;
  try {
    studies = await fs.readdir(trajectoriesDir, { withFileTypes: true });
  } catch (error) {
    if (error?.code === "ENOENT") return [];
    throw error;
  }

  const removed = [];
  for (const study of studies) {
    if (!study.isDirectory()) continue;

    const generatedDir = path.join(trajectoriesDir, study.name, "tracks", "_generated");
    let stat;
    try {
      stat = await fs.lstat(generatedDir);
    } catch (error) {
      if (error?.code === "ENOENT") continue;
      throw error;
    }

    // Never follow a directory link during recursive cache cleanup.
    if (!stat.isDirectory() || stat.isSymbolicLink()) continue;
    await fs.rm(generatedDir, {
      recursive: true,
      force: true,
      maxRetries: 3,
      retryDelay: 100,
    });
    removed.push(generatedDir);
  }

  return removed;
}
