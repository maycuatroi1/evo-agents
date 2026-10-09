import { describe, expect, it } from "vitest";

import type { LiveSignal } from "@/components/live/live-model";

import {
  followTiles,
  gridShape,
  MAX_TILES,
  mediumCols,
  monitorHref,
  parseTiles,
  tailStart,
  TAIL_EVENTS,
  tileKey,
  toggleTile,
  worstSignal,
} from "./model";

const signal = (over: Partial<LiveSignal> = {}): LiveSignal => ({
  transport: "stream",
  updatedAt: 1_000,
  failing: false,
  degraded: false,
  offline: false,
  paused: false,
  retryAt: null,
  pollMs: null,
  ...over,
});

describe("the tiles in the URL", () => {
  it("follows every run in flight without the parameter, and shows no tile with it empty", () => {
    expect(parseTiles(null)).toBeNull();
    expect(parseTiles("")).toEqual([]);
    expect(monitorHref(null)).toBe("/monitor");
    expect(monitorHref([])).toBe("/monitor?runs=");
  });

  it("reads project:id entries of every project, in order, each once, and leaves out what the API would refuse", () => {
    expect(parseTiles("evo-agents:12,lms:4, evo-agents:12 ,a:0,Bad:3,x:-1,:5,lms,lms:abc,lms:9")).toEqual([
      { project: "evo-agents", id: 12 },
      { project: "lms", id: 4 },
      { project: "lms", id: 9 },
    ]);
    // What the browser may have encoded reads the same once decoded.
    expect(parseTiles(decodeURIComponent("p-2%3A7%2Cq%3A8"))).toEqual([
      { project: "p-2", id: 7 },
      { project: "q", id: 8 },
    ]);
  });

  it("writes the tiles back as it reads them", () => {
    const tiles = [
      { project: "evo-agents", id: 12 },
      { project: "lms", id: 4 },
    ];
    const href = monitorHref(tiles);
    expect(href).toBe("/monitor?runs=evo-agents:12,lms:4");
    expect(parseTiles(new URL(href, "http://hub.test").searchParams.get("runs"))).toEqual(tiles);
    expect(tileKey(tiles[0])).toBe("evo-agents:12");
  });

  it("adds a run at the end, or takes it out when it is there", () => {
    const tiles = [{ project: "a", id: 1 }];
    expect(toggleTile(tiles, { project: "b", id: 1 })).toEqual([
      { project: "a", id: 1 },
      { project: "b", id: 1 },
    ]);
    expect(toggleTile(tiles, { project: "a", id: 1 })).toEqual([]);
  });
});

describe("following every run in flight", () => {
  it("keeps the tiles it had, ended ones included, and adds each new run in flight at the end", () => {
    const first = followTiles([], [{ project: "a", id: 3 }, { project: "b", id: 1 }]);
    expect(first).toEqual([
      { project: "a", id: 3 },
      { project: "b", id: 1 },
    ]);
    // Run a:3 ended (it left the overview) and a:5 started.
    const next = followTiles(first, [{ project: "b", id: 1 }, { project: "a", id: 5 }]);
    expect(next.map(tileKey)).toEqual(["a:3", "b:1", "a:5"]);
    // Nothing new: the same array, so the page sets no state.
    expect(followTiles(next, [{ project: "a", id: 5 }])).toBe(next);
  });

  it(`makes room past ${MAX_TILES} tiles by letting the oldest ended runs go, never a run in flight`, () => {
    const ended = Array.from({ length: MAX_TILES - 1 }, (_, index) => ({ project: "old", id: index + 1 }));
    const running = { project: "a", id: 100 };
    const full = followTiles(ended, [running]);
    expect(full).toHaveLength(MAX_TILES);
    const started = [running, { project: "a", id: 101 }, { project: "a", id: 102 }];
    const next = followTiles(full, started);
    expect(next).toHaveLength(MAX_TILES);
    expect(next.slice(-3).map(tileKey)).toEqual(["a:100", "a:101", "a:102"]);
    expect(next[0]).toEqual({ project: "old", id: 3 });
  });
});

describe("the grid", () => {
  it("shares out the window from xl: 1, 2, 2x2, 3x2, 3x3, then four columns that scroll", () => {
    const shapes = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 13].map((count) => [count, gridShape(count)]);
    expect(shapes).toEqual([
      [1, { cols: 1, rows: 1, scroll: false }],
      [2, { cols: 2, rows: 1, scroll: false }],
      [3, { cols: 2, rows: 2, scroll: false }],
      [4, { cols: 2, rows: 2, scroll: false }],
      [5, { cols: 3, rows: 2, scroll: false }],
      [6, { cols: 3, rows: 2, scroll: false }],
      [7, { cols: 3, rows: 3, scroll: false }],
      [8, { cols: 3, rows: 3, scroll: false }],
      [9, { cols: 3, rows: 3, scroll: false }],
      [10, { cols: 4, rows: 3, scroll: true }],
      [13, { cols: 4, rows: 4, scroll: true }],
    ]);
  });

  it("has at most two columns from 768 to 1279 px", () => {
    expect([1, 2, 9, 20].map(mediumCols)).toEqual([1, 2, 2, 2]);
  });
});

describe("the tail of a trace", () => {
  it(`starts ${TAIL_EVENTS} events before the run's last seq, never before its first`, () => {
    expect(tailStart(0)).toBe(0);
    expect(tailStart(TAIL_EVENTS)).toBe(0);
    expect(tailStart(20_000)).toBe(20_000 - TAIL_EVENTS);
  });
});

describe("the worst connection", () => {
  it("says nothing without a signal", () => {
    expect(worstSignal([])).toBeNull();
  });

  it("picks offline over failing over a fallback over paused over live", () => {
    const live = signal({ updatedAt: 5 });
    const paused = signal({ paused: true });
    const fallback = signal({ degraded: true, retryAt: 9, pollMs: 1_500 });
    const failing = signal({ failing: true, transport: "poll" });
    const offline = signal({ offline: true });
    expect(worstSignal([live, paused])).toBe(paused);
    expect(worstSignal([live, fallback, paused])).toBe(fallback);
    expect(worstSignal([failing, fallback, live])).toBe(failing);
    expect(worstSignal([live, offline, failing])).toBe(offline);
  });

  it("among live ones the freshest, among failing ones the one silent longest", () => {
    const old = signal({ updatedAt: 1 });
    const fresh = signal({ updatedAt: 9 });
    expect(worstSignal([old, fresh])).toBe(fresh);
    const failingOld = signal({ failing: true, updatedAt: 1 });
    const failingNew = signal({ failing: true, updatedAt: 9 });
    expect(worstSignal([failingNew, failingOld])).toBe(failingOld);
  });
});
