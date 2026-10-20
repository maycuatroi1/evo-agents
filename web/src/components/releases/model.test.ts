import { describe, expect, it } from "vitest";

import { CYCLE, TAG } from "@/test/fixtures/releases";

import {
  budgetShare,
  budgetTone,
  failedShown,
  githubCommitUrl,
  githubTagUrl,
  hasTagDetail,
  lacksActions,
  outcomeTone,
  publishedTag,
  releaseRights,
  safeUrl,
  shortCommit,
  watchedTag,
} from "./model";
import { inFlight } from "./queries";

describe("the release page's model", () => {
  it("lets an admin of the project write and everyone else read", () => {
    expect(releaseRights({ role: "admin" })).toEqual({ admin: true });
    expect(releaseRights({ role: "writer" })).toEqual({ admin: false });
    expect(releaseRights({ role: null })).toEqual({ admin: false });
    expect(releaseRights(null)).toEqual({ admin: false });
  });

  it("links a tag and a commit on GitHub, and nothing once the repo is off GitHub", () => {
    expect(githubTagUrl("northwind/fleet", "v0.25.0")).toBe("https://github.com/northwind/fleet/tree/v0.25.0");
    expect(githubCommitUrl("northwind/fleet", "c".repeat(40))).toBe(`https://github.com/northwind/fleet/commit/${"c".repeat(40)}`);
    expect(githubTagUrl(null, "v0.25.0")).toBeNull();
    expect(githubCommitUrl("northwind/fleet", null)).toBeNull();
    expect(shortCommit("abcdef0123456789")).toBe("abcdef0");
    expect(shortCommit(null)).toBe("");
  });

  it("takes only http and https addresses", () => {
    expect(safeUrl("https://github.com/northwind/fleet/actions/runs/7")).toBe("https://github.com/northwind/fleet/actions/runs/7");
    expect(safeUrl("javascript:alert(1)")).toBeNull();
    expect(safeUrl(null)).toBeNull();
  });

  it("says how much of the cap the runs spent, in attention near its end and danger at it", () => {
    expect(budgetShare(50, 200)).toBe(0.25);
    expect(budgetShare(5, 0)).toBe(0);
    expect(budgetTone(0.5)).toBe("normal");
    expect(budgetTone(0.8)).toBe("warning");
    expect(budgetTone(0.95)).toBe("danger");
    expect(budgetTone(1.2)).toBe("danger");
  });

  it("finds a CI the App cannot read in a tag's problem or in the newest check's reason", () => {
    expect(lacksActions({ cycle: CYCLE, last_check: null })).toBe(false);
    const blind = { ...TAG, problem: "actions_permission: the installation grants no actions" };
    expect(lacksActions({ cycle: { ...CYCLE, tags: [blind] }, last_check: null })).toBe(true);
    const reason = "nightly changes no packaging path; the hub could not read the CI of v0.24.0: actions_permission: no actions";
    const check = { ...CHECK_BASE, reason };
    expect(lacksActions({ cycle: null, last_check: check })).toBe(true);
  });

  it("gives a check's outcome its tone", () => {
    expect(outcomeTone("opened")).toBe("neutral");
    expect(outcomeTone("unchanged")).toBe("neutral");
    expect(outcomeTone("paused")).toBe("attention");
    expect(outcomeTone("owner")).toBe("attention");
    expect(outcomeTone("error")).toBe("danger");
  });

  it("names the tag a cycle published and the one it watches", () => {
    const passed = { ...TAG, tag: "v0.25.1", ci_state: "passed" as const };
    expect(publishedTag({ state: "published", tags: [TAG, passed] })?.tag).toBe("v0.25.1");
    expect(publishedTag({ state: "closed", tags: [TAG, passed] })).toBeNull();
    expect(watchedTag({ tags: [TAG, passed] })?.tag).toBe("v0.25.1");
    expect(watchedTag({ tags: [] })).toBeNull();
  });

  it("shows five failed tests at most and counts the rest", () => {
    const many = { failed_tests: Array.from({ length: 7 }, (_, n) => `tests/test_${n}.py::test_it`) };
    expect(failedShown(many)).toEqual({ shown: many.failed_tests.slice(0, 5), more: 2 });
    expect(failedShown({ failed_tests: [] })).toEqual({ shown: [], more: 0 });
    expect(hasTagDetail(TAG)).toBe(true);
    expect(hasTagDetail({ failed_tests: [], problem: null, infra: false })).toBe(false);
  });

  it("reads the release often only while its cycle has work in flight", () => {
    expect(inFlight(null)).toBe(false);
    expect(inFlight(CYCLE)).toBe(true); // its fix run is running
    const idle = { ...CYCLE, runs: CYCLE.runs.map((run) => ({ ...run, state: "done" })), tags: [{ ...TAG, ci_state: "failed" as const }] };
    expect(inFlight(idle)).toBe(false);
    expect(inFlight({ ...idle, tags: [{ ...TAG, ci_state: "running" as const }] })).toBe(true);
    expect(inFlight({ ...CYCLE, state: "stopped" })).toBe(false);
  });
});

const CHECK_BASE = {
  id: 3,
  trigger: "schedule" as const,
  local_day: "2026-10-22",
  checked_at: "2026-10-21T23:00:05Z",
  requested_by: null,
  outcome: "unchanged" as const,
  reason: null,
  base_tag: "v0.24.0",
  base_commit: "b".repeat(40),
  target_commit: "c".repeat(40),
  changed_paths: [],
  cycle_id: null,
  run_id: null,
};
