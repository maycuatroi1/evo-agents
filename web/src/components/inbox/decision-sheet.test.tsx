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

const device = vi.hoisted(() => ({ phone: false }));
vi.mock("@/hooks/use-mobile", () => ({ useIsMobile: () => device.phone }));

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

/** A page that opens the sheet from a button and closes it, as Home's Needs you does; the Inbox's with `screen`. */
function Page({ initial, screen }: { initial: DecisionTarget | null; screen: boolean }) {
  const [target, setTarget] = useState<DecisionTarget | null>(initial);
  return (
    <>
      <button type="button" onClick={() => setTarget({ id: 7, project: "demo", parksAt: null })}>
        Answer
      </button>
      <DecisionSheet target={target} onClose={() => setTarget(null)} screen={screen} />
    </>
  );
}

function setup(initial: DecisionTarget | null, grants = ["demo"], screen = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  client.setQueryData(queryKeys.whoami, {
    login: "octo",
    admin: false,
    token: {},
    grants: grants.map((project) => ({ project, role: "writer", max_level: "internal" })),
  });
  renderVi(
    <QueryClientProvider client={client}>
      <Page initial={initial} screen={screen} />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

beforeEach(() => {
  api.answer = (request) =>
    new URL(request.url).pathname === "/v1/projects/demo/decisions/7" ? Response.json(DECISION) : Response.json({ error: "not_found", message: "no such decision" }, { status: 404 });
  api.seen = [];
  device.phone = false;
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

/** Run 12's events: it read the plan, ran the tests, said what it found and asked decision 7. */
const EVENTS = [
  { seq: 41, at: "2026-10-05T06:58:00Z", kind: "state", body: { from: "leased", to: "running" } },
  { seq: 42, at: "2026-10-05T06:58:05Z", kind: "agent_message_chunk", body: { content: { type: "text", text: "Reading the plan." } } },
  { seq: 43, at: "2026-10-05T06:58:10Z", kind: "tool_call", body: { toolCallId: "t1", title: "Bash", kind: "execute", rawInput: { command: "pnpm test" } } },
  { seq: 44, at: "2026-10-05T06:58:58Z", kind: "tool_call_update", body: { toolCallId: "t1", status: "completed" } },
  { seq: 45, at: "2026-10-05T06:59:00Z", kind: "agent_message_chunk", body: { content: { type: "text", text: "Tests pass; staging is next." } } },
  { seq: 46, at: "2026-10-05T07:00:00Z", kind: "system", body: { text: "Asked you", decision: { id: 7, category: "deploy", step: "3" } } },
  { seq: 47, at: "2026-10-05T07:00:00Z", kind: "state", body: { from: "running", to: "waiting" } },
];

describe("DecisionSheet on a phone, from the Inbox", () => {
  beforeEach(() => {
    device.phone = true;
    let held: Decision = DECISION;
    api.answer = async (request) => {
      const url = new URL(request.url);
      if (url.pathname === "/v1/projects/demo/decisions/7" && request.method === "GET") return Response.json(held);
      if (url.pathname === "/v1/projects/demo/decisions/7/answer") {
        const body = (await request.json()) as { option?: string };
        held = { ...DECISION, run_state: "running", state: "answered", answer_option: body.option ?? null, answered_by: "octo", answer_run_id: 12, answered_at: "2026-10-05T07:01:00Z" };
        return Response.json(held);
      }
      if (url.pathname === "/v1/projects/demo/runs/12") return Response.json({ id: 12, project: "demo", state: "waiting", last_seq: 47, worker_id: null });
      if (url.pathname === "/v1/projects/demo/runs/12/events") return Response.json({ run_id: 12, state: "waiting", last_seq: 47, events: EVENTS, more: false });
      return Response.json({ error: "not_found", message: "no such thing" }, { status: 404 });
    };
  });

  it("is a screen of its own: the question as its h1, Back to Inbox, the run, and Send answer in the bar at its foot", async () => {
    const user = setup({ id: 7, project: "demo", parksAt: null }, ["demo"], true);
    const screenView = await screen.findByRole("dialog", { name: "Deploy the build to staging now?" });
    expect(screenView).toHaveAttribute("data-layout", "screen");
    const heading = within(screenView).getByRole("heading", { level: 1, name: "Deploy the build to staging now?" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(within(screenView).getByTestId("decision-screen-run")).toHaveAttribute("href", "/p/demo/runs/12");
    expect(within(screenView).getByRole("button", { name: "Về Inbox" })).toBeInTheDocument();

    // The bar is outside the part that scrolls, and its Send answer submits the form in it, with no key on a phone.
    const bar = within(screenView).getByTestId("decision-bar");
    const panel = within(screenView).getByTestId("decision-panel");
    expect(panel.contains(bar)).toBe(false);
    const send = within(bar).getByRole("button", { name: "Gửi câu trả lời" });
    const form = within(panel).getByTestId("decision-form");
    expect(send).toHaveAttribute("form", form.id);
    expect(within(screenView).queryByTestId("decision-send-kbd")).toBeNull();
    // 16 px in the note, so the phone does not zoom into it.
    expect(within(form).getByTestId("decision-text").className).toMatch(/\btext-base\b/);

    await user.click(send);
    await waitFor(() => expect(api.seen).toContain("POST /v1/projects/demo/decisions/7/answer"));
    expect(await within(screenView).findByTestId("decision-answer")).toHaveTextContent("Deploy to staging now");
    expect(within(screenView).queryByTestId("decision-bar")).toBeNull();
    await waitFor(() => expect(within(screenView).getByRole("heading", { level: 1 })).toHaveFocus());

    await user.click(within(screenView).getByTestId("decision-back"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("reads what the agent did only once its fold opens, and lists the last things oldest first", async () => {
    const user = setup({ id: 7, project: "demo", parksAt: null }, ["demo"], true);
    const screenView = await screen.findByRole("dialog", { name: "Deploy the build to staging now?" });
    const fold = within(screenView).getByTestId("decision-so-far");
    expect(fold).not.toHaveAttribute("open");
    expect(api.seen.some((seen) => seen.includes("/events"))).toBe(false);

    await user.click(within(fold).getByText("Agent đã làm gì đến giờ"));
    const items = await within(fold).findAllByTestId("decision-so-far-item");
    // The moves and the decision on screen are left out: what is left is what the agent said and ran.
    expect(items.map((item) => item.getAttribute("data-type"))).toEqual(["agent", "tool", "agent"]);
    expect(items[0]).toHaveTextContent("Reading the plan.");
    expect(items[1]).toHaveTextContent("Bash pnpm test");
    expect(items[2]).toHaveTextContent("Tests pass; staging is next.");
    expect(within(fold).getByRole("link", { name: "Mở trace của run" })).toHaveAttribute("href", "/p/demo/runs/12");
  });

  it("stays a sheet without `screen`, as Home opens it", async () => {
    setup({ id: 7, project: "demo", parksAt: null }, ["demo"], false);
    const sheet = await screen.findByRole("dialog", { name: "Quyết định #7" });
    expect(sheet).toHaveAttribute("data-layout", "sheet");
    expect(within(sheet).getByTestId("decision-close")).toBeInTheDocument();
  });
});
