import { describe, expect, it } from "vitest";

import {
  answerBody,
  answerProblem,
  budgetShare,
  charterBody,
  charterForm,
  codeUrl,
  curatorLook,
  curatorRights,
  digestEntry,
  evidenceView,
  isAnswerable,
  NO_PROPOSAL_FILTERS,
  nightOutcome,
  proposalListQuery,
  proposalSearch,
  readCharterView,
  readProposalFilters,
} from "./model";
import type { Charter, CuratorStatus } from "./queries";

const CHARTER: Charter = {
  project: "demo",
  revision: 3,
  updated_by: "octo",
  updated_at: "2026-10-07T10:00:00Z",
  worker_id: 4,
  schedule_owner: "octo",
  goals: [{ id: "night-shift", what: "Run the approved plans at night." }],
  window: { start: "22:00", end: "06:00", timezone: "Asia/Ho_Chi_Minh" },
  worker: "night-mac",
  night_budget_usd: 2,
  run_budget_usd: 0.5,
  run_max_turns: 40,
  run_minutes: 30,
  max_runs_per_night: 3,
  night_plans: ["fleet"],
  max_decisions_per_day: 5,
  brief_at: "06:30",
  auto_merge: [0],
  protected_paths: ["curator.yaml", ".github/workflows/**"],
  circuit_breaker: { max_failed_in_a_row: 2 },
  outcome_days: 7,
  review: { lenses: 3, days: 7, budget_usd: null },
  reviewer: { runtime: "claude-code", model: null },
  builder: { runtime: "claude-code", model: "claude-opus-5-5" },
  judge: { runtime: "codex", model: null, hidden_checks: ["python -m pytest -q tests/hidden"] },
};

const schedule = (owner: string, paused: boolean): CuratorStatus["schedules"][number] => ({
  id: 1,
  kind: "night_shift",
  owner,
  worker_id: 4,
  worker: "night-mac",
  paused_at: paused ? "2026-10-07T23:00:00Z" : null,
  paused_by: paused ? owner : null,
});

describe("the proposals' filters", () => {
  it("reads the URL's filters, keeping only values the API takes", () => {
    expect(readProposalFilters(new URLSearchParams("state=open&tier=2&lens=environment&run=12&page=3"))).toEqual({
      state: "open",
      tier: 2,
      lens: "environment",
      run: 12,
      page: 3,
    });
    expect(readProposalFilters(new URLSearchParams("state=closed&tier=7&lens=vibes&run=-1&page=0"))).toEqual(NO_PROPOSAL_FILTERS);
    expect(readProposalFilters(new URLSearchParams("tier=0")).tier).toBe(0);
    expect(readProposalFilters(new URLSearchParams("tier=")).tier).toBeNull();
  });

  it("writes them back in a stable order and asks the API for the page they name", () => {
    expect(proposalSearch(NO_PROPOSAL_FILTERS)).toBe("");
    const filters = { state: "deferred" as const, tier: 0 as const, lens: "cost" as const, run: 4, page: 2 };
    expect(proposalSearch(filters)).toBe("?state=deferred&tier=0&lens=cost&run=4&page=2");
    expect(readProposalFilters(new URLSearchParams(proposalSearch(filters).slice(1)))).toEqual(filters);
    expect(proposalListQuery(filters)).toEqual({ state: "deferred", tier: 0, lens: "cost", run: 4, limit: 50, offset: 50 });
  });
});

describe("where the night shift stands and who may act", () => {
  it("says the state in a word, off without a charter", () => {
    expect(curatorLook({ charter: null, state: null })).toBe("off");
    expect(curatorLook({ charter: CHARTER, state: "paused" })).toBe("paused");
  });

  it("lets an admin write the charter and answer, and an admin or a schedule's owner pause", () => {
    const status = { schedules: [schedule("octo", false)] };
    expect(curatorRights(status, { login: "lin", role: "admin" })).toEqual({ charter: true, answer: true, pause: true });
    expect(curatorRights(status, { login: "octo", role: "writer" })).toEqual({ charter: false, answer: false, pause: true });
    expect(curatorRights(status, { login: "bob", role: "reader" })).toEqual({ charter: false, answer: false, pause: false });
    expect(curatorRights({ schedules: [] }, { login: "lin", role: "admin" }).pause).toBe(false);
    expect(curatorRights(status, null)).toEqual({ charter: false, answer: false, pause: false });
  });
});

describe("answers", () => {
  it("answers an open or deferred proposal only", () => {
    expect(isAnswerable({ state: "open" })).toBe(true);
    expect(isAnswerable({ state: "deferred" })).toBe(true);
    expect(isAnswerable({ state: "accepted" })).toBe(false);
    expect(isAnswerable({ state: "dropped" })).toBe(false);
  });

  it("refuses a note of two lines or too long, and days out of 1 to 90 for a deferral", () => {
    expect(answerProblem("accept", "", "7")).toBeNull();
    expect(answerProblem("accept", "two\nlines", "7")).toBe("noteLine");
    expect(answerProblem("reject", "x".repeat(2001), "7")).toBe("noteLong");
    expect(answerProblem("defer", "", "0")).toBe("days");
    expect(answerProblem("defer", "", "2.5")).toBe("days");
    expect(answerProblem("defer", "", "90")).toBeNull();
    expect(answerProblem("accept", "", "0")).toBeNull(); // days go with a deferral only
  });

  it("sends the note when there is one and the days of a deferral only", () => {
    expect(answerBody("accept", "  ", "7")).toEqual({ action: "accept" });
    expect(answerBody("reject", " not now ", "7")).toEqual({ action: "reject", note: "not now" });
    expect(answerBody("defer", "", "14")).toEqual({ action: "defer", defer_days: 14 });
  });
});

describe("evidence", () => {
  it("links code on GitHub and GitLab at the commit or the default branch, and nowhere for other origins", () => {
    expect(codeUrl("https://github.com/org/api.git", "abc123", "src/a b.py", 12)).toBe("https://github.com/org/api/blob/abc123/src/a%20b.py#L12");
    expect(codeUrl("git@github.com:org/api.git", "main", "README.md", null)).toBe("https://github.com/org/api/blob/main/README.md");
    expect(codeUrl("https://gitlab.example.org/group/sub/api", "main", "x.py", 3)).toBe("https://gitlab.example.org/group/sub/api/-/blob/main/x.py#L3");
    expect(codeUrl("file:///srv/api", "main", "x.py", 1)).toBeNull();
    expect(codeUrl(null, "main", "x.py", 1)).toBeNull();
    expect(codeUrl("https://github.com/org/api", null, "x.py", 1)).toBeNull();
  });

  it("reads each kind of evidence as the hub resolved it", () => {
    const repos = [{ name: "api", origin: "https://github.com/org/api", default_branch: "main" }];
    expect(evidenceView({ kind: "run", run_id: 12, seq: 4, resolved: "run_event" }, "demo", repos)).toEqual({
      kind: "run",
      runId: 12,
      seq: 4,
      href: "/p/demo/runs/12?view=log",
    });
    expect(evidenceView({ kind: "session", session_id: "s-1", field: "errors", index: 0 }, "demo", repos)).toEqual({
      kind: "session",
      session: "s-1",
      field: "errors",
      index: 0,
    });
    expect(evidenceView({ kind: "code", repo: "api", path: "a.py", line: 3 }, "demo", repos)).toMatchObject({
      kind: "code",
      url: "https://github.com/org/api/blob/main/a.py#L3",
    });
    expect(evidenceView({ kind: "code", repo: "gone", path: "a.py" }, "demo", repos)).toMatchObject({ kind: "code", url: null });
    expect(evidenceView({ kind: "run" }, "demo", repos).kind).toBe("other");
  });

  it("finds the digest's entry a piece names, as text", () => {
    const digest = { errors: [{ text: "blocked", n: 3 }], user_turns: ["no, not sleep"] };
    expect(digestEntry(digest, "user_turns", 0)).toBe("no, not sleep");
    expect(digestEntry(digest, "errors", 0)).toContain('"blocked"');
    expect(digestEntry(digest, "errors", 5)).toBeNull();
    expect(digestEntry(digest, null, null)).toBeNull();
  });
});

describe("the charter as a form", () => {
  it("writes back the charter it was filled from", () => {
    const result = charterBody(charterForm(CHARTER, { worker: "", timezone: "" }));
    expect(result.errors).toBeNull();
    const { project: _p, revision: _r, updated_by: _u, updated_at: _a, worker_id: _w, schedule_owner: _o, ...body } = CHARTER;
    expect(result.body).toEqual(body);
  });

  it("fills a first charter with the hub's defaults, the visitor's worker and time zone", () => {
    const form = charterForm(null, { worker: "mini", timezone: "Europe/Paris" });
    const result = charterBody(form);
    expect(result.errors).toBeNull();
    expect(result.body).toMatchObject({
      window: { start: "22:00", end: "06:00", timezone: "Europe/Paris" },
      worker: "mini",
      night_budget_usd: 5,
      run_budget_usd: null,
      max_runs_per_night: 6,
      auto_merge: [],
      judge: { runtime: "claude-code", model: null, hidden_checks: [] },
    });
  });

  it("keeps the Judge's checks when the visitor may not see them", () => {
    const form = charterForm({ ...CHARTER, judge: { runtime: "codex", model: null, hidden_checks: null } }, { worker: "", timezone: "" });
    expect(form.hiddenChecks).toBeNull();
    expect(charterBody(form).body?.judge?.hidden_checks).toBeNull();
  });

  it("refuses what the hub refuses, field by field", () => {
    const form = charterForm(CHARTER, { worker: "", timezone: "" });
    const bad = charterBody({
      ...form,
      goals: [{ id: "Bad Id", what: "x" }],
      windowEnd: "22:00",
      timezone: "not a zone",
      runBudget: "5",
      runMinutes: "5",
      nightPlans: "fleet\nNot A Plan",
      reviewDays: "31",
      builder: { runtime: "claude-code", model: "two\nlines" },
    });
    expect(bad.body).toBeNull();
    expect(bad.errors).toEqual({
      goals: { code: "goalId", line: 1 },
      windowEnd: { code: "sameTime" },
      timezone: { code: "timezone" },
      runBudget: { code: "overNight" },
      runMinutes: { code: "whole", min: 10, max: 720 },
      nightPlans: { code: "planId", line: 2 },
      reviewDays: { code: "whole", min: 1, max: 30 },
      builderModel: { code: "model", max: 200 },
    });
    expect(charterBody({ ...form, goals: [{ id: "a", what: "x" }, { id: "a", what: "y" }] }).errors).toEqual({ goals: { code: "goalRepeated", id: "a" } });
    expect(charterBody({ ...form, nightBudget: "" }).errors).toEqual({ nightBudget: { code: "required" } });
    expect(charterBody({ ...form, briefAt: "7:00" }).errors).toEqual({ briefAt: { code: "time" } });
  });

  it("takes the days before an outcome from 1 to 90, as the hub does", () => {
    const form = charterForm(CHARTER, { worker: "", timezone: "" });
    expect(charterBody({ ...form, outcomeDays: "14" }).body?.outcome_days).toBe(14);
    expect(charterBody({ ...form, outcomeDays: "0" }).errors).toEqual({ outcomeDays: { code: "whole", min: 1, max: 90 } });
    expect(charterBody({ ...form, outcomeDays: "91" }).errors).toEqual({ outcomeDays: { code: "whole", min: 1, max: 90 } });
    expect(charterBody(charterForm(null, { worker: "mini", timezone: "UTC" })).body?.outcome_days).toBe(7);
  });

  it("reads which revision the page shows and whether it edits", () => {
    expect(readCharterView(new URLSearchParams("revision=2"))).toEqual({ revision: 2, edit: false });
    expect(readCharterView(new URLSearchParams("edit=1"))).toEqual({ revision: null, edit: true });
    expect(readCharterView(new URLSearchParams("edit=1&revision=2"))).toEqual({ revision: 2, edit: false });
    expect(readCharterView(new URLSearchParams("revision=0"))).toEqual({ revision: null, edit: false });
  });
});

describe("nights", () => {
  it("says how a night went and how much of its budget it spent", () => {
    expect(nightOutcome({ runs: 3, done: 1, failed: 1, active: 1 })).toBe("active");
    expect(nightOutcome({ runs: 3, done: 2, failed: 1, active: 0 })).toBe("failed");
    expect(nightOutcome({ runs: 2, done: 2, failed: 0, active: 0 })).toBe("done");
    expect(nightOutcome({ runs: 0, done: 0, failed: 0, active: 0 })).toBe("empty");
    expect(budgetShare(0.5, 2)).toBe(0.25);
    expect(budgetShare(5, 2)).toBe(1);
    expect(budgetShare(1, null)).toBe(0);
  });
});
