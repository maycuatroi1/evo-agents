import { describe, expect, it } from "vitest";

import type { Today, TodayItem } from "@/lib/notification-queries";

import { countsByKind, summaryOf, type SummaryWords, windowOf } from "./today-model";

const item = (kind: TodayItem["kind"]): TodayItem => ({ kind, title: kind, body: null, at: "2026-10-25T01:00:00Z", project: "northwind", link: "/" });

function today(extra: Partial<Today> = {}): Today {
  return {
    summary: "Nothing waits for your answer, and nothing changed since you last looked.",
    since: "2026-10-24T08:00:00Z",
    seen_at: "2026-10-24T08:00:00Z",
    seen_until: "2026-10-25T08:00:00Z",
    needs_you: [],
    needs_you_count: 0,
    items: [],
    caught_up: true,
    ...extra,
  };
}

/** Words that show what the sentence was made of. */
const WORDS: SummaryWords = {
  asks: (count) => `asks(${count})`,
  nothing: (asks, window) => `${asks} nothing(${window})`,
  part: (kind, count) => `${kind}x${count}`,
  changed: (asks, window, parts, more) => `${asks} ${window}: ${parts}${more ? " +more" : ""}`,
  list: (parts) => parts.join(" & "),
};

describe("Today", () => {
  it("knows its window: since the last look, the last day without a mark, the last week for an old mark", () => {
    expect(windowOf(today())).toBe("seen");
    expect(windowOf(today({ seen_at: null, since: "2026-10-24T08:00:00Z" }))).toBe("day");
    expect(windowOf(today({ seen_at: "2026-10-01T00:00:00Z", since: "2026-10-18T08:00:00Z" }))).toBe("week");
  });

  it("counts the items by kind in the hub's order, leaving out kinds with none", () => {
    expect(countsByKind([item("proposal"), item("run_failed"), item("release"), item("run_failed")])).toEqual([
      { kind: "release", count: 1 },
      { kind: "run_failed", count: 2 },
      { kind: "proposal", count: 1 },
    ]);
  });

  it("says what waits and that nothing changed, or what changed and whether there is more", () => {
    expect(summaryOf(today({ needs_you_count: 2 }), WORDS)).toBe("asks(2) nothing(seen)");
    const changed = today({ seen_at: null, items: [item("step_done"), item("cost"), item("step_done")], caught_up: false });
    expect(summaryOf(changed, WORDS)).toBe("asks(0) day: step_donex2 & costx1 +more");
    expect(summaryOf({ ...changed, caught_up: true }, WORDS)).toBe("asks(0) day: step_donex2 & costx1");
  });
});
