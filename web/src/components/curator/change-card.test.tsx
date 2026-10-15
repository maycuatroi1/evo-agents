// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/react";
import { createTranslator, NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import en from "../../../messages/en.json";
import vi from "../../../messages/vi.json";

import { ChangeFacts } from "./change-card";
import type { Change } from "./queries";

const REPOS = [{ name: "api", origin: "https://github.com/example-org/api.git" }];

const PLANNED: Change = {
  id: 4,
  project: "demo",
  proposal_id: 12,
  plan_id: "curator-12-wait-helper",
  repo: "api",
  branch: "curator/12-wait-helper",
  forge: "github",
  tier: 2,
  state: "planned",
  reason: "the hub has not checked the ruleset of repo api yet: the job curator.changes checks it at its next pass",
  builder_run_id: null,
  judge_run_id: null,
  pr_number: null,
  pr_url: null,
  base_branch: null,
  head_sha: null,
  passed: null,
  verdict: null,
  merged_at: null,
  merge_sha: null,
  created_at: "2026-10-15T01:00:00Z",
  updated_at: "2026-10-15T02:00:00Z",
};

function renderEn(ui: ReactElement) {
  return render(
    <NextIntlClientProvider locale="en" messages={en} timeZone="Asia/Ho_Chi_Minh">
      {ui}
    </NextIntlClientProvider>,
  );
}

describe("ChangeFacts", () => {
  it("links a planned change's plan and branch, and says why it waits", () => {
    renderEn(<ChangeFacts project="demo" change={PLANNED} repos={REPOS} />);
    expect(screen.getByTestId("proposal-change-plan-link")).toHaveAttribute("href", "/p/demo/plans/curator-12-wait-helper");
    expect(screen.getByTestId("proposal-change-repo")).toHaveTextContent("api");
    const branch = screen.getByTestId("proposal-change-branch-link");
    expect(branch).toHaveAttribute("href", "https://github.com/example-org/api/tree/curator/12-wait-helper");
    expect(branch).toHaveAttribute("target", "_blank");
    expect(branch).toHaveAccessibleName("Branch curator/12-wait-helper on its forge (opens in a new tab)");
    expect(screen.getByRole("button", { name: "Copy the branch curator/12-wait-helper" })).toBeInTheDocument();
    expect(screen.queryByTestId("proposal-change-pull")).toBeNull();
    const reason = screen.getByTestId("proposal-change-reason");
    expect(reason).toHaveAttribute("data-tone", "info");
    expect(within(reason).getByRole("heading", { level: 3 })).toHaveTextContent("Why it waits");
    expect(reason).toHaveTextContent("the hub has not checked the ruleset of repo api yet");
  });

  it("says a change left open without a plan has none, and why, in attention", () => {
    const left = { ...PLANNED, plan_id: null, repo: null, branch: null, forge: null, state: "open" as const, reason: "it cannot become the Curator's plan: a Curator's plan works in one repo" };
    renderEn(<ChangeFacts project="demo" change={left} repos={REPOS} />);
    expect(screen.getByTestId("proposal-change-plan")).toHaveTextContent("None: the draft could not become a plan");
    expect(screen.queryByTestId("proposal-change-plan-link")).toBeNull();
    expect(screen.queryByTestId("proposal-change-branch")).toBeNull();
    const reason = screen.getByTestId("proposal-change-reason");
    expect(reason).toHaveAttribute("data-tone", "attention");
    expect(reason).toHaveTextContent("Why it stays open");
  });

  it("links the pull request once the hub opened it, and shows no note without a reason", () => {
    const judged = { ...PLANNED, state: "judged" as const, reason: null, pr_number: 7, pr_url: "https://github.com/example-org/api/pull/7" };
    renderEn(<ChangeFacts project="demo" change={judged} repos={REPOS} />);
    const pull = screen.getByTestId("proposal-change-pr");
    expect(pull).toHaveAttribute("href", "https://github.com/example-org/api/pull/7");
    expect(pull).toHaveAttribute("rel", "noopener noreferrer");
    expect(pull).toHaveAccessibleName("Pull request #7 (opens in a new tab)");
    expect(pull).toHaveTextContent("#7");
    expect(screen.queryByTestId("proposal-change-reason")).toBeNull();
  });

  it("keeps the branch as text when the repo's origin gives no forge page, and never links a pull request but https", () => {
    const elsewhere = { ...PLANNED, pr_number: 3, pr_url: "javascript:alert(1)" };
    renderEn(<ChangeFacts project="demo" change={elsewhere} repos={[{ name: "api", origin: null }]} />);
    expect(screen.getByTestId("proposal-change-branch-link")).not.toHaveAttribute("href");
    expect(screen.queryByTestId("proposal-change-pull")).toBeNull();
  });
});

describe("what accepting does, by tier", () => {
  const TIERS = ["0", "1", "2", "3"] as const;

  it.each([
    ["en", en],
    ["vi", vi],
  ] as const)("says in %s which tiers become a plan and which the hub may merge", (locale, messages) => {
    const t = createTranslator({ locale, messages, namespace: "curator" });
    const next = TIERS.map((tier) => t("proposal.acceptedNext", { tier }));
    const asked = TIERS.map((tier) => t("answer.text.accept", { tier }));
    const done = TIERS.map((tier) => t("answer.doneText.accept", { tier }));
    for (const said of [next, asked, done]) {
      expect(new Set(said).size, said.join(" | ")).toBe(3); // tier 0, tiers 1 and 2, tier 3
      expect(said[1]).toBe(said[2]);
      for (const text of said) expect(text).not.toMatch(/[{}]/);
    }
    if (locale === "en") {
      expect(next[0]).toContain("the hub may merge it");
      expect(next[1]).toContain("Its pull request stays open until a person merges it");
      expect(next[3]).toBe("Tier 3 is never built: its owner makes the change.");
      expect(asked[2]).toContain("makes its draft a plan of the Curator at once");
      expect(asked[3]).toBe("The hub records your yes and builds nothing: a tier 3 change is yours to make.");
    }
  });
});
