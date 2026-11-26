import { describe, expect, it } from "vitest";

import type { components } from "@/lib/api/schema";

import { homeRail, inThisWeek, railTitle } from "./home-rail";

type Summary = components["schemas"]["ChatSummary"];
type Brief = components["schemas"]["WorkBrief"];

// Thursday 2026-11-26, 10:00 in Ho Chi Minh City (UTC+7).
const NOW = Date.parse("2026-11-26T03:00:00Z");
const ZONE = "Asia/Ho_Chi_Minh";
const DAY = 86_400_000;

let next = 1;
function task(work: Partial<Brief>, at: number, extra: Partial<Summary> = {}): Summary {
  const id = next++;
  return {
    id,
    title: `Work ${id}`,
    created_by: "linh",
    created_at: new Date(at - 60_000).toISOString(),
    last_message_at: new Date(at).toISOString(),
    last_seq: 3,
    unread: 0,
    people: ["linh"],
    agents: ["lumen-lead", "lumen-helper"],
    kind: "work",
    image_url: null,
    direct_agent: null,
    faces: [],
    last_message: null,
    work: {
      trace_id: id,
      system: {
        id: 1,
        slug: "desk",
        name: "Northwind desk",
        project: "northwind",
      },
      title: `Work ${id}`,
      status: "done",
      agent: null,
      waiting: null,
      outcome: null,
      left: null,
      ...work,
    } as Brief,
    ...extra,
  };
}

const ids = (chats: readonly Summary[]) => chats.map((chat) => chat.id);

describe("homeRail", () => {
  it("puts a task in Needs you", () => {
    const asks = task({ status: "needs_person" }, NOW - 30 * DAY);
    const failedNow = task({ status: "failed" }, NOW - 2 * DAY);
    const failedLong = task({ status: "failed" }, NOW - 8 * DAY);
    const notMet = task(
      {
        status: "done",
        outcome: "not_met",
        left: "Two replies wait for a refund rule",
      },
      NOW - 40 * DAY,
    );
    // Marked handled: the hub reads its outcome as null, and left stays.
    const handled = task(
      {
        status: "done",
        outcome: null,
        left: "Two replies wait for a refund rule",
      },
      NOW - DAY,
    );
    const met = task({ status: "done", outcome: "met" }, NOW - DAY);
    const running = task({ status: "running" }, NOW);
    const rail = homeRail([asks, failedNow, failedLong, notMet, handled, met, running], "lumen-lead", NOW, ZONE);
    expect(ids(rail.needsYou)).toEqual([asks.id, failedNow.id, notMet.id]);
    expect(ids(rail.running)).toEqual([running.id]);
    expect(ids(rail.thisWeek)).toEqual([handled.id, met.id]);
    expect(ids(rail.earlier)).toEqual([failedLong.id]);
  });

  it("keeps the chats of the agent chosen: its tasks in sections, its group and settle chats apart", () => {
    const mine = task({ status: "running" }, NOW);
    const other = task({ status: "running" }, NOW, {
      agents: ["lumen-scribe"],
    });
    const group = task({}, NOW, { kind: "group", work: null });
    const settle = task({}, NOW, { kind: "settle", work: null });
    const direct = task({}, NOW, { kind: "direct", work: null });
    const rail = homeRail([mine, other, group, settle, direct], "lumen-lead", NOW, ZONE);
    expect(ids(rail.running)).toEqual([mine.id]);
    expect(ids(rail.groups)).toEqual([group.id, settle.id]);
  });

  it("finds tasks by their title", () => {
    const refunds = task({ status: "running", title: "@lumen-lead Draft the refund replies" }, NOW);
    const notes = task({ status: "running", title: "Release notes" }, NOW);
    expect(ids(homeRail([refunds, notes], "lumen-lead", NOW, ZONE, "REFUND").running)).toEqual([refunds.id]);
  });
});

describe("inThisWeek", () => {
  it("starts the week on Monday by the viewer's calendar", () => {
    // Monday 2026-11-23 00:30 in Ho Chi Minh City is Sunday 17:30 UTC.
    expect(inThisWeek(Date.parse("2026-11-22T17:30:00Z"), NOW, ZONE)).toBe(true);
    expect(inThisWeek(Date.parse("2026-11-22T17:30:00Z"), NOW, "UTC")).toBe(false);
    expect(inThisWeek(Date.parse("2026-11-22T16:30:00Z"), NOW, ZONE)).toBe(false);
  });
});

describe("railTitle", () => {
  it("drops leading mentions, a trailing link and the prefix of a chat named after its first message", () => {
    expect(railTitle("@lumen-lead @lumen-helper fix the deploy")).toBe("fix the deploy");
    expect(railTitle("Draft the refund replies https://lumenops.example/tickets/12")).toBe("Draft the refund replies");
    expect(railTitle("Linh + @lumen-lead: check the nightly build")).toBe("check the nightly build");
    expect(railTitle("@lumen-lead")).toBe("@lumen-lead");
  });
});
