import { describe, expect, it } from "vitest";

import type { HeldStop, OrgAutonomy, OrgSeat, OrgUnit, OrgViewer } from "./queries";
import { ALL, heldStop, primaryUnit, stopRightOf, targetName } from "./stops";

const unit = (slug: string, parent: string | null): OrgUnit => ({
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
});

let nextId = 1;
const seat = (unitSlug: string, login: string, extra: Partial<OrgSeat> = {}): OrgSeat => ({
  id: nextId++,
  unit: unitSlug,
  login,
  agent: null,
  project: null,
  role: "member",
  primary: true,
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

const UNITS = [unit("acme", null), unit("eng", "acme"), unit("web", "eng"), unit("ops", "acme")];
const SEATS = [seat("web", "kim"), seat("ops", "lou"), seat("eng", "ana", { role: "lead" })];
const TODAY = "2026-10-22";
const viewer = (extra: Partial<OrgViewer> = {}): OrgViewer => ({ login: "ana", admin: false, leads: [], effective: "A3", source: "unit:acme", ...extra });
const chart = (who: OrgViewer) => ({ units: UNITS, seats: SEATS, viewer: who });

describe("org stops", () => {
  it("reads a person's unit from their primary live seat, the root's without one", () => {
    expect(primaryUnit(chart(viewer()), "kim", TODAY)).toBe("web");
    expect(primaryUnit(chart(viewer()), "nobody", TODAY)).toBe("acme");
  });

  it("lets a person stop their own runs, a lead those of the units they lead and below, an admin anything", () => {
    const member = stopRightOf(chart(viewer({ login: "kim" })), TODAY);
    expect(member({ scope: "login", login: "kim" })).toBe(true);
    expect(member({ scope: "login", login: "lou" })).toBe(false);
    expect(member({ scope: "unit", unit: "web" })).toBe(false);
    expect(member(ALL)).toBe(false);

    const lead = stopRightOf(chart(viewer({ leads: ["eng"] })), TODAY);
    expect(lead({ scope: "unit", unit: "eng" })).toBe(true);
    expect(lead({ scope: "unit", unit: "web" })).toBe(true);
    expect(lead({ scope: "unit", unit: "ops" })).toBe(false);
    expect(lead({ scope: "login", login: "kim" })).toBe(true);
    expect(lead({ scope: "login", login: "lou" })).toBe(false);
    expect(lead(ALL)).toBe(false);

    const admin = stopRightOf(chart(viewer({ admin: true })), TODAY);
    expect(admin(ALL)).toBe(true);
    expect(admin({ scope: "login", login: "lou" })).toBe(true);
  });

  it("finds the stop of exactly a target's scope and names it", () => {
    const stop = (extra: Partial<HeldStop>): HeldStop => ({ id: 1, scope: "login", target: "", login: null, unit: null, reason: null, stopped_at: "2026-10-22T00:00:00Z", ...extra });
    const autonomy: OrgAutonomy = { seats: [], stops: [stop({ scope: "login", login: "kim" }), stop({ id: 2, scope: "unit", unit: "web" })] };
    expect(heldStop(autonomy, { scope: "login", login: "kim" })?.id).toBe(1);
    expect(heldStop(autonomy, { scope: "login", login: "lou" })).toBeUndefined();
    expect(heldStop(autonomy, { scope: "unit", unit: "web" })?.id).toBe(2);
    expect(heldStop(autonomy, ALL)).toBeUndefined();
    expect(heldStop(undefined, ALL)).toBeUndefined();
    expect(targetName(UNITS, { scope: "unit", unit: "web" })).toBe("WEB");
    expect(targetName(UNITS, ALL)).toBeNull();
  });
});
