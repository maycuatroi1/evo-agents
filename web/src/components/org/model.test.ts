import { describe, expect, it } from "vitest";

import { buildTree, endDay, knownLogins, rightsOf, runsByLogin, runsHref, seatNote, subtree, unplaced, utcDay } from "./model";
import type { OrgSeat, OrgUnit, OrgUser, OrgViewer } from "./queries";

const unit = (slug: string, parent: string | null, extra: Partial<OrgUnit> = {}): OrgUnit => ({
  slug,
  name: slug.toUpperCase(),
  kind: parent === null ? "org" : "team",
  parent,
  ends_on: null,
  archived_at: null,
  revision: 1,
  ceiling: null,
  effective: "A5",
  source: `unit:${slug}`,
  ...extra,
});

let nextId = 1;
const seat = (unitSlug: string, login: string | null, extra: Partial<OrgSeat> = {}): OrgSeat => ({
  id: nextId++,
  unit: unitSlug,
  login,
  agent: null,
  project: null,
  role: "member",
  primary: false,
  reports_to: null,
  reports_to_seat: null,
  sponsor: null,
  starts_on: "2026-01-01",
  ends_on: null,
  ended_at: null,
  ceiling: null,
  effective: "A5",
  source: `unit:${unitSlug}`,
  ...extra,
});

const UNITS = [
  unit("acme", null),
  unit("eng", "acme"),
  unit("web", "eng"),
  unit("spring", "eng", { kind: "cohort", ends_on: "2026-10-25" }),
  unit("old", "acme", { archived_at: "2026-02-01T00:00:00Z" }),
];
const TODAY = "2026-10-22";

const viewer = (extra: Partial<OrgViewer> = {}): OrgViewer => ({ login: "ana", admin: false, leads: [], effective: "A3", source: "unit:acme", ...extra });

describe("org model", () => {
  it("reads today in UTC", () => {
    expect(utcDay(Date.parse("2026-10-22T23:30:00Z"))).toBe("2026-10-22");
  });

  it("ends a seat on the earliest of its own end and its cohorts' ends, and flags only ended or soon ending seats", () => {
    const inCohort = seat("spring", "kim");
    const late = seat("web", "lou", { ends_on: "2026-12-01" });
    const soon = seat("web", "mia", { ends_on: "2026-10-29" });
    const past = seat("web", "ned", { ends_on: "2026-10-21" });
    const ended = seat("web", "ora", { ended_at: "2026-10-01T00:00:00Z" });
    expect(endDay(UNITS, inCohort)).toBe("2026-10-25");
    expect(seatNote(UNITS, inCohort, TODAY)).toEqual({ kind: "endsSoon", days: 3, day: "2026-10-25" });
    expect(seatNote(UNITS, late, TODAY)).toBeNull();
    expect(seatNote(UNITS, soon, TODAY)).toEqual({ kind: "endsSoon", days: 7, day: "2026-10-29" });
    expect(seatNote(UNITS, past, TODAY)).toEqual({ kind: "ended" });
    expect(seatNote(UNITS, ended, TODAY)).toEqual({ kind: "ended" });
    expect(seatNote(UNITS, seat("web", "pat"), TODAY)).toBeNull();
  });

  it("builds the tree from the root, hiding archived units and ended seats unless asked", () => {
    const seats = [seat("web", "zed"), seat("web", "amy", { role: "lead" }), seat("web", "bob"), seat("web", "old", { ended_at: "2026-10-01T00:00:00Z" })];
    const tree = buildTree({ units: UNITS, seats }, TODAY, false);
    expect(tree?.unit.slug).toBe("acme");
    expect(tree?.children.map((node) => node.unit.slug)).toEqual(["eng"]);
    const web = tree?.children[0]?.children.find((node) => node.unit.slug === "web");
    expect(web?.seats.map((row) => row.login)).toEqual(["amy", "bob", "zed"]);
    const all = buildTree({ units: UNITS, seats }, TODAY, true);
    expect(all?.children.map((node) => node.unit.slug)).toEqual(["eng", "old"]);
    expect(all?.children[0]?.children.find((node) => node.unit.slug === "web")?.seats).toHaveLength(4);
    expect([...subtree(UNITS, "eng")].sort()).toEqual(["eng", "spring", "web"]);
  });

  it("gives a hub admin every right and every level", () => {
    const rights = rightsOf(viewer({ admin: true }), UNITS);
    expect(rights.any).toBe(true);
    expect(rights.createUnder(UNITS[0])).toBe(true);
    expect(rights.editUnit(UNITS[0])).toBe(true);
    expect(rights.roles("acme")).toEqual(["lead", "member", "intern", "agent"]);
    expect(rights.levels).toEqual(["A0", "A1", "A2", "A3", "A4", "A5"]);
  });

  it("lets a lead manage below their unit, only member and intern seats in it, never above it nor their own seat's ceiling", () => {
    const rights = rightsOf(viewer({ leads: ["eng"] }), UNITS);
    const [acme, eng, web] = UNITS;
    expect(rights.any).toBe(true);
    expect(rights.createUnder(eng)).toBe(true);
    expect(rights.createUnder(web)).toBe(true);
    expect(rights.createUnder(acme)).toBe(false);
    expect(rights.editUnit(eng)).toBe(false);
    expect(rights.editUnit(web)).toBe(true);
    expect(rights.roles("eng")).toEqual(["member", "intern"]);
    expect(rights.roles("web")).toEqual(["lead", "member", "intern", "agent"]);
    expect(rights.roles("acme")).toEqual([]);
    expect(rights.seatCeiling(seat("web", "ana"))).toBe(false);
    expect(rights.seatCeiling(seat("web", "bob"))).toBe(true);
    expect(rights.levels).toEqual(["A0", "A1", "A2", "A3"]);
  });

  it("gives a member without a led unit no write at all", () => {
    const rights = rightsOf(viewer(), UNITS);
    expect(rights.any).toBe(false);
    expect(UNITS.some((one) => rights.createUnder(one) || rights.editUnit(one) || rights.roles(one.slug).length > 0)).toBe(false);
  });

  it("sums each person's runs at work by project and links the runs list", () => {
    const runs = runsByLogin({ running: [{ login: "ana", project: "alpha", running: 1 }, { login: "ana", project: "beta", running: 3 }, { login: "bob", project: "alpha", running: 2 }] });
    expect(runs.get("ana")).toEqual({ total: 4, projects: [{ project: "beta", running: 3 }, { project: "alpha", running: 1 }] });
    expect(runs.get("bob")?.total).toBe(2);
    expect(runsByLogin(undefined).size).toBe(0);
    expect(runsHref("alpha", "ana")).toBe("/p/alpha/runs?state=active&q=ana");
  });

  it("lists as unplaced the people with a grant and no live seat outside the root", () => {
    const user = (login: string, grants: number): OrgUser => ({
      login,
      admin: false,
      signed_in: true,
      created_at: "2026-01-01T00:00:00Z",
      last_seen_at: null,
      active_tokens: 0,
      grants: Array.from({ length: grants }, (_, index) => ({ project: `p${index}`, role: "member" }) as OrgUser["grants"][number]),
    });
    const seats = [seat("acme", "rootonly"), seat("web", "placed"), seat("web", "gone", { ended_at: "2026-10-01T00:00:00Z" })];
    const users = [user("placed", 1), user("rootonly", 1), user("gone", 2), user("nogrant", 0)];
    expect(unplaced(users, { units: UNITS, seats }, TODAY).map((one) => one.login)).toEqual(["gone", "rootonly"]);
    expect(knownLogins({ seats }, users)).toEqual(["gone", "nogrant", "placed", "rootonly"]);
  });
});
