import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { usePlanCompile } from "./usePlanCompile";
import { compileAndLoad, loadScheduleTracks } from "./compilePlan";
import type { AuthoredPlan } from "./planTypes";

vi.mock("./compilePlan", () => ({
  compileAndLoad: vi.fn(),
  loadScheduleTracks: vi.fn(),
}));
const mockCompile = vi.mocked(compileAndLoad);
const mockLoadTracks = vi.mocked(loadScheduleTracks);

const plan = (n = 1): AuthoredPlan => ({
  tasks: [{ robot: "robot0", steps: [{ id: `s${n}`, op: "wait", duration: n }] }],
});
const okResult = (warnings: string[] = []) => ({
  items: [],
  warnings,
  response: { schedule: [], warnings, conflicts: [], completed: {} },
});

beforeEach(() => {
  vi.useFakeTimers();
  mockCompile.mockReset();
  mockLoadTracks.mockReset();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("usePlanCompile", () => {
  it("debounces then compiles once, exposing items/warnings and status", async () => {
    mockCompile.mockResolvedValue(okResult(["shared sink"]));
    const { result } = renderHook(() => usePlanCompile(plan(), { debounceMs: 250 }));

    expect(result.current.status).toBe("idle");
    expect(mockCompile).not.toHaveBeenCalled(); // still within debounce

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    expect(mockCompile).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBe("ready");
    expect(result.current.warnings).toEqual(["shared sink"]);
    expect(result.current.epoch).toBe(1);
  });

  it("stays idle and never compiles when plan is null", async () => {
    const { result } = renderHook(() => usePlanCompile(null));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(mockCompile).not.toHaveBeenCalled();
    expect(result.current.status).toBe("idle");
  });

  it("coalesces rapid edits within the debounce window into one compile", async () => {
    mockCompile.mockResolvedValue(okResult());
    const { rerender } = renderHook(({ p }) => usePlanCompile(p, { debounceMs: 250 }), {
      initialProps: { p: plan(1) },
    });
    rerender({ p: plan(2) }); // edit before the first timer fires
    rerender({ p: plan(3) });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(mockCompile).toHaveBeenCalledTimes(1); // only the last survived
    expect(mockCompile.mock.calls[0][0]).toEqual(plan(3));
  });

  it("aborts an in-flight compile when the plan changes again", async () => {
    let firstSignal: AbortSignal | undefined;
    mockCompile.mockImplementationOnce((_p, o) => {
      firstSignal = o?.signal;
      return new Promise(() => {}); // never resolves
    });
    const { rerender } = renderHook(({ p }) => usePlanCompile(p, { debounceMs: 250 }), {
      initialProps: { p: plan(1) },
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(mockCompile).toHaveBeenCalledTimes(1);
    expect(firstSignal?.aborted).toBe(false);

    mockCompile.mockResolvedValue(okResult());
    rerender({ p: plan(2) }); // cleanup should abort the hung first compile
    expect(firstSignal?.aborted).toBe(true);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(mockCompile).toHaveBeenCalledTimes(2);
  });

  it("reports an error status when compile rejects", async () => {
    mockCompile.mockRejectedValue(new Error("service down"));
    const { result } = renderHook(() => usePlanCompile(plan(), { debounceMs: 250 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(result.current.status).toBe("error");
    expect(result.current.error).toMatch(/service down/);
  });

  it("adopts a Resolver compile and skips the matching plan recompile", async () => {
    const resolvedPlan = plan(9);
    const response = {
      schedule: [],
      warnings: ["verified"],
      conflicts: [],
      completed: { robot0: [{ id: "s9", op: "wait", duration: 9 }] },
    };
    mockLoadTracks.mockResolvedValue([]);
    const { result, rerender } = renderHook(
      ({ p }) => usePlanCompile(p, { debounceMs: 250 }),
      { initialProps: { p: null as AuthoredPlan | null } },
    );

    await act(async () => {
      await result.current.adoptCompileResult(resolvedPlan, response);
    });
    rerender({ p: resolvedPlan });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    expect(mockLoadTracks).toHaveBeenCalledWith(response.schedule);
    expect(mockCompile).not.toHaveBeenCalled();
    expect(result.current.status).toBe("ready");
    expect(result.current.compiledPlan).toBe(resolvedPlan);
    expect(result.current.warnings).toEqual(["verified"]);
    expect(result.current.compileId).toBeNull();
  });
});
