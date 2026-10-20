// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import en from "../../../messages/en.json";

import { type ChangeReplay, insightKeys } from "./insight-queries";
import type { Change } from "./queries";
import { ReplayCard } from "./replay-card";

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async () => Response.json({ error: "not_found", message: "no such thing" }, { status: 404 }),
    }),
}));

const CHANGE: Change = {
  id: 4,
  project: "demo",
  proposal_id: 12,
  plan_id: "curator-12-skill",
  repo: "api",
  branch: "curator/12-skill",
  forge: "github",
  tier: 0,
  state: "judged",
  reason: null,
  builder_run_id: 30,
  judge_run_id: 31,
  pr_number: 7,
  pr_url: "https://github.com/example-org/api/pull/7",
  base_branch: "main",
  head_sha: "c".repeat(40),
  passed: true,
  verdict: null,
  merged_at: null,
  merge_sha: null,
  created_at: "2026-10-15T01:00:00Z",
  updated_at: "2026-10-15T02:00:00Z",
};

function replay(id: number, a: boolean | null, b: boolean | null, split: "holdout" | "select" = "holdout"): ChangeReplay["replays"][number] {
  const arm = (passed: boolean | null) => (passed === null ? null : { passed, harness_sha: "c".repeat(40), cost_usd: 0.5, turns: 12 });
  return {
    id,
    change_id: 4,
    task_id: id,
    plan_id: "fleet",
    step_key: String(id),
    repo: "api",
    title: `Step ${id} of fleet`,
    split,
    run_id: 40 + id,
    state: a === null || b === null ? "running" : "done",
    attempts: 1,
    base_sha: "d".repeat(40),
    a: arm(a),
    b: arm(b),
    reason: null,
    created_at: "2026-10-16T01:00:00Z",
    updated_at: "2026-10-16T02:00:00Z",
  };
}

const WON: ChangeReplay = {
  project: "demo",
  change_id: 4,
  tasks: 6,
  replays: [replay(1, true, true), replay(2, false, true), replay(3, null, null)],
  arms: { a: { replays: 2, passed: 1, tool_errors: 4, duration_ms: 90_000 }, b: { replays: 2, passed: 2, tool_errors: 1, duration_ms: 60_000 } },
  verdict: {
    won: true,
    reason: "arm b passed more of the 6 holdout tasks than arm a (5 against 4)",
    tasks: 6,
    a: { passed: 4, cost_usd: 1.2, turns: 30 },
    b: { passed: 5, cost_usd: 0.9, turns: 33 },
    saving: { cost_usd: 0.25, turns: -0.1 },
    paths: ["skills/deploy/SKILL.md"],
  },
};

function withData(ui: ReactElement, data: ChangeReplay | null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  if (data) client.setQueryData(insightKeys.replay("demo", 4), data);
  return <QueryClientProvider client={client}>{ui}</QueryClientProvider>;
}

function renderEn(ui: ReactElement) {
  return render(
    <NextIntlClientProvider locale="en" messages={en} timeZone="Asia/Ho_Chi_Minh">
      {ui}
    </NextIntlClientProvider>,
  );
}

describe("ReplayCard", () => {
  it("says the replays won, that the change merges at tier 0, and sets arm b beside arm a", () => {
    renderEn(withData(<ReplayCard project="demo" change={CHANGE} proposalTier={2} />, WON));
    const card = screen.getByTestId("proposal-replay");
    expect(within(card).getByTestId("replay-verdict")).toHaveTextContent("Replays won");
    expect(within(card).getByTestId("replay-reason")).toHaveTextContent("arm b passed more of the 6 holdout tasks");
    expect(within(card).getByTestId("replay-tier")).toHaveTextContent("merges at tier 0 instead of its proposal's tier 2");
    const arms = within(card).getByTestId("replay-arms");
    expect(arms).toHaveAttribute("role", "region");
    expect(arms).toHaveAccessibleName("A, the default branch's harness, against B, this change's");
    expect(arms).toHaveAttribute("tabindex", "0");
    const cost = within(arms).getAllByTestId("replay-arm-row").find((row) => row.getAttribute("data-key") === "cost");
    expect(cost).toHaveTextContent("Mean cost$1.20$0.9025% lower");
    const turns = within(arms).getAllByTestId("replay-arm-row").find((row) => row.getAttribute("data-key") === "turns");
    expect(turns).toHaveTextContent("Mean turns303310% higher");
    const lines = within(card).getAllByTestId("replay");
    expect(lines).toHaveLength(3);
    expect(within(lines[1]).getByText("Failed")).toBeInTheDocument();
    expect(within(lines[2]).getAllByText("Not reported")).toHaveLength(2);
  });

  it("says in one line, in the visitor's language, that a change was not replayed", () => {
    renderVi(withData(<ReplayCard project="demo" change={{ ...CHANGE, tier: 1 }} proposalTier={1} />, { ...WON, replays: [], tasks: 3 }));
    expect(screen.getByTestId("replay-none")).toHaveTextContent("Chưa replay. Ca đêm chỉ replay thay đổi của skill, AGENTS.md hay CLAUDE.md, trên 3 tác vụ giữ lại");
    expect(screen.queryByTestId("replay-tier")).toBeNull();
  });
});
