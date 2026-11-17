import { describe, expect, it } from "vitest";

import {
  countText,
  HOME_NAV,
  HUB_NAV,
  isActive,
  NAV_GROUPS,
  navRows,
  parseNavGroups,
  pluginArea,
  PROJECT_NAV,
  projectHref,
} from "./nav";
import { activeRunCount, fleetOf } from "./nav-counts";

describe("nav", () => {
  it("names the feature a hub admin may turn off on the items it hides", () => {
    const tagged = [...HOME_NAV, ...PROJECT_NAV, ...HUB_NAV].filter((item) => item.feature);
    expect(tagged.map((item) => [item.label, item.feature])).toEqual([
      ["chats", "chats"],
      ["kg", "kg"],
      ["curator", "curator"],
      ["market", "market"],
    ]);
  });

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

  it("lists the groups in the kit's order, counting the inbox's decisions, unread chats and the project's active runs", () => {
    expect(HOME_NAV.map((item) => [item.label, item.href, item.count ?? null])).toEqual([
      ["home", "/", null],
      ["today", "/today", null],
      ["inbox", "/inbox", "openDecisions"],
      ["chats", "/chats", "unreadChats"],
    ]);
    expect(PROJECT_NAV.map((item) => [item.label, item.group ?? null])).toEqual([
      ["overview", null],
      ["systems", null],
      ["kg", null],
      ["decide", null],
      ["signals", "business"],
      ["problems", "business"],
      ["opportunities", "business"],
      ["goals", "business"],
      ["plans", "development"],
      ["runs", "development"],
      ["loop", "development"],
      ["mergeQueue", "development"],
      ["releases", "development"],
      ["cronJobs", "development"],
      ["insights", "improvement"],
      ["curator", "improvement"],
      ["agents", "knowledge"],
      ["memories", "knowledge"],
      ["skills", "knowledge"],
      ["browser", "knowledge"],
      ["plugins", "apps"],
    ]);
    expect(PROJECT_NAV.filter((item) => item.count).map((item) => [item.label, item.count])).toEqual([
      ["runs", "activeRuns"],
    ]);
    // The systems and the knowledge graph sit with Home, Today, Inbox and Chats, for the project shown.
    expect(PROJECT_NAV.filter((item) => item.top).map((item) => item.label)).toEqual(["systems", "kg"]);
    expect(HUB_NAV.map((item) => [item.label, item.group ?? null])).toEqual([
      ["workers", null],
      ["org", null],
      ["myMemories", null],
      ["globalSkills", null],
      ["market", null],
      ["secrets", "settings"],
      ["accounts", "settings"],
      ["admin", "settings"],
    ]);
    expect(HUB_NAV.filter((item) => item.adminOnly).map((item) => item.label)).toEqual(["admin"]);
  });

  it("keeps the items of a fold next to each other", () => {
    for (const list of [HOME_NAV, PROJECT_NAV, HUB_NAV]) {
      const groups = list.map((item) => item.group ?? null);
      for (const group of Object.keys(NAV_GROUPS)) {
        const at = groups.flatMap((name, index) => (name === group ? [index] : []));
        if (at.length) expect(at.at(-1)! - at[0] + 1).toBe(at.length);
      }
    }
  });

  it("rows a section: items on their own, a fold at its first item, and a fold of one as its item", () => {
    const rows = navRows([
      { id: 1 },
      { id: 2, group: "development" as const },
      { id: 3, group: "development" as const },
      { id: 4 },
      { id: 5, group: "apps" as const },
    ]);
    expect(rows).toEqual([
      { kind: "item", item: { id: 1 } },
      { kind: "group", group: "development", items: [{ id: 2, group: "development" }, { id: 3, group: "development" }] },
      { kind: "item", item: { id: 4 } },
      { kind: "item", item: { id: 5, group: "apps" } },
    ]);
  });

  it("reads the open folds from the cookie: the two cycles until closed, with those opened, leaving out unknown names", () => {
    const cycles = ["business", "development"];
    expect(parseNavGroups(undefined)).toEqual(cycles);
    expect(parseNavGroups("")).toEqual(cycles);
    expect(parseNavGroups(encodeURIComponent("apps,knowledge,apps,bogus"))).toEqual([...cycles, "knowledge", "apps"]);
    // a cycle the visitor closed; a fold of an older web (delivery) is no fold any more
    expect(parseNavGroups(encodeURIComponent("-business,improvement,-bogus,delivery"))).toEqual(["development", "improvement"]);
    expect(parseNavGroups("%E0%A4%A")).toEqual(cycles);
  });

  it("puts a plugin's page in the fold its manifest names, and in Apps when it names none a project's menu has", () => {
    expect(pluginArea("business")).toBe("business");
    expect(pluginArea("development")).toBe("development");
    expect(pluginArea(null)).toBe("apps");
    expect(pluginArea(undefined)).toBe("apps");
    expect(pluginArea("settings")).toBe("apps"); // a fold of the hub, not of a project
    expect(pluginArea("sales")).toBe("apps");
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
      idle: 0,
      ended: 0,
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
