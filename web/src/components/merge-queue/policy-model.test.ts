import { describe, expect, it } from "vitest";

import { onGithub, parseField, policyBody, policyForm, problemCount, readPolicyView, specOf, splitKey } from "./policy-model";

const EFFECTIVE = {
  repos: {
    shop: {
      mode: "off",
      target_branch: "main",
      prod_release: "immediate",
      after_uat_hours: 4,
      jobs: { uat_check: null, prod_deploy: null, prod_check: null, rollback: null },
    },
    "web.app": {
      mode: "off",
      target_branch: "trunk",
      prod_release: "immediate",
      after_uat_hours: 4,
      jobs: { uat_check: null, prod_deploy: null, prod_check: null, rollback: null },
    },
  },
  merge: { opt_in_label: "evo-merge", risk_paths: [], hold_until: "merged", pipeline_minutes: 60 },
  judge: { worker: null, model: null },
  breaker: {
    target_red_minutes: 30,
    reverts_in_a_row: 2,
    revert_rate_factor: 2,
    revert_rate_max: 0.1,
    baseline_min_merges: 10,
    judge_misses_7d: 2,
  },
  audit: { sample_rate: 0.1 },
};

const REPOS = {
  shop: { origin: "https://gitlab.example.org/northwind/shop", listed: true },
  "web.app": { origin: "git@github.com:northwind/web.app.git", listed: true },
};

describe("policy form", () => {
  it("shows every key at its value, a repo's target branch empty while the policy leaves it to the default branch", () => {
    const form = policyForm({ policy: {}, effective: EFFECTIVE });
    expect(form.global["merge.hold_until"]).toBe("merged");
    expect(form.global["audit.sample_rate"]).toBe("0.1");
    expect(form.global["judge.worker"]).toBe("");
    expect(form.repos.shop.target_branch).toBe("");
    expect(form.repos["web.app"].mode).toBe("off");
  });

  it("sends only what differs from the defaults, and keeps what the policy set and the admin left", () => {
    const held = { merge: { pipeline_minutes: 60 } };
    const form = policyForm({ policy: held, effective: EFFECTIVE });
    form.repos.shop = { ...form.repos.shop, mode: "auto", prod_release: "manual", "jobs.uat_check": " uat-check " };
    form.global["breaker.reverts_in_a_row"] = "3";
    form.global["merge.risk_paths"] = "**/billing/**\n\n deploy/** ";
    const result = policyBody(form, held, REPOS);
    expect(result.problems).toBeNull();
    expect(result.body).toEqual({
      merge: { risk_paths: ["**/billing/**", "deploy/**"], pipeline_minutes: 60 },
      breaker: { reverts_in_a_row: 3 },
      repos: { shop: { mode: "auto", prod_release: "manual", jobs: { uat_check: "uat-check" } } },
    });
  });

  it("drops a key the admin put back to its default", () => {
    const held = { breaker: { reverts_in_a_row: 3 } };
    const form = policyForm({ policy: held, effective: { ...EFFECTIVE, breaker: { ...EFFECTIVE.breaker, reverts_in_a_row: 3 } } });
    form.global["breaker.reverts_in_a_row"] = "2";
    expect(policyBody(form, held, REPOS).body).toEqual({});
  });

  it("refuses what the hub refuses: numbers out of range, a bad choice, auto on github.com", () => {
    const form = policyForm({ policy: {}, effective: EFFECTIVE });
    form.global["merge.pipeline_minutes"] = "1.5";
    form.global["breaker.revert_rate_factor"] = "0";
    form.global["audit.sample_rate"] = "1.2";
    form.global["merge.opt_in_label"] = " ";
    form.repos["web.app"] = { ...form.repos["web.app"], mode: "auto" };
    const result = policyBody(form, {}, REPOS);
    expect(result.body).toBeNull();
    expect(result.problems?.global).toEqual({
      "merge.opt_in_label": { code: "line", max: 255 },
      "merge.pipeline_minutes": { code: "whole", min: 1, max: 1440 },
      "breaker.revert_rate_factor": { code: "aboveNumber", min: 0, max: 100 },
      "audit.sample_rate": { code: "number", min: 0, max: 1 },
    });
    expect(result.problems?.repos["web.app"]).toEqual({ mode: { code: "github" } });
    expect(result.problems ? problemCount(result.problems) : 0).toBe(5);
  });

  it("drops a repo the project no longer lists", () => {
    const form = policyForm({ policy: { repos: { gone: { mode: "auto" } } }, effective: { ...EFFECTIVE, repos: { ...EFFECTIVE.repos, gone: { ...EFFECTIVE.repos.shop, mode: "auto" } } } });
    expect(policyBody(form, { repos: { gone: { mode: "auto" } } }, { ...REPOS, gone: { origin: null, listed: false } }).body).toEqual({});
  });

  it("parses each kind of field", () => {
    expect(parseField({ path: "x", kind: "optional", default: null, maxChars: 5 }, "  ")).toEqual({ value: null });
    expect(parseField({ path: "x", kind: "number", default: 0, min: 0, max: 1 }, ".25")).toEqual({ value: 0.25 });
    expect(parseField({ path: "x", kind: "choice", default: "a", options: ["a", "b"] }, "c")).toEqual({ problem: { code: "oneOf", options: ["a", "b"] } });
  });

  it("names a repo's key from its known end, a dot in the repo's name included", () => {
    expect(splitKey("repos.web.app.jobs.uat_check")).toEqual({ repo: "web.app", path: "jobs.uat_check" });
    expect(splitKey("merge.hold_until")).toEqual({ repo: null, path: "merge.hold_until" });
    expect(specOf("repos.shop.after_uat_hours")?.kind).toBe("number");
    expect(specOf("nope.key")).toBeNull();
    expect(onGithub("https://github.com/northwind/web")).toBe(true);
    expect(onGithub("https://gitlab.example.org/northwind/shop")).toBe(false);
  });

  it("reads the page's state in its URL", () => {
    expect(readPolicyView(new URLSearchParams("revision=3&from=0&to=2"))).toEqual({ revision: 3, edit: false, from: 0, to: 2 });
    expect(readPolicyView({ edit: "1", revision: "0" })).toEqual({ revision: null, edit: true, from: null, to: null });
  });
});
