import { describe, expect, it } from "vitest";

import { dayRows, durationParts, durationTicks, figuresOf, longestDuration, stackEnds, utcMidnight } from "./model";
import { DEFAULT_RANGE, rangeSearch, readRange, type RunDay, statsKey } from "./queries";

function day(date: string, figures: Partial<RunDay> = {}): RunDay {
  return {
    day: date,
    done: 0,
    failed: 0,
    lost: 0,
    cancelled: 0,
    p50_seconds: null,
    p90_seconds: null,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    reasoning_tokens: 0,
    runs_with_usage: 0,
    ...figures,
  };
}

describe("figuresOf", () => {
  it("counts failed and lost as failures of the runs that ended done, failed or lost, leaving cancelled out", () => {
    const figures = figuresOf(day("2026-10-05", { done: 2, failed: 1, lost: 1, cancelled: 3 }));
    expect(figures).toMatchObject({ ended: 7, stopped: 4, failing: 2, counted: 4, rate: 0.5 });
  });

  it("has no failure rate on a day only cancelled runs ended, and no duration where none started", () => {
    const figures = figuresOf(day("2026-10-05", { cancelled: 2 }));
    expect(figures.rate).toBeNull();
    expect(figures.p50).toBeNull();
    expect(figures.p90).toBeNull();
    expect(figuresOf(day("2026-10-06")).rate).toBeNull();
  });

  it("adds the four kinds of tokens up and keeps how many runs reported usage", () => {
    const figures = figuresOf(
      day("2026-10-05", { cache_read_tokens: 17000, input_tokens: 4400, output_tokens: 1500, reasoning_tokens: 400, runs_with_usage: 3 }),
    );
    expect(figures).toMatchObject({ cacheRead: 17000, input: 4400, output: 1500, reasoning: 400, tokens: 23300, withUsage: 3 });
  });
});

describe("dayRows", () => {
  it("keeps every day of the range in the API's order, the oldest first", () => {
    const rows = dayRows({ by_day: [day("2026-10-05", { done: 1 }), day("2026-10-06"), day("2026-10-07", { failed: 2 })] });
    expect(rows.map((row) => [row.day, row.ended])).toEqual([
      ["2026-10-05", 1],
      ["2026-10-06", 0],
      ["2026-10-07", 2],
    ]);
  });
});

describe("stackEnds", () => {
  const keys = ["done", "failed", "stopped"] as const;

  it("names the lowest and highest series that hold a value", () => {
    expect(stackEnds({ done: 2, failed: 0, stopped: 1 }, keys)).toEqual({ bottom: "done", top: "stopped" });
    expect(stackEnds({ done: 0, failed: 3, stopped: 0 }, keys)).toEqual({ bottom: "failed", top: "failed" });
  });

  it("names none on a day with nothing in the stack", () => {
    expect(stackEnds({ done: 0, failed: 0, stopped: 0 }, keys)).toEqual({ bottom: null, top: null });
  });
});

describe("durations", () => {
  it("splits seconds into hours, minutes and seconds, rounded to the second", () => {
    expect(durationParts(45.4)).toEqual({ hours: 0, minutes: 0, seconds: 45 });
    expect(durationParts(540)).toEqual({ hours: 0, minutes: 9, seconds: 0 });
    expect(durationParts(3725.6)).toEqual({ hours: 1, minutes: 2, seconds: 6 });
  });

  it("puts the axis's ticks at round steps from zero to the longest value", () => {
    expect(durationTicks(57)).toEqual([0, 15, 30, 45, 60]);
    expect(durationTicks(540)).toEqual([0, 300, 600]);
    expect(durationTicks(5000)).toEqual([0, 1800, 3600, 5400]);
    expect(durationTicks(0)).toEqual([0, 15, 30, 45, 60]);
    expect(durationTicks(400_000)).toEqual([0, 172_800, 345_600, 518_400]);
  });

  it("reads the longest duration from the p90, else the p50, of every day", () => {
    expect(longestDuration([{ p50: 30, p90: 57 }, { p50: 300, p90: null }, { p50: null, p90: null }])).toBe(300);
    expect(longestDuration([])).toBe(0);
  });
});

describe("ranges", () => {
  it("reads 7, 30 or 90 days from the URL, anything else as the 30 the API counts by default", () => {
    expect(readRange("7")).toBe(7);
    expect(readRange(["90", "7"])).toBe(90);
    expect(readRange("14")).toBe(DEFAULT_RANGE);
    expect(readRange(null)).toBe(30);
    expect(readRange(undefined)).toBe(30);
  });

  it("writes no query for the default, so the plain URL is the 30 days", () => {
    expect(rangeSearch(30)).toBe("");
    expect(rangeSearch(7)).toBe("?days=7");
    expect(rangeSearch(90)).toBe("?days=90");
  });

  it("keeps the figures under the project's run keys, one entry per range", () => {
    expect(statsKey("demo", 7)).toEqual(["projects", "demo", "runs", "stats", 7]);
  });

  it("reads a UTC day at its midnight in UTC", () => {
    expect(utcMidnight("2026-10-07").toISOString()).toBe("2026-10-07T00:00:00.000Z");
  });
});
