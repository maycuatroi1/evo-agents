import { describe, expect, it } from "vitest";

import type { QueuedMergeRequest } from "@/lib/merge-queue";

import {
  canApproveRelease,
  canAskRevert,
  countByGroup,
  filterRequests,
  forgeUrl,
  groupOf,
  judgeVerdicts,
  queueRepos,
  queueSearch,
  readDays,
  readQueueFilters,
  requestName,
  riskOf,
} from "./model";

function request(patch: Partial<QueuedMergeRequest>): QueuedMergeRequest {
  return {
    id: 1,
    repo: "shop",
    iid: 12,
    source: "plan",
    target_branch: "main",
    source_branch: "evo/northwind",
    run_id: null,
    plan_id: null,
    steps: [],
    change_id: null,
    profile: null,
    model: null,
    title: "Add the invoice export",
    web_url: null,
    state: "ready",
    reason: null,
    head_sha: null,
    fingerprint: null,
    risk: null,
    verdict: null,
    judge_run_id: null,
    queued_at: null,
    merged_at: null,
    created_at: "2026-10-21T08:00:00Z",
    updated_at: "2026-10-21T08:00:00Z",
    events: [],
    rebases: 0,
    ...patch,
  };
}

describe("merge queue model", () => {
  it("groups each state where the page lists it, an unknown one as waiting", () => {
    expect(groupOf("judging")).toBe("waiting");
    expect(groupOf("paused")).toBe("waiting");
    expect(groupOf("testing")).toBe("turn");
    expect(groupOf("blocked")).toBe("blocked");
    expect(groupOf("rolling_back")).toBe("after");
    expect(groupOf("reverted")).toBe("finished");
    expect(groupOf("someday")).toBe("waiting");
    const requests = [request({ state: "ready" }), request({ id: 2, state: "merging" }), request({ id: 3, state: "blocked", repo: "billing" })];
    expect(countByGroup(requests)).toEqual({ waiting: 1, turn: 1, blocked: 1, after: 0, finished: 0 });
    expect(filterRequests(requests, { group: "blocked", repo: null }).map((row) => row.id)).toEqual([3]);
    expect(filterRequests(requests, { group: null, repo: "shop" }).map((row) => row.id)).toEqual([1, 2]);
    expect(queueRepos(requests, [{ repo: "api" }])).toEqual(["api", "billing", "shop"]);
  });

  it("reads and writes the page's filters in its URL, ignoring what it does not know", () => {
    expect(readQueueFilters(new URLSearchParams("state=blocked&repo=shop&mr=7"))).toEqual({ group: "blocked", repo: "shop", open: 7 });
    expect(readQueueFilters({ state: "nope", mr: "-1" })).toEqual({ group: null, repo: null, open: null });
    expect(queueSearch({ group: "after", repo: null, open: 3 })).toBe("state=after&mr=3");
    expect(queueSearch({ group: null, repo: null, open: null })).toBe("");
    expect(readDays("7")).toBe(7);
    expect(readDays(["90"])).toBe(90);
    expect(readDays("12")).toBe(30);
  });

  it("offers an admin the release of a merge the policy releases by hand, and the revert of a merge it followed", () => {
    expect(canApproveRelease({ state: "releasing", uat_result: "passed" }, "manual")).toBe(true);
    expect(canApproveRelease({ state: "releasing", uat_result: "passed" }, "immediate")).toBe(false);
    expect(canApproveRelease({ state: "verifying", uat_result: null }, "manual")).toBe(false);
    expect(canAskRevert({ state: "done", source: "plan" })).toBe(true);
    expect(canAskRevert({ state: "done", source: "revert" })).toBe(false);
    expect(canAskRevert({ state: "ready", source: "plan" })).toBe(false);
  });

  it("names a merge request, and opens only an http link of the forge", () => {
    expect(requestName({ iid: 12, id: 1 })).toBe("!12");
    expect(requestName({ iid: null, id: 4 })).toBe("#4");
    expect(forgeUrl("https://gitlab.example.org/northwind/shop/-/merge_requests/12")).toContain("merge_requests/12");
    expect(forgeUrl("javascript:alert(1)")).toBeNull();
    expect(forgeUrl(null)).toBeNull();
  });

  it("reads the Judges' verdicts and the risk as the hub keeps them", () => {
    const judges = judgeVerdicts({
      fingerprint: "abc",
      first: {
        model: "claude-sonnet",
        passed: false,
        run_id: 9,
        failures: ["tests: no test covers the export"],
        dimensions: { work: { verdict: "pass", reason: "does the step" }, tests: { verdict: "fail", reason: "no test" } },
      },
    });
    expect(judges).toEqual([
      {
        judge: "first",
        model: "claude-sonnet",
        passed: false,
        runId: 9,
        failures: ["tests: no test covers the export"],
        dimensions: [
          { name: "work", passed: true, reason: "does the step" },
          { name: "tests", passed: false, reason: "no test" },
        ],
      },
    ]);
    expect(judgeVerdicts(null)).toEqual([]);
    expect(riskOf({ high: true, reasons: ["shop:deploy/app.yml is a risk path", 3] })).toEqual({ high: true, reasons: ["shop:deploy/app.yml is a risk path"] });
    expect(riskOf(null)).toBeNull();
  });
});
