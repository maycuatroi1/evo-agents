// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { renderVi } from "@/test/render";

import type { Run, RunChat } from "./queries";
import { RunChatPanel } from "./run-chat";
import { chatEndable, chatReplyable } from "./run-model";

const api = vi.hoisted(() => ({
  chat: null as unknown,
  seen: [] as Request[],
  post: null as null | ((request: Request) => Response),
  login: "octo",
  decisions: {} as Record<string, unknown[]>,
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push(request);
        const url = new URL(request.url);
        if (url.pathname === "/v1/auth/web/csrf") return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (url.pathname === "/v1/projects/demo/runs/41/chat") return Response.json(api.chat);
        if (url.pathname === "/v1/auth/whoami") return Response.json({ login: api.login, admin: false, token: {}, grants: [{ project: "demo", role: "writer", max_level: "internal" }] });
        if (url.pathname === "/v1/projects/demo/decisions" && request.method === "GET") {
          const decisions = api.decisions[url.searchParams.get("run_id") ?? ""] ?? [];
          return Response.json({ decisions, total: decisions.length, limit: 200, offset: 0 });
        }
        if (request.method === "POST" && api.post) return api.post(request);
        return Response.json({ error: "not_found", message: url.pathname }, { status: 404 });
      },
    }),
}));

const RUN = {
  id: 41,
  kind: "author",
  project: "demo",
  plan_id: "",
  plan_revision: null,
  state: "done",
  request: "Let members write plans from the web.",
  queued_at: "2026-10-07T03:00:00Z",
  finish_requested_at: null,
} as unknown as Run;

const CHAT: RunChat = {
  run_id: 44,
  state: "parked",
  status: "waiting",
  owner: "octo",
  plan_id: "web-authoring",
  plan_revision: 2,
  more: false,
  messages: [
    { run_id: 41, seq: 9, author: "agent", login: null, text: "Should **New plan** sit on the Plans page?", at: "2026-10-07T03:05:00Z" },
    { run_id: 41, seq: 12, author: "owner", login: "octo", text: "Yes, in its header.", at: "2026-10-07T03:06:00Z" },
    { run_id: 44, seq: 4, author: "agent", login: null, text: "The plan is on the hub. Anything to change?", at: "2026-10-07T03:20:00Z" },
  ],
};

type Decision = components["schemas"]["Decision"];

const ASKED: Decision = {
  id: 8,
  project: "demo",
  run_id: 44,
  run_state: "parked",
  plan_id: null,
  step_key: null,
  category: "scope",
  question: "Which runs does the first revision cover?",
  context: "The plan can cover **plan runs** only, or author runs as well.",
  options: [
    { key: "plan", label: "Plan runs only", description: null, recommended: true },
    { key: "both", label: "Plan and author runs", description: "One more step.", recommended: false },
  ],
  recommended: "plan",
  state: "open",
  owner: "octo",
  asked_of: "octo",
  answer_option: null,
  answer_text: null,
  answered_by: null,
  answer_run_id: null,
  asked_at: "2026-10-07T03:19:00Z",
  answered_at: null,
  delivered_at: null,
};

const ANSWERED: Decision = {
  ...ASKED,
  id: 5,
  run_id: 41,
  run_state: "parked",
  question: "Should the button sit in the header?",
  state: "answered",
  answer_option: "plan",
  answer_text: "Keep it small.",
  answered_by: "octo",
  answer_run_id: 44,
  asked_at: "2026-10-07T03:04:00Z",
  answered_at: "2026-10-07T03:06:00Z",
};

function show(run: Run, owner: boolean) {
  return renderVi(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RunChatPanel run={run} owner={owner} shown />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  api.chat = CHAT;
  api.seen = [];
  api.post = null;
  api.login = "octo";
  api.decisions = {};
});

describe("RunChatPanel", () => {
  it("shows the request, then the agent's and the owner's messages in order, whose turn it is and the plan written", async () => {
    show(RUN, true);
    const chat = await screen.findByTestId("run-chat");
    expect(chat).toHaveAttribute("data-status", "waiting");
    expect(within(chat).getByTestId("run-chat-status")).toHaveTextContent("Đang chờ bạn");
    const log = within(chat).getByRole("log", { name: "Chat của run #41" });
    const lines = within(log).getAllByRole("article");
    expect(lines.map((line) => line.getAttribute("data-testid"))).toEqual(["run-chat-request", "run-chat-agent", "run-chat-owner", "run-chat-agent"]);
    expect(lines[0]).toHaveTextContent("Yêu cầu10:00 7/10/26Let members write plans from the web.");
    expect(within(lines[1]).getByText("New plan").tagName).toBe("STRONG");
    expect(lines[2]).toHaveTextContent("Bạn10:06 7/10/26Yes, in its header.");
    // The agent's last message, while the chat waits, is the question waiting for a reply.
    expect(within(lines[3]).getByTestId("run-chat-asks")).toHaveTextContent("Đang chờ trả lời");
    expect(within(lines[1]).queryByTestId("run-chat-asks")).toBeNull();
    const plan = within(chat).getByTestId("run-chat-plan");
    expect(plan).toHaveAttribute("href", "/p/demo/plans/web-authoring");
    expect(plan).toHaveTextContent("Plan web-authoring, revision 2");
    // The chat goes on in the run that resumed this parked one.
    expect(within(chat).getByTestId("run-chat-continues")).toHaveTextContent("Chat này tiếp tục ở run #44.");
    expect(within(chat).getByRole("link", { name: "run #44" })).toHaveAttribute("href", "/p/demo/runs/44");
  });

  it("shows each decision as the kit's card after the agent's message that ended the turn which asked it, and how many wait", async () => {
    api.decisions = { "41": [ANSWERED], "44": [ASKED] };
    show(RUN, true);
    const log = await screen.findByRole("log", { name: "Chat của run #41" });
    await waitFor(() => expect(within(log).getAllByTestId("run-chat-decision")).toHaveLength(2));
    // In the order of the chat: the decision asked in the first turn after its agent's message, the open one after the last.
    expect(Array.from(log.children).map((line) => line.getAttribute("data-testid"))).toEqual([
      "run-chat-request",
      "run-chat-agent",
      "run-chat-decision",
      "run-chat-owner",
      "run-chat-agent",
      "run-chat-decision",
    ]);
    const [answered, open] = within(log).getAllByTestId("run-chat-decision");
    expect(answered).toHaveAttribute("id", "run-decision-5");
    expect(answered).toHaveAttribute("data-state", "answered");
    // Answered, the card is one block with the answer; the owner's choice is not a message of the chat again.
    expect(within(answered).getByTestId("decision-answer")).toHaveTextContent("Keep it small.");
    expect(within(answered).queryByRole("radio")).toBeNull();
    expect(within(log).getAllByTestId("run-chat-owner")).toHaveLength(1);
    // Open, it takes the owner's answer with the mouse, the agent's pick chosen.
    expect(open).toHaveAttribute("id", "run-decision-8");
    expect(await within(open).findByRole("radio", { name: /Plan runs only/ })).toBeChecked();
    expect(within(open).getByTestId("decision-send")).toBeInTheDocument();
    expect(within(open).queryByTestId("decision-plan-link")).toBeNull();
    expect(screen.getByTestId("run-chat-status")).toHaveTextContent("1 quyết định đang chờ bạn");
    expect(screen.getByTestId("run-chat-status")).toHaveAttribute("data-open-decisions", "1");
    // The reply box and End chat stay.
    expect(screen.getByTestId("run-chat-reply")).toBeInTheDocument();
    expect(screen.getByTestId("run-chat-end")).toBeInTheDocument();
    const reads = api.seen.filter((request) => new URL(request.url).pathname === "/v1/projects/demo/decisions");
    expect(new Set(reads.map((request) => new URL(request.url).searchParams.get("run_id")))).toEqual(new Set(["41", "44"]));
  });

  it("shows anyone but the owner the open decision's card read-only", async () => {
    api.login = "mona";
    api.decisions = { "44": [ASKED] };
    show(RUN, false);
    const card = await screen.findByTestId("run-chat-decision");
    expect(await within(card).findByTestId("decision-locked")).toHaveTextContent("Chỉ octo");
    expect(within(card).queryByRole("radio")).toBeNull();
    expect(within(card).getAllByTestId("decision-option")).toHaveLength(2);
    expect(screen.getByTestId("run-chat-status")).toHaveTextContent("1 quyết định đang chờ octo");
  });

  it("posts the owner's reply to the run that takes the chat's next message, and refuses a blank one", async () => {
    const user = userEvent.setup();
    api.post = (request) =>
      request.url.endsWith("/runs/44/messages")
        ? Response.json({ id: 3, run_id: 44, seq: 13, from: "octo", text: "Ship it.", at: "2026-10-07T03:21:00Z" }, { status: 201 })
        : Response.json({ error: "conflict", message: "no" }, { status: 409 });
    show(RUN, true);
    const reply = await screen.findByTestId("run-chat-reply");
    expect(reply).toHaveTextContent("Run đã bị park; câu trả lời của bạn chạy tiếp run trên cùng worker.");
    await user.click(within(reply).getByTestId("run-chat-send"));
    expect(within(reply).getByTestId("run-chat-reply-problem")).toHaveTextContent("Hãy gõ câu trả lời trước.");
    await user.type(within(reply).getByRole("textbox", { name: "Trả lời agent" }), "Ship it.");
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(within(reply).getByTestId("run-chat-reply-sent")).toHaveTextContent("Đã gửi."));
    const post = api.seen.find((request) => request.method === "POST");
    expect(post?.url).toBe("http://hub.test/v1/projects/demo/runs/44/messages");
    expect(post?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(await post?.clone().json()).toEqual({ text: "Ship it." });
  });

  it("ends the chat once the owner confirms, on the run that takes its next message", async () => {
    const user = userEvent.setup();
    api.chat = { ...CHAT, run_id: 41, state: "waiting" };
    api.post = () => Response.json({ ...RUN, state: "waiting", finish_requested_at: "2026-10-07T03:22:00Z" });
    show({ ...RUN, state: "waiting" } as Run, true);
    await user.click(await screen.findByTestId("run-chat-end"));
    const dialog = await screen.findByTestId("run-chat-end-dialog");
    expect(dialog).toHaveTextContent("Agent làm xong lượt hiện tại rồi run kết thúc ở trạng thái done.");
    await user.click(within(dialog).getByTestId("run-chat-end-dialog-confirm"));
    await waitFor(() => expect(screen.queryByTestId("run-chat-end-dialog")).toBeNull());
    expect(api.seen.find((request) => request.method === "POST")?.url).toBe("http://hub.test/v1/projects/demo/runs/41/finish");
    expect(await screen.findByText("Run kết thúc ở trạng thái done sau lượt của agent.")).toBeInTheDocument();
  });

  it("reads only for anyone but the owner, and says when the agent has not written yet", async () => {
    api.chat = { ...CHAT, run_id: 41, state: "running", status: "working", plan_id: null, plan_revision: null, messages: [] };
    show({ ...RUN, state: "running" } as Run, false);
    const chat = await screen.findByTestId("run-chat");
    expect(within(chat).getByTestId("run-chat-status")).toHaveTextContent("Agent đang làm");
    expect(within(chat).getByTestId("run-chat-no-plan")).toHaveTextContent("Chưa có plan trên hub");
    expect(within(chat).getByTestId("run-chat-empty")).toHaveTextContent("Agent đang đọc dự án.");
    expect(within(chat).getByTestId("run-chat-typing")).toBeInTheDocument();
    expect(within(chat).queryByTestId("run-chat-reply")).toBeNull();
    expect(within(chat).queryByTestId("run-chat-end")).toBeNull();
    expect(within(chat).getByTestId("run-chat-readonly")).toHaveTextContent("Chỉ octo trả lời trong chat này");
  });

  it("says when the chat cannot be read, with a retry", async () => {
    api.chat = undefined;
    show(RUN, true);
    // An empty body is not a chat: the read fails and the panel says so.
    expect(await screen.findByTestId("run-chat-error")).toHaveTextContent("Không tải được chat.");
    expect(screen.getByTestId("run-chat-retry")).toBeInTheDocument();
  });
});

describe("chatReplyable and chatEndable", () => {
  it("take no reply once the chat ended or its owner ended it, and no end for a queued run", () => {
    expect(chatReplyable({ status: "waiting", state: "waiting" })).toBe(true);
    expect(chatReplyable({ status: "ended", state: "done" })).toBe(false);
    expect(chatReplyable({ status: "working", state: "running" }, { id: 41, finish_requested_at: "2026-10-07T03:22:00Z" }, 41)).toBe(false);
    expect(chatReplyable({ status: "working", state: "running" }, { id: 41, finish_requested_at: "2026-10-07T03:22:00Z" }, 44)).toBe(true);
    expect(chatEndable({ status: "working", state: "queued" }, { finish_requested_at: null })).toBe(false);
    expect(chatEndable({ status: "waiting", state: "parked" }, { finish_requested_at: null })).toBe(true);
    expect(chatEndable({ status: "working", state: "running" }, { finish_requested_at: "2026-10-07T03:22:00Z" })).toBe(false);
  });
});
