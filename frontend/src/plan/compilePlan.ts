/**
 * Headless plan-compilation pipeline (P0). No React, no UI: plan in →
 * { items, warnings } out. UI surfaces feed `items` to <SchedulePlayer> and
 * render `warnings` however they like.
 */
import { backendUrl, skillServiceUrl } from "../config";
import type { ScheduledItem } from "../SchedulePlayer";
import type { SkillTrack } from "../skillTrackPlayback";
import type { AuthoredPlan, CompileResponse, ScheduleEntry } from "./planTypes";

export type { CompileResponse, ScheduleEntry } from "./planTypes";

type CompileOptions = {
  /** Override the skill service base URL (defaults to skillServiceUrl). */
  serviceUrl?: string;
  signal?: AbortSignal;
};

type LoadTracksOptions = {
  signal?: AbortSignal;
  /** Origin that serves relative /trajectories paths (defaults to backendUrl). */
  trackBaseUrl?: string;
};

/** POST an authoring plan to the warm skill service and return its response. */
export async function compilePlan(
  plan: AuthoredPlan,
  opts: CompileOptions = {},
): Promise<CompileResponse> {
  const base = opts.serviceUrl ?? skillServiceUrl;
  let response: Response;
  try {
    response = await fetch(`${base}/compile_plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan }),
      signal: opts.signal,
    });
  } catch (err) {
    throw new Error(
      `compile_plan request failed (is skill_service running at ${base}?): ${
        err instanceof Error ? err.message : String(err)
      }`,
    );
  }
  const payload = (await response.json().catch(() => ({}))) as
    | CompileResponse
    | { error?: string };
  if (!response.ok || "error" in payload) {
    const detail = "error" in payload && payload.error ? payload.error : response.status;
    throw new Error(`compile_plan failed: ${detail}`);
  }
  return payload as CompileResponse;
}

/**
 * Fetch each scheduled track by its track_url and assemble ScheduledItems for
 * <SchedulePlayer>. The backend already resolved absolute start times (respecting
 * cross-robot `after` deps), so we pass entry.start through verbatim — never
 * recompute it. Keys are made unique across robots.
 */
export async function loadScheduleTracks(
  schedule: ScheduleEntry[],
  opts: LoadTracksOptions = {},
): Promise<ScheduledItem[]> {
  // Cache-bust so a re-compile after the backend regenerates tracks on disk
  // never serves a stale cached copy (track_urls are stable across compiles).
  const bust = Date.now();
  const trackBase = (opts.trackBaseUrl ?? backendUrl).replace(/\/$/, "");
  return Promise.all(
    schedule.map(async (entry, index) => {
      const trackUrlIsAbsolute = /^https?:\/\//i.test(entry.track_url);
      const absoluteTrackUrl = trackUrlIsAbsolute
        ? entry.track_url
        : `${trackBase}/${entry.track_url.replace(/^\//, "")}`;
      let response = await fetch(`${absoluteTrackUrl}?t=${bust}`, { signal: opts.signal });
      // Compatibility for an already-running pre-route backend. Once 8787 is
      // restarted, relative tracks are served there. Until then its JSON 404
      // falls back to Vite's public directory; the content-type guard below
      // prevents Vite's SPA index fallback from masquerading as a track.
      if (!response.ok && !trackUrlIsAbsolute) {
        response = await fetch(`${entry.track_url}?t=${bust}`, { signal: opts.signal });
      }
      if (!response.ok) {
        throw new Error(`track fetch failed (${response.status}): ${entry.track_url}`);
      }
      const contentType = response.headers?.get?.("content-type") ?? "";
      if (contentType && !contentType.toLowerCase().includes("application/json")) {
        throw new Error(
          `track fetch returned ${contentType} instead of JSON: ${entry.track_url}`,
        );
      }
      let track: SkillTrack;
      try {
        track = (await response.json()) as SkillTrack;
      } catch {
        throw new Error(`track response is not valid JSON: ${entry.track_url}`);
      }
      const expectedRobot = Number(entry.robot.replace("robot", ""));
      const trackRobots = new Set<number>([track.meta.robot_index]);
      for (const channel of Object.keys(track.channels)) {
        const match = channel.match(/^(?:mobilebase|robot|gripper)(\d+)_/);
        if (match) trackRobots.add(Number(match[1]));
      }
      if (trackRobots.size !== 1 || !trackRobots.has(expectedRobot)) {
        const actual = [...trackRobots]
          .sort((a, b) => a - b)
          .map((robot) => `robot${robot}`)
          .join(", ");
        throw new Error(
          `track robot mismatch for ${entry.id}: scheduled on ${entry.robot}, ` +
            `but ${entry.track_url} controls ${actual || "no robot"}`,
        );
      }
      return {
        key: `${entry.robot}:${entry.label}:${index}`,
        robot: entry.robot,
        skill: entry.label,
        track,
        start: entry.start,
        duration: entry.duration,
        object: entry.object,
        op: entry.op,
      } satisfies ScheduledItem;
    }),
  );
}

/** Convenience: compile a plan and load its tracks in one call. */
export async function compileAndLoad(
  plan: AuthoredPlan,
  opts: CompileOptions = {},
): Promise<{ items: ScheduledItem[]; warnings: string[]; response: CompileResponse }> {
  const response = await compilePlan(plan, opts);
  const items = await loadScheduleTracks(response.schedule, { signal: opts.signal });
  return { items, warnings: response.warnings, response };
}
