// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { queryKeys } from "@/lib/queries";
import { renderVi } from "@/test/render";

import { DecisionSheet, type DecisionTarget } from "./decision-sheet";
import type { Decision } from "./queries";

const api = vi.hoisted(() => ({
  answer: null as null | ((request: Request) => Response | Promise<Response>),
  seen: [] as string[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push(`${request.method} ${new URL(request.url).pathname}`);
        if (request.url.endsWith("/v1/auth/web/csrf")) return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (!api.answer) throw new Error("no answer set");
        return api.answer(request);
      },
    }),
}));

vi.mock("next/navigation", () => ({ usePathname: () => "/" }));

const DECISION: Decision = {
  id: 7,
  project: "demo",
  run_id: 12,
  run_state: "waiting",
  plan_id: "rollout",
  step_key: "3",
  category: "deploy",
  question: "Deploy the build to staging now?",
  context: "The checkpoint deploys **staging**.",
  options: [
    { key: "deploy", label: "Deploy to staging now", description: null, recommended: true },
    { key: "wait", label: "Wait until tomorrow", description: null, recommended: false },
  ],
  recommended: "deploy",
  state: "open",
  owner: "octo",
  answer_option: null,
  answer_text: null,
  answered_by: null,
  answer_run_id: null,
  asked_at: "2026-10-05T07:00:00Z",
  answered_at: null,
  delivered_at: null,
};

/** A page that opens the sheet from a button and closes it, as Home's Needs you does. */
function Page({ initial }: { initial: DecisionTarget | null }) {
  const [target, setTarget] = useState<DecisionTarget | null>(initial);
  return (
    <>
      <button type="button" onClick={() => setTarget({ id: 7, project: "demo", parksAt: null })}>
        Answer
      </button>
      <DecisionSheet target={target} onClose={() => setTarget(null)} />
    </>
  );
}

function setup(initial: DecisionTarget | null, grants = ["demo"]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  client.setQueryData(queryKeys.whoami, {
    login: "octo",
    admin: false,
    token: {},
    grants: grants.map((project) => ({ project, role: "writer", max_level: "internal" })),
  });
  renderVi(
    <QueryClientProvider client={client}>
      <Page initial={initial} />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

beforeEach(() => {
  api.answer = (request) =>
    new URL(request.url).pathname === "/v1/projects/demo/decisions/7" ? Response.json(DECISION) : Response.json({ error: "not_found", message: "no such decision" }, { status: 404 });
  api.seen = [];
});

describe("DecisionSheet", () => {
  it("opens over the page from a button, the question focused, and closes back to it", async () => {
    const user = setup(null);
    expect(screen.queryByTestId("decision-sheet")).toBeNull();
    const opener = screen.getByRole("button", { name: "Answer" });
    await user.click(opener);
    const sheet = await screen.findByRole("dialog", { name: "Quyết định #7" });
    expect(sheet).toHaveAttribute("data-testid", "decision-sheet");
    const heading = await within(sheet).findByRole("heading", { level: 2, name: "Deploy the build to staging now?" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(within(sheet).getByTestId("decision-form")).toBeInTheDocument();
    expect(within(sheet).getByRole("radio", { name: /Deploy to staging now/ })).toBeChecked();
    // Home knows when the run parks: the sheet does not ask the overview for it.
    expect(api.seen).not.toContain("GET /v1/me/overview");

    await user.click(within(sheet).getByTestId("decision-close"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("finds a decision a link names without its project among the visitor's projects", async () => {
    api.answer = (request) => {
      const path = new URL(request.url).pathname;
      if (path === "/v1/projects/demo/decisions/7") return Response.json(DECISION);
      if (path === "/v1/me/overview") return Response.json({ open_decisions: [] });
      return Response.json({ error: "not_found", message: "no such decision" }, { status: 404 });
    };
    setup({ id: 7, project: null }, ["alpha", "demo"]);
    const sheet = await screen.findByRole("dialog", { name: "Quyết định #7" });
    expect(await within(sheet).findByRole("heading", { level: 2, name: "Deploy the build to staging now?" })).toBeInTheDocument();
    expect(api.seen).toEqual(expect.arrayContaining(["GET /v1/projects/alpha/decisions/7", "GET /v1/projects/demo/decisions/7"]));
  });

  it("says when no project of the visitor holds the decision", async () => {
    setup({ id: 9, project: null }, ["alpha"]);
    const sheet = await screen.findByRole("dialog", { name: "Quyết định #9" });
    expect(await within(sheet).findByTestId("decision-not-found")).toHaveTextContent("Không tìm thấy quyết định #9");
  });
});
