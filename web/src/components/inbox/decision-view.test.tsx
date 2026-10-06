// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { queryKeys } from "@/lib/queries";
import { renderVi } from "@/test/render";

import { DecisionView } from "./decision-view";
import { InboxBell } from "./inbox-bell";
import type { Decision } from "./queries";

const api = vi.hoisted(() => ({
  answer: null as null | ((request: Request) => Response | Promise<Response>),
  seen: [] as { url: string; method: string; headers: Headers; body: string }[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push({ url: request.url, method: request.method, headers: request.headers, body: await request.clone().text() });
        if (request.url.endsWith("/v1/auth/web/csrf")) return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (!api.answer) throw new Error("no answer set");
        return api.answer(request);
      },
    }),
}));

vi.mock("next/navigation", () => ({ usePathname: () => "/p/demo/runs/12" }));

const DECISION: Decision = {
  id: 7,
  project: "demo",
  run_id: 12,
  run_state: "waiting",
  plan_id: "rollout",
  step_key: "3",
  category: "deploy",
  question: "Deploy the build to staging now?",
  context: "The checkpoint deploys **staging**.\n\n<script>alert(1)</script>",
  options: [
    { key: "deploy", label: "Deploy to staging now", description: "Takes ten minutes.", recommended: true },
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

function setup(decision: Decision = DECISION, login = "octo") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  client.setQueryData(queryKeys.whoami, { login, admin: false, token: {}, grants: [{ project: "demo", role: "writer", max_level: "internal" }] });
  const view = renderVi(
    <QueryClientProvider client={client}>
      <DecisionView decision={decision} where="inbox" />
    </QueryClientProvider>,
  );
  return { user: userEvent.setup(), client, view };
}

beforeEach(() => {
  api.answer = null;
  api.seen = [];
});

describe("DecisionView", () => {
  it("shows the question, its context as safe Markdown, and the options with the recommended one badged", () => {
    setup();
    expect(screen.getByRole("heading", { level: 2, name: "Deploy the build to staging now?" })).toBeInTheDocument();
    const context = screen.getByTestId("decision-context-markdown");
    expect(context.querySelector("strong")).toHaveTextContent("staging");
    expect(context.querySelector("script")).toBeNull();
    expect(context).toHaveTextContent("<script>alert(1)</script>");
    const deploy = screen.getByTestId("decision-choice-deploy");
    expect(within(deploy).getByTestId("decision-recommended")).toHaveTextContent("Nên chọn");
    expect(within(screen.getByTestId("decision-choice-wait")).queryByTestId("decision-recommended")).toBeNull();
    // Nothing is picked for the owner.
    expect(screen.getAllByRole("radio").filter((radio) => (radio as HTMLInputElement).checked)).toHaveLength(0);
    expect(screen.getByTestId("decision-category")).toHaveTextContent("Triển khai");
    expect(screen.getByTestId("decision-step-link")).toHaveAttribute("href", "/p/demo/plans/rollout/steps/3");
    expect(screen.getByTestId("decision-run-link")).toHaveAttribute("href", "/p/demo/runs/12");
  });

  it("refuses an empty answer without asking the hub", async () => {
    const { user } = setup();
    await user.click(screen.getByTestId("decision-send"));
    expect(screen.getByTestId("decision-problem")).toHaveTextContent("Hãy chọn một phương án hoặc viết câu trả lời trước.");
    expect(screen.getAllByRole("radio")[0]).toHaveFocus();
    expect(api.seen).toHaveLength(0);
  });

  it("sends the option and the words with the session's CSRF header, then shows the answer instead of the form", async () => {
    api.answer = (request) => {
      const answered: Decision = {
        ...DECISION,
        run_state: "running",
        state: "answered",
        answer_option: "wait",
        answer_text: "after the backup",
        answered_by: "octo",
        answer_run_id: 12,
        answered_at: "2026-10-05T07:05:00Z",
      };
      return request.method === "POST" ? Response.json(answered) : Response.json(answered);
    };
    const { user } = setup();
    await user.click(screen.getByRole("radio", { name: /Wait until tomorrow/ }));
    await user.type(screen.getByTestId("decision-text"), "  after the backup ");
    await user.click(screen.getByTestId("decision-send"));
    await waitFor(() => expect(screen.getByTestId("admin-notice-status")).toHaveTextContent("Đã gửi câu trả lời tới run #12."));
    const post = api.seen.find((request) => request.method === "POST");
    expect(post?.url).toBe("http://hub.test/v1/projects/demo/decisions/7/answer");
    expect(post?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(JSON.parse(post?.body ?? "{}")).toEqual({ option: "wait", text: "after the backup" });
  });

  it("says a decision answered meanwhile cannot take another answer, with the hub's own words", async () => {
    api.answer = () =>
      Response.json({ error: "conflict", message: "decision 7 is answered, not open: it takes no answer any more", request_id: "rid-4" }, { status: 409 });
    const { user } = setup();
    await user.click(screen.getByRole("radio", { name: /Deploy to staging now/ }));
    await user.click(screen.getByTestId("decision-send"));
    const alert = await screen.findByTestId("admin-notice-alert");
    expect(alert).toHaveTextContent("Quyết định đã được trả lời hoặc đã đóng trong lúc đó");
    expect(alert).toHaveTextContent("Hub báo: decision 7 is answered, not open");
    expect(alert).toHaveTextContent("rid-4");
  });

  it("names who answered, what and whether the agent got it; a parked run's answer names the run that resumed it", () => {
    setup({
      ...DECISION,
      run_state: "done",
      state: "answered",
      answer_option: "deploy",
      answer_text: "Go ahead.",
      answered_by: "octo",
      answer_run_id: 15,
      answered_at: "2026-10-05T07:05:00Z",
      delivered_at: null,
    });
    expect(screen.queryByTestId("decision-form")).toBeNull();
    const answer = screen.getByTestId("decision-answer");
    expect(within(answer).getByTestId("decision-answer-option")).toHaveTextContent("Deploy to staging now");
    expect(within(answer).getByTestId("decision-answer-text")).toHaveTextContent("Go ahead.");
    expect(within(answer).getByTestId("decision-delivery")).toHaveAttribute("data-delivered", "false");
    expect(within(answer).getByTestId("decision-resumed").querySelector("a")).toHaveAttribute("href", "/p/demo/runs/15");
    const chosen = screen.getAllByTestId("decision-option").filter((option) => option.hasAttribute("data-chosen"));
    expect(chosen.map((option) => option.getAttribute("data-key"))).toEqual(["deploy"]);
    expect(within(chosen[0]).getByTestId("decision-chosen")).toHaveTextContent("Đã chọn");
  });

  it("shows another member the options but no form", () => {
    setup(DECISION, "mona");
    expect(screen.queryByTestId("decision-form")).toBeNull();
    expect(screen.getByTestId("decision-locked")).toHaveTextContent("Chỉ octo, người đã giao run #12, mới trả lời được quyết định này.");
    expect(screen.getAllByTestId("decision-option")).toHaveLength(2);
  });
});

describe("InboxBell", () => {
  it("links to the Inbox and says the unread number and the decisions waiting in its name", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    api.answer = () => Response.json({ unread: 120, open_decisions: 2 });
    renderVi(
      <QueryClientProvider client={client}>
        <InboxBell />
      </QueryClientProvider>,
    );
    const bell = screen.getByTestId("inbox-bell");
    expect(bell).toHaveAttribute("href", "/inbox");
    await waitFor(() => expect(bell).toHaveAttribute("data-unread", "120"));
    expect(bell).toHaveAccessibleName("Inbox: 120 thông báo chưa đọc, 2 quyết định chờ bạn trả lời");
    expect(screen.getByTestId("inbox-bell-count")).toHaveTextContent("99+");
  });
});
