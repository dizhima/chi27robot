import { afterEach, describe, expect, it, vi } from "vitest";
import { compilePlan, loadScheduleTracks } from "./compilePlan";
import { SAMPLE_PLAN } from "./samplePlan";
import type { ScheduleEntry } from "./planTypes";

const makeTrack = (robotIndex = 0, nq = 125) => ({
  meta: { skill: "x", robot_index: robotIndex, n_frames: 2, duration: 1, scene_nq: nq },
  channels: { [`robot${robotIndex}_joint1`]: [[0], [0]] },
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("compilePlan", () => {
  it("POSTs { plan } to the service and returns its response", async () => {
    const body: { schedule: ScheduleEntry[]; warnings: string[]; completed: object } = {
      schedule: [],
      warnings: ["w"],
      completed: {},
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => body,
    });
    vi.stubGlobal("fetch", fetchMock);

    const res = await compilePlan(SAMPLE_PLAN, { serviceUrl: "http://svc" });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://svc/compile_plan");
    expect(JSON.parse(init.body)).toEqual({ plan: SAMPLE_PLAN });
    expect(res.warnings).toEqual(["w"]);
  });

  it("throws a service-hint error when the request fails to connect", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));
    await expect(compilePlan(SAMPLE_PLAN, { serviceUrl: "http://svc" })).rejects.toThrow(
      /is skill_service running at http:\/\/svc/,
    );
  });

  it("surfaces a service-reported error payload", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 400, json: async () => ({ error: "bad op" }) }),
    );
    await expect(compilePlan(SAMPLE_PLAN)).rejects.toThrow(/bad op/);
  });
});

describe("loadScheduleTracks", () => {
  const schedule: ScheduleEntry[] = [
    {
      id: "r0_nav",
      after: [],
      robot: "robot0",
      label: "navigate_sink",
      start: 17.07,
      duration: 14.77,
      facility: "sink",
      object: null,
      group: "t",
      track_url: "/trajectories/x/tracks/robot0/navigate_sink.track.json",
    },
    {
      id: "r1_nav",
      after: [],
      robot: "robot1",
      label: "navigate_sink",
      start: 9.73,
      duration: 14.3,
      facility: "sink",
      object: null,
      group: "t",
      track_url: "/trajectories/x/tracks/robot1/navigate_sink.track.json",
    },
  ];

  it("fetches each track_url and passes the backend's absolute start through", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => makeTrack(0) })
      .mockResolvedValueOnce({ ok: true, json: async () => makeTrack(1) });
    vi.stubGlobal("fetch", fetchMock);

    const items = await loadScheduleTracks(schedule);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][0]).toContain(schedule[0].track_url);
    expect(fetchMock.mock.calls[0][0]).toMatch(/^http:\/\/127\.0\.0\.1:8787\//);
    // absolute starts preserved verbatim (never recomputed sequentially)
    expect(items.map((i) => i.start)).toEqual([17.07, 9.73]);
    expect(items.map((i) => i.duration)).toEqual([14.77, 14.3]);
  });

  it("gives every item a unique key even when labels collide across robots", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce({ ok: true, json: async () => makeTrack(0) })
        .mockResolvedValueOnce({ ok: true, json: async () => makeTrack(1) }),
    );
    const items = await loadScheduleTracks(schedule);
    expect(new Set(items.map((i) => i.key)).size).toBe(items.length);
  });

  it("rejects a track that controls a different robot than its schedule lane", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => makeTrack(1) }),
    );
    await expect(loadScheduleTracks([schedule[0]])).rejects.toThrow(
      /scheduled on robot0.*controls robot1/,
    );
  });

  it("rejects when a track cannot be fetched", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 404 }));
    await expect(loadScheduleTracks(schedule)).rejects.toThrow(/track fetch failed \(404\)/);
  });

  it("reports an HTML fallback as a track-serving error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        headers: { get: () => "text/html; charset=utf-8" },
        json: async () => {
          throw new SyntaxError("Unexpected token '<'");
        },
      }),
    );
    await expect(loadScheduleTracks([schedule[0]])).rejects.toThrow(
      /returned text\/html.*instead of JSON/,
    );
  });
});
