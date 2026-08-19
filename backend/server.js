import { execFile, execFileSync, spawn } from "node:child_process";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import * as pty from "node-pty";
import { createCheckpointStore } from "./checkpointStore.js";
import { deriveSceneRuntime, inspectSkillRuntime } from "./sceneRuntime.js";

const execFileAsync = promisify(execFile);

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const workspace = process.env.CODEX_WORKSPACE || path.resolve(__dirname, "..");
const frontendPublicDir =
  process.env.MUJOCO_REACT_PUBLIC_DIR || path.resolve(__dirname, "../frontend/public");
const trajectoriesDir = path.join(frontendPublicDir, "trajectories");
const studyCheckpointDir = path.resolve(
  process.env.STUDY_CHECKPOINT_DIR || path.join(workspace, "study", "checkpoints"),
);
const checkpointWriteEnabled = process.env.ENABLE_CHECKPOINT_WRITE !== "false";
const defaultScenePath = path.join(
  frontendPublicDir,
  "assets",
  "robocasa",
  "layout042_study.xml",
);
const command = process.env.CODEX_COMMAND || "codex";
const host = process.env.CODEX_BACKEND_HOST || "127.0.0.1";
const port = Number(process.env.CODEX_BACKEND_PORT || 8787);
const wasmMujocoVersion = process.env.MUJOCO_WASM_VERSION || "3.10.0";
const compileMjbScript = path.join(__dirname, "compile_scene_mjb.py");
const resolvedCommand = resolveCommand(command);

// Warm skill/plan service (src/mujoco_skills/service/skill_service.py): the
// backend auto-spawns it so `npm run dev` is the only command needed. Set
// SKILL_SERVICE_AUTOSTART=0 to run it manually instead. The front-end still
// talks to it directly on its port.
const skillServiceModule = "mujoco_skills.service.skill_service";
const skillServicePort = Number(process.env.SKILL_SERVICE_PORT || 8899);
const skillServiceAutostart = process.env.SKILL_SERVICE_AUTOSTART !== "0";
const orchestratorModule = "mujoco_skills.orchestrator.service";
const orchestratorPort = Number(process.env.ORCHESTRATOR_PORT || 8900);
const orchestratorAutostart = process.env.ORCHESTRATOR_AUTOSTART !== "0";
const sceneServiceStartTimeoutMs = Number(process.env.SCENE_SERVICE_START_TIMEOUT_MS || 120000);

const clients = new Set();
const backlog = [];
let child = null;
let activeScenePath = path.resolve(process.env.MUJOCO_REACT_SCENE_PATH || defaultScenePath);

function activeSceneRuntime() {
  return deriveSceneRuntime(frontendPublicDir, activeScenePath);
}

const checkpointStore = createCheckpointStore({
  rootDir: studyCheckpointDir,
  frontendPublicDir,
  fingerprintFiles: () => [activeSceneRuntime().sceneXmlPath, activeSceneRuntime().manifestPath],
});

function sceneServiceEnvironment(extra = {}) {
  return {
    ...process.env,
    MUJOCO_REACT_PUBLIC_DIR: frontendPublicDir,
    MUJOCO_REACT_SCENE_PATH: activeSceneRuntime().sceneXmlPath,
    ...extra,
  };
}

function buildInitialPrompt() {
  if (process.env.CODEX_INITIAL_PROMPT !== undefined) {
    return process.env.CODEX_INITIAL_PROMPT;
  }
  return `Use the $cosim skill. The current front-end viewer uses this MuJoCo scene: ${activeScenePath}. You do not need to do anything.`;
}

function getCodexArgs() {
  const initialPrompt = buildInitialPrompt();
  return [
    ...splitArgs(process.env.CODEX_ARGS || ""),
    ...(initialPrompt ? [initialPrompt] : []),
  ];
}

function splitArgs(raw) {
  const parts = [];
  const pattern = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let match;
  while ((match = pattern.exec(raw)) !== null) {
    parts.push(match[1] ?? match[2] ?? match[3]);
  }
  return parts;
}

function resolveCommand(rawCommand) {
  if (path.isAbsolute(rawCommand) || rawCommand.includes("\\") || rawCommand.includes("/")) {
    return rawCommand;
  }

  if (process.platform !== "win32") {
    return rawCommand;
  }

  try {
    const output = execFileSync("where.exe", [rawCommand], {
      encoding: "utf8",
      windowsHide: true,
    });
    const firstMatch = output
      .split(/\r?\n/)
      .map((line) => line.trim())
      .find(Boolean);
    return firstMatch || rawCommand;
  } catch {
    return rawCommand;
  }
}

function remember(event) {
  backlog.push(event);
  if (backlog.length > 200) {
    backlog.shift();
  }
}

function send(res, event) {
  res.write(`data: ${JSON.stringify(event)}\n\n`);
}

function broadcast(event) {
  remember(event);
  for (const client of clients) {
    send(client, event);
  }
}

function startCodex() {
  if (child) {
    return;
  }

  const args = getCodexArgs();
  broadcast({
    type: "status",
    data: `Starting ${resolvedCommand}${args.length ? ` ${args.join(" ")}` : ""} in ${workspace}\r\n`,
  });

  try {
    child = pty.spawn(resolvedCommand, args, {
      cwd: workspace,
      env: process.env,
      name: process.platform === "win32" ? "xterm-256color" : "xterm-color",
      cols: 96,
      rows: 32,
    });
  } catch (error) {
    broadcast({
      type: "error",
      data: `Failed to start ${resolvedCommand}: ${error instanceof Error ? error.message : String(error)}\r\n`,
    });
    child = null;
    return;
  }

  child.onData((data) => {
    broadcast({ type: "output", data });
  });

  child.onExit(({ exitCode, signal }) => {
    broadcast({
      type: "status",
      data: `\r\n${command} exited with code ${exitCode ?? "null"}${signal ? `, signal ${signal}` : ""}.\r\n`,
    });
    child = null;
  });
}

function stopCodex() {
  if (!child) {
    return;
  }
  child.kill();
  child = null;
}

// --- skill_service supervisor -------------------------------------------
let skillService = null;
let skillServiceShuttingDown = false;
let skillRestarts = 0;
let skillLastStart = 0;
const intentionallyStoppedSkillServices = new WeakSet();

function startSkillService() {
  if (!skillServiceAutostart || skillService || skillServiceShuttingDown) {
    return;
  }
  skillLastStart = Date.now();
  console.log(
    `Starting skill_service (mujoco ${wasmMujocoVersion}) on http://127.0.0.1:${skillServicePort} ...`,
  );
  const args = ["run", "--with", `mujoco==${wasmMujocoVersion}`, "python", "-m", skillServiceModule];
  const proc = spawn("uv", args, {
    cwd: workspace,
    env: sceneServiceEnvironment({
      SKILL_SERVICE_PORT: String(skillServicePort),
    }),
    windowsHide: true,
  });
  skillService = proc;
  proc.stdout?.on("data", (data) => process.stdout.write(`[skill_service] ${data}`));
  proc.stderr?.on("data", (data) => process.stderr.write(`[skill_service] ${data}`));
  proc.on("error", (err) => {
    console.error(`Failed to start skill_service (is 'uv' on PATH?): ${err.message}`);
  });
  proc.on("exit", (code, signal) => {
    if (skillService === proc) {
      skillService = null;
    }
    if (skillServiceShuttingDown || intentionallyStoppedSkillServices.has(proc)) {
      return;
    }
    console.error(
      `skill_service exited (code ${code ?? "null"}${signal ? `, signal ${signal}` : ""}).`,
    );
    // Reset the rapid-crash counter if it stayed up a while; otherwise back off
    // and cap consecutive fast crashes so a persistent failure (e.g. port in
    // use) doesn't spin.
    if (Date.now() - skillLastStart > 10000) {
      skillRestarts = 0;
    }
    if (skillRestarts >= 5) {
      console.error(
        `skill_service crashed 5x quickly — giving up autostart. Is port ${skillServicePort} in use? Start it manually or set SKILL_SERVICE_AUTOSTART=0.`,
      );
      return;
    }
    skillRestarts += 1;
    setTimeout(startSkillService, 1000);
  });
}

function stopSkillService({ permanent = true } = {}) {
  if (permanent) {
    skillServiceShuttingDown = true;
  }
  if (!skillService) {
    return;
  }
  const proc = skillService;
  const pid = proc.pid;
  intentionallyStoppedSkillServices.add(proc);
  skillService = null;
  try {
    // uv spawns python as a grandchild; kill the whole tree on Windows.
    if (process.platform === "win32" && pid) {
      execFileSync("taskkill", ["/F", "/T", "/PID", String(pid)], { windowsHide: true });
    } else {
      proc.kill();
    }
  } catch {
    // best effort
  }
}

// --- stage-1 orchestrator supervisor ------------------------------------
let orchestratorService = null;
let orchestratorShuttingDown = false;
let orchestratorRestarts = 0;
let orchestratorLastStart = 0;
const intentionallyStoppedOrchestrators = new WeakSet();

function startOrchestratorService() {
  if (!orchestratorAutostart || orchestratorService || orchestratorShuttingDown) {
    return;
  }
  orchestratorLastStart = Date.now();
  console.log(`Starting orchestrator on http://127.0.0.1:${orchestratorPort} ...`);
  const proc = spawn("uv", ["run", "python", "-m", orchestratorModule], {
    cwd: workspace,
    env: sceneServiceEnvironment({
      ORCHESTRATOR_PORT: String(orchestratorPort),
      SKILL_SERVICE_URL:
        process.env.SKILL_SERVICE_URL || `http://127.0.0.1:${skillServicePort}`,
    }),
    windowsHide: true,
  });
  orchestratorService = proc;
  proc.stdout?.on("data", (data) => process.stdout.write(`[orchestrator] ${data}`));
  proc.stderr?.on("data", (data) => process.stderr.write(`[orchestrator] ${data}`));
  proc.on("error", (err) => {
    console.error(`Failed to start orchestrator (is 'uv' on PATH?): ${err.message}`);
  });
  proc.on("exit", (code, signal) => {
    if (orchestratorService === proc) {
      orchestratorService = null;
    }
    if (orchestratorShuttingDown || intentionallyStoppedOrchestrators.has(proc)) return;
    console.error(
      `orchestrator exited (code ${code ?? "null"}${signal ? `, signal ${signal}` : ""}).`,
    );
    if (Date.now() - orchestratorLastStart > 10000) {
      orchestratorRestarts = 0;
    }
    if (orchestratorRestarts >= 5) {
      console.error(
        `orchestrator crashed 5x quickly — giving up autostart. Is port ${orchestratorPort} in use?`,
      );
      return;
    }
    orchestratorRestarts += 1;
    setTimeout(startOrchestratorService, 1000);
  });
}

function stopOrchestratorService({ permanent = true } = {}) {
  if (permanent) {
    orchestratorShuttingDown = true;
  }
  if (!orchestratorService) return;
  const proc = orchestratorService;
  const pid = proc.pid;
  intentionallyStoppedOrchestrators.add(proc);
  orchestratorService = null;
  try {
    if (process.platform === "win32" && pid) {
      execFileSync("taskkill", ["/F", "/T", "/PID", String(pid)], { windowsHide: true });
    } else {
      proc.kill();
    }
  } catch {
    // best effort
  }
}

async function waitForServiceHealth(url, acceptsPayload) {
  const deadline = Date.now() + sceneServiceStartTimeoutMs;
  let lastError = "service did not answer";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${url}?t=${Date.now()}`);
      const payload = await response.json();
      if (response.ok && acceptsPayload(payload)) {
        return payload;
      }
      lastError = `unexpected health payload: ${JSON.stringify(payload)}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`Timed out waiting for ${url}: ${lastError}`);
}

async function restartSceneServices() {
  const runtime = activeSceneRuntime();
  stopOrchestratorService({ permanent: false });
  stopSkillService({ permanent: false });
  skillRestarts = 0;
  orchestratorRestarts = 0;

  const skillRuntime = await inspectSkillRuntime(runtime, pathExists);
  if (!skillRuntime.available) {
    const missing = skillRuntime.missing.map((artifact) => artifact.kind).join(", ");
    console.log(
      `Scene ${path.basename(runtime.sceneXmlPath)} opened in viewer-only mode; ` +
        `skill services disabled (missing: ${missing}).`,
    );
    return skillRuntime;
  }

  if (skillServiceAutostart) {
    startSkillService();
    await waitForServiceHealth(
      `http://127.0.0.1:${skillServicePort}/health`,
      (payload) => payload?.ok === true && payload.scene === path.basename(runtime.sceneXmlPath),
    );
  }
  if (orchestratorAutostart) {
    startOrchestratorService();
    await waitForServiceHealth(
      `http://127.0.0.1:${orchestratorPort}/health`,
      (payload) =>
        payload?.ok === true &&
        path.resolve(payload.manifest || "") === path.resolve(runtime.manifestPath),
    );
  }
  return skillRuntime;
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => {
      body += chunk;
      // A checkpoint request includes the completed plan's routed steps and
      // schedule metadata (the large trajectory arrays are copied on disk,
      // not posted). Leave enough room for non-trivial two-robot plans.
      if (body.length > 16 * 1024 * 1024) {
        reject(new Error("request body too large"));
        req.destroy();
      }
    });
    req.on("end", () => resolve(body));
    req.on("error", reject);
  });
}

function writeJson(res, statusCode, payload) {
  res.writeHead(statusCode, {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
  });
  res.end(JSON.stringify(payload));
}

async function listTrajectoryFiles() {
  const entries = await fs.readdir(trajectoriesDir, { withFileTypes: true });
  return entries
    .filter((entry) => entry.isFile() && entry.name.toLowerCase().endsWith(".json"))
    .map((entry) => `/trajectories/${entry.name}`)
    .sort((a, b) => a.localeCompare(b));
}

async function serveTrajectoryFile(reqUrl, res) {
  const prefix = "/trajectories/";
  let relativePath;
  try {
    relativePath = decodeURIComponent(reqUrl.slice(prefix.length));
  } catch {
    writeJson(res, 400, { ok: false, error: "invalid trajectory path" });
    return;
  }

  const target = path.resolve(trajectoriesDir, relativePath);
  const relative = path.relative(trajectoriesDir, target);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)
      || path.extname(target).toLowerCase() !== ".json") {
    writeJson(res, 404, { ok: false, error: "trajectory not found" });
    return;
  }

  try {
    const data = await fs.readFile(target);
    res.writeHead(200, {
      "Content-Type": "application/json; charset=utf-8",
      "Content-Length": data.length,
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin": "*",
    });
    res.end(data);
  } catch (error) {
    if (error && typeof error === "object" && error.code === "ENOENT") {
      writeJson(res, 404, { ok: false, error: "trajectory not found" });
      return;
    }
    writeJson(res, 500, {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    });
  }
}

async function serveCheckpointTrack(reqUrl, res) {
  const prefix = "/api/study/checkpoint-tracks/";
  const parts = reqUrl.slice(prefix.length).split("/");
  if (parts.length !== 2) {
    writeJson(res, 404, { ok: false, error: "checkpoint track not found" });
    return;
  }
  try {
    const checkpointId = decodeURIComponent(parts[0]);
    const filename = decodeURIComponent(parts[1]);
    const data = await fs.readFile(checkpointStore.trackPath(checkpointId, filename));
    res.writeHead(200, {
      "Content-Type": "application/json; charset=utf-8",
      "Content-Length": data.length,
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin": "*",
    });
    res.end(data);
  } catch (error) {
    const notFound = error && typeof error === "object" && error.code === "ENOENT";
    writeJson(res, notFound ? 404 : 400, {
      ok: false,
      error: notFound ? "checkpoint track not found" : error instanceof Error ? error.message : String(error),
    });
  }
}

// The skills manifest for a scene lives at trajectories/<sceneBaseName>/skills_manifest.json.
async function loadSkillsManifest() {
  const base = path.basename(activeScenePath).replace(/\.(xml|mjb)$/i, "");
  const manifestPath = path.join(trajectoriesDir, base, "skills_manifest.json");
  if (!(await pathExists(manifestPath))) {
    return { ok: false, base, reason: "no skills_manifest.json for current scene" };
  }
  const manifest = JSON.parse(await fs.readFile(manifestPath, "utf-8"));
  // base lets the client build track URLs: /trajectories/<base>/<track relative path>
  return { ok: true, base, manifest };
}

async function pathExists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

function isInsideDirectory(childPath, parentPath) {
  const relative = path.relative(parentPath, childPath);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function toPublicSceneFile(scenePath) {
  if (!isInsideDirectory(scenePath, frontendPublicDir)) {
    throw new Error(`Scene XML must be under the front-end public directory: ${frontendPublicDir}`);
  }
  return path.relative(frontendPublicDir, scenePath).split(path.sep).join("/");
}

async function buildSceneSession() {
  const sceneDir = path.dirname(activeScenePath);
  const sceneManifestPath = path.join(sceneDir, "scene_manifest.json");
  const taskPlanPath = path.join(sceneDir, "task_plan.json");
  const runtime = activeSceneRuntime();
  const skillRuntimeStatus = await inspectSkillRuntime(runtime, pathExists);
  return {
    ok: true,
    scenePath: activeScenePath,
    sceneDir,
    src: "/",
    sceneFile: toPublicSceneFile(activeScenePath),
    sceneExists: await pathExists(activeScenePath),
    skillRuntime: {
      sceneXml: runtime.sceneXmlPath,
      studyName: runtime.studyName,
      manifest: runtime.manifestPath,
      standoffs: runtime.standoffsPath,
      tracks: runtime.tracksPath,
      available: skillRuntimeStatus.available,
      missing: skillRuntimeStatus.missing,
    },
    manifest: {
      path: sceneManifestPath,
      exists: await pathExists(sceneManifestPath),
    },
    taskPlan: {
      path: taskPlanPath,
      exists: await pathExists(taskPlanPath),
    },
  };
}

async function compileSceneMjb(xmlPath) {
  const mjbPath = xmlPath.replace(/\.xml$/i, ".mjb");
  const xmlStat = await fs.stat(xmlPath);
  let needsCompile = true;

  if (await pathExists(mjbPath)) {
    const mjbStat = await fs.stat(mjbPath);
    needsCompile = mjbStat.mtimeMs < xmlStat.mtimeMs;
  }

  if (!needsCompile) {
    return { scenePath: mjbPath, compiled: false, cached: true };
  }

  broadcast({
    type: "status",
    data: `Compiling ${path.basename(xmlPath)} -> ${path.basename(mjbPath)} (mujoco ${wasmMujocoVersion})...\r\n`,
  });

  const compileArgs = [
    "run",
    `--with`,
    `mujoco==${wasmMujocoVersion}`,
    "python",
    compileMjbScript,
    xmlPath,
    mjbPath,
  ];

  try {
    const { stdout, stderr } = await execFileAsync("uv", compileArgs, {
      cwd: workspace,
      maxBuffer: 16 * 1024 * 1024,
      timeout: 10 * 60 * 1000,
      windowsHide: true,
    });
    if (stdout.trim()) {
      broadcast({ type: "status", data: `${stdout.trim()}\r\n` });
    }
    if (stderr.trim()) {
      broadcast({ type: "status", data: `${stderr.trim()}\r\n` });
    }
  } catch (error) {
    const detail =
      error instanceof Error && "stderr" in error && typeof error.stderr === "string"
        ? error.stderr.trim()
        : error instanceof Error
          ? error.message
          : String(error);
    throw new Error(`Failed to compile scene MJB: ${detail}`);
  }

  if (!(await pathExists(mjbPath))) {
    throw new Error(`MJB compile finished but file was not created: ${mjbPath}`);
  }

  return { scenePath: mjbPath, compiled: true, cached: false };
}

async function openScene(rawScenePath) {
  if (typeof rawScenePath !== "string" || !rawScenePath.trim()) {
    throw new Error("scenePath is required");
  }

  // Relative paths (e.g. "assets/robocasa/scene.xml") are resolved against the
  // front-end public directory; absolute paths keep working as before.
  const trimmedScenePath = rawScenePath.trim().replace(/^[/\\]+/, "");
  const nextScenePath = path.isAbsolute(rawScenePath.trim())
    ? path.resolve(rawScenePath.trim())
    : path.resolve(frontendPublicDir, trimmedScenePath);
  const sceneExt = path.extname(nextScenePath).toLowerCase();
  if (sceneExt !== ".xml" && sceneExt !== ".mjb") {
    throw new Error("Scene path must point to an .xml or .mjb file");
  }
  if (!isInsideDirectory(nextScenePath, frontendPublicDir)) {
    throw new Error(`Scene file must be under the front-end public directory: ${frontendPublicDir}`);
  }

  const stat = await fs.stat(nextScenePath);
  if (!stat.isFile()) {
    throw new Error("Scene path is not a file");
  }

  const previousScenePath = activeScenePath;
  let compileInfo = null;
  if (sceneExt === ".xml") {
    compileInfo = await compileSceneMjb(nextScenePath);
    activeScenePath = compileInfo.scenePath;
  } else {
    activeScenePath = nextScenePath;
  }

  try {
    await restartSceneServices();
  } catch (error) {
    const failedScenePath = activeScenePath;
    activeScenePath = previousScenePath;
    try {
      await restartSceneServices();
    } catch (rollbackError) {
      throw new Error(
        `Failed to start scene services for ${failedScenePath}; rollback to ${previousScenePath} also failed: ${
          rollbackError instanceof Error ? rollbackError.message : String(rollbackError)
        }`,
      );
    }
    throw new Error(
      `Failed to start scene services for ${failedScenePath}; restored ${previousScenePath}: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
  }

  const session = await buildSceneSession();
  if (compileInfo) {
    session.compile = compileInfo;
  }
  broadcast({
    type: "status",
    data: `Active scene set to ${activeScenePath}\r\n`,
  });
  return session;
}

const server = http.createServer(async (req, res) => {
  // Route matching below compares req.url exactly, so strip any query string
  // (clients append ?t=<cache-buster>) before dispatching.
  if (req.url) {
    req.url = req.url.split("?")[0];
  }
  if (req.method === "OPTIONS") {
    res.writeHead(204, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    });
    res.end();
    return;
  }

  if (req.method === "GET" && req.url === "/api/health") {
    writeJson(res, 200, {
      ok: true,
      command,
      resolvedCommand,
      args: getCodexArgs(),
      workspace,
      frontendPublicDir,
      activeScenePath,
      skillRuntime: activeSceneRuntime(),
      running: Boolean(child),
    });
    return;
  }

  if (req.method === "GET" && req.url === "/api/scene/current") {
    try {
      writeJson(res, 200, await buildSceneSession());
    } catch (error) {
      writeJson(res, 500, {
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      });
    }
    return;
  }

  if (req.method === "GET" && req.url === "/api/study/checkpoints") {
    try {
      writeJson(res, 200, { ok: true, checkpoints: await checkpointStore.list() });
    } catch (error) {
      writeJson(res, 500, { ok: false, error: error instanceof Error ? error.message : String(error) });
    }
    return;
  }

  if (req.method === "POST" && req.url === "/api/study/checkpoints") {
    if (!checkpointWriteEnabled) {
      writeJson(res, 403, { ok: false, error: "checkpoint saving is disabled on the backend" });
      return;
    }
    try {
      const body = await readBody(req);
      const payload = body ? JSON.parse(body) : {};
      const checkpoint = await checkpointStore.save({
        sceneFile: toPublicSceneFile(activeScenePath),
        snapshot: payload.snapshot,
      });
      writeJson(res, 201, { ok: true, checkpoint });
    } catch (error) {
      writeJson(res, 400, { ok: false, error: error instanceof Error ? error.message : String(error) });
    }
    return;
  }

  if (req.method === "GET" && req.url?.startsWith("/api/study/checkpoint-tracks/")) {
    await serveCheckpointTrack(req.url, res);
    return;
  }

  if (req.method === "GET" && req.url?.startsWith("/api/study/checkpoints/")) {
    try {
      const encodedId = req.url.slice("/api/study/checkpoints/".length);
      const checkpoint = await checkpointStore.load(
        decodeURIComponent(encodedId),
        toPublicSceneFile(activeScenePath),
      );
      writeJson(res, 200, { ok: true, checkpoint });
    } catch (error) {
      const notFound = error && typeof error === "object" && error.code === "ENOENT";
      writeJson(res, notFound ? 404 : 409, {
        ok: false,
        error: notFound ? "checkpoint not found" : error instanceof Error ? error.message : String(error),
      });
    }
    return;
  }

  if (req.method === "POST" && req.url === "/api/scene/open") {
    try {
      const body = await readBody(req);
      const payload = body ? JSON.parse(body) : {};
      writeJson(res, 200, await openScene(payload.scenePath));
    } catch (error) {
      writeJson(res, 400, {
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      });
    }
    return;
  }

  if (req.method === "GET" && req.url === "/api/trajectories") {
    try {
      const trajectories = await listTrajectoryFiles();
      writeJson(res, 200, {
        ok: true,
        trajectories,
        directory: trajectoriesDir,
      });
    } catch (error) {
      writeJson(res, 500, {
        ok: false,
        error: error instanceof Error ? error.message : String(error),
        directory: trajectoriesDir,
      });
    }
    return;
  }

  if (req.method === "GET" && req.url?.startsWith("/trajectories/")) {
    await serveTrajectoryFile(req.url, res);
    return;
  }

  if (req.method === "GET" && req.url === "/api/skills") {
    try {
      writeJson(res, 200, await loadSkillsManifest());
    } catch (error) {
      writeJson(res, 500, {
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      });
    }
    return;
  }

  if (req.method === "GET" && req.url === "/api/terminal/events") {
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "Access-Control-Allow-Origin": "*",
    });
    res.write(": connected\n\n");
    clients.add(res);
    for (const event of backlog) {
      send(res, event);
    }
    startCodex();
    req.on("close", () => {
      clients.delete(res);
    });
    return;
  }

  if (req.method === "POST" && req.url === "/api/terminal/input") {
    try {
      const body = await readBody(req);
      const payload = body ? JSON.parse(body) : {};
      const input = typeof payload.data === "string" ? payload.data : "";
      startCodex();
      child?.write(input);
      writeJson(res, 200, { ok: true });
    } catch (error) {
      writeJson(res, 400, { ok: false, error: error instanceof Error ? error.message : String(error) });
    }
    return;
  }

  if (req.method === "POST" && req.url === "/api/terminal/restart") {
    stopCodex();
    startCodex();
    writeJson(res, 200, { ok: true });
    return;
  }

  if (req.method === "POST" && req.url === "/api/terminal/stop") {
    stopCodex();
    writeJson(res, 200, { ok: true });
    return;
  }

  if (req.method === "POST" && req.url === "/api/terminal/resize") {
    try {
      const body = await readBody(req);
      const payload = body ? JSON.parse(body) : {};
      const cols = Number(payload.cols);
      const rows = Number(payload.rows);
      if (child && Number.isFinite(cols) && Number.isFinite(rows)) {
        child.resize(Math.max(40, cols), Math.max(10, rows));
      }
      writeJson(res, 200, { ok: true });
    } catch (error) {
      writeJson(res, 400, { ok: false, error: error instanceof Error ? error.message : String(error) });
    }
    return;
  }

  writeJson(res, 404, { ok: false, error: "not found" });
});

server.listen(port, host, () => {
  const args = getCodexArgs();
  console.log(`Codex bridge listening on http://${host}:${port}`);
  console.log(`Workspace: ${workspace}`);
  console.log(`Command: ${command}${args.length ? ` ${args.join(" ")}` : ""}`);
  void restartSceneServices().catch((error) => {
    console.error(
      `Failed to initialize scene services: ${error instanceof Error ? error.message : String(error)}`,
    );
  });
});

// Tear down child processes when the backend exits so nothing is orphaned.
function shutdown() {
  stopOrchestratorService();
  stopSkillService();
  stopCodex();
}
process.on("SIGINT", () => {
  shutdown();
  process.exit(0);
});
process.on("SIGTERM", () => {
  shutdown();
  process.exit(0);
});
process.on("exit", shutdown);
