import { describe, expect, it } from "vitest";

import {
  emptySeatForm,
  emptyUnitForm,
  seatAddBody,
  seatEditBody,
  seatFormOf,
  seatProblems,
  unitCreateBody,
  unitEditBody,
  unitFormOf,
  unitProblems,
} from "./form-model";
import type { OrgSeat, OrgUnit } from "./queries";

const COHORT: OrgUnit = {
  slug: "spring",
  name: "Spring cohort",
  kind: "cohort",
  parent: "eng",
  ends_on: "2026-12-01",
  archived_at: null,
  revision: 4,
  ceiling: "A1",
  effective: "A1",
  source: "unit:spring",
};

const SEAT: OrgSeat = {
  id: 7,
  unit: "spring",
  login: "kim",
  agent: null,
  project: null,
  role: "intern",
  primary: true,
  reports_to: "amy",
  reports_to_seat: 3,
  sponsor: null,
  starts_on: "2026-09-01",
  ends_on: "2026-11-30",
  ended_at: null,
  ceiling: null,
  effective: "A1",
  source: "unit:spring",
};

describe("org forms", () => {
  it("checks a new unit's slug and sends only what the form holds", () => {
    const form = { ...emptyUnitForm("eng"), slug: "Bad slug" };
    expect(unitProblems(form, { adding: true, taken: [] })).toEqual({ slug: "slug" });
    expect(unitProblems({ ...form, slug: "eng" }, { adding: true, taken: ["eng"] })).toEqual({ slug: "slugTaken" });
    expect(unitCreateBody({ ...form, slug: "web" })).toEqual({ slug: "web", parent: "eng", kind: "team" });
    expect(unitCreateBody({ ...form, slug: "fall", kind: "cohort", endsOn: "2027-01-31", level: "A2", note: " pilot ", name: " Fall " })).toEqual({
      slug: "fall",
      parent: "eng",
      kind: "cohort",
      name: "Fall",
      ends_on: "2027-01-31",
      level: "A2",
      note: "pilot",
    });
  });

  it("edits a unit against its revision with only the fields that changed, clearing an end with null", () => {
    const form = unitFormOf(COHORT);
    expect(unitEditBody(COHORT, form)).toEqual({ if_revision: 4 });
    expect(unitEditBody(COHORT, { ...form, name: "Spring", endsOn: "" })).toEqual({ if_revision: 4, name: "Spring", ends_on: null });
    expect(unitEditBody(COHORT, { ...form, parent: "acme" })).toEqual({ if_revision: 4, parent: "acme" });
    expect(unitProblems({ ...form, name: " " }, { adding: false, taken: [] })).toEqual({ name: "name" });
  });

  it("asks an intern for whom they report to and an agent for its sponsor", () => {
    expect(seatProblems({ ...emptySeatForm("spring", "intern"), login: "kim" }, true)).toEqual({ reportsTo: "internReportsTo" });
    const agent = { ...emptySeatForm("spring", "member"), who: "agent" as const, agent: "Bad", project: "alpha" };
    expect(seatProblems(agent, true)).toEqual({ agent: "handle", sponsor: "agentSponsor" });
    expect(seatProblems({ ...emptySeatForm("spring", "member"), login: "kim", startsOn: "2026-10-02", endsOn: "2026-10-01" }, true)).toEqual({ endsOn: "dates" });
  });

  it("adds a person's or an agent's seat with the fields set", () => {
    expect(seatAddBody({ ...emptySeatForm("web", "member", "kim"), primary: true })).toEqual({ unit: "web", role: "member", login: "kim", primary: true });
    const agent = { ...emptySeatForm("web", "member"), who: "agent" as const, agent: "reviewer", project: "alpha", sponsor: "amy" };
    expect(seatAddBody(agent)).toEqual({ unit: "web", role: "agent", agent: "reviewer", project: "alpha", sponsor: "amy" });
  });

  it("edits a seat with only the changes, clearing reports_to and ends_on with null", () => {
    const form = seatFormOf(SEAT);
    expect(seatEditBody(SEAT, form)).toEqual({});
    expect(seatEditBody(SEAT, { ...form, role: "member", reportsTo: "", endsOn: "" })).toEqual({ role: "member", reports_to: null, ends_on: null });
    expect(seatEditBody(SEAT, { ...form, endsOn: "2026-12-15", startsOn: "2026-09-02" })).toEqual({ ends_on: "2026-12-15", starts_on: "2026-09-02" });
  });
});
