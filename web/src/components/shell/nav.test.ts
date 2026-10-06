import { describe, expect, it } from "vitest";

import { countText, HOME_NAV, HUB_NAV, isActive, PROJECT_NAV, projectHref } from "./nav";
import { activeRunCount, fleetOf } from "./nav-counts";

describe("nav", () => {
  it("builds project links", () => {
    expect(projectHref("demo")).toBe("/p/demo");
    expect(projectHref("demo", "plans")).toBe("/p/demo/plans");
  });

  it("marks the current page, and a section for its subpages", () => {
    expect(isActive("/", "/", false)).toBe(true);
    expect(isActive("/admin", "/", false)).toBe(false);
    expect(isActive("/admin/users", "/admin", false)).toBe(true);
    expect(isActive("/administrator", "/admin", false)).toBe(false);
    expect(isActive("/p/demo/plans", "/p/demo", true)).toBe(false);
    expect(isActive("/p/demo", "/p/demo", true)).toBe(true);
  });

  it("lists the groups in the kit's order, counting the inbox's decisions and the project's active runs", () => {
    expect(HOME_NAV.map((item) => [item.label, item.href, item.count ?? null])).toEqual([
      ["home", "/", null],
      ["inbox", "/inbox", "openDecisions"],
    ]);
    expect(PROJECT_NAV.map((item) => item.label)).toEqual(["overview", "plans", "runs", "memories", "skills", "kg"]);
    expect(PROJECT_NAV.filter((item) => item.count).map((item) => [item.label, item.count])).toEqual([
      ["runs", "activeRuns"],
    ]);
    expect(HUB_NAV.map((item) => item.label)).toEqual(["workers", "myMemories", "globalSkills", "admin"]);
    expect(HUB_NAV.filter((item) => item.adminOnly).map((item) => item.label)).toEqual(["admin"]);
  });

  it("writes a count up to 99, then 99+", () => {
    expect(countText(1)).toBe("1");
    expect(countText(99)).toBe("99");
    expect(countText(100)).toBe("99+");
  });
});

describe("nav counts", () => {
  it("counts the runs in an active state, from the counts of every state", () => {
    const counts = {
      queued: 1,
      leased: 1,
      running: 2,
      interactive: 1,
      verifying: 1,
      waiting: 1,
      review: 1,
      parked: 1,
      done: 7,
      failed: 3,
      lost: 2,
      cancelled: 4,
    };
    expect(activeRunCount({ counts })).toBe(9);
  });

  it("reads the fleet: online is idle or busy, busy holds a run, revoked workers are left out", () => {
    expect(
      fleetOf([
        { status: "online", held_runs: 0 },
        { status: "online", held_runs: 2 },
        { status: "draining", held_runs: 1 },
        { status: "offline", held_runs: 0 },
        { status: "revoked", held_runs: 0 },
      ]),
    ).toEqual({ registered: 4, online: 2, busy: 1 });
    expect(fleetOf([])).toEqual({ registered: 0, online: 0, busy: 0 });
  });
});
