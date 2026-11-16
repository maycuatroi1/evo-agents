// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";

import en from "../../../messages/en.json";

import { EvolveCard } from "./evolve-card";
import { type Evolution, evolutionSubject, evolveKeys } from "./evolve-queries";

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async () => Response.json({ error: "not_found", message: "no such thing" }, { status: 404 }),
    }),
}));

const GIT: Evolution = {
  id: 7,
  project: "demo",
  run_id: 41,
  target: "git",
  repo: "lumen-kb-docs",
  paths: ["skills/review/SKILL.md"],
  system_id: null,
  agent: null,
  state: "done",
  runtime: "claude-code",
  model: null,
  budget: {},
  tasks: [],
  iterations: 6,
  frontier: [
    { candidate_id: 3, freq: 2 },
    { candidate_id: 5, freq: 1 },
  ],
  best_candidate_id: 3,
  proposal_id: 12,
  change_id: 4,
  reason: null,
  created_at: "2026-11-10T01:00:00Z",
  updated_at: "2026-11-10T02:00:00Z",
};

const AGENT: Evolution = { ...GIT, id: 8, target: "agent_config", repo: null, paths: [], system_id: 2, agent: "reviewer", state: "failed", frontier: [], proposal_id: null, reason: "run #42 ended failed" };

function renderCard(evolutions: Evolution[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(evolveKeys.list("demo"), { project: "demo", evolutions });
  return render(
    <NextIntlClientProvider locale="en" messages={en} timeZone="Asia/Ho_Chi_Minh">
      <QueryClientProvider client={client}>
        <EvolveCard project="demo" />
      </QueryClientProvider>
    </NextIntlClientProvider>,
  );
}

describe("EvolveCard", () => {
  it("lists each evolution with what it rewrites, its state, its front and its proposal", () => {
    renderCard([AGENT, GIT]);
    const rows = screen.getAllByTestId("evolve-row");
    expect(rows.map((row) => row.getAttribute("data-target"))).toEqual(["agent_config", "git"]);
    expect(within(rows[0]).getByTestId("evolve-state")).toHaveTextContent("Failed");
    expect(rows[0]).toHaveTextContent("run #42 ended failed");
    expect(within(rows[1]).getByTestId("evolve-subject")).toHaveTextContent("lumen-kb-docs: skills/review/SKILL.md");
    expect(within(rows[1]).getByTestId("evolve-front")).toHaveTextContent("2");
    expect(within(rows[1]).getByRole("link", { name: "#12" })).toHaveAttribute("href", "/p/demo/curator/proposals/12");
  });

  it("shows nothing for a project with no evolution", () => {
    renderCard([]);
    expect(screen.queryByTestId("evolve-card")).toBeNull();
  });

  it("names an agent evolution by its agent", () => {
    expect(evolutionSubject(AGENT)).toBe("reviewer");
  });
});
