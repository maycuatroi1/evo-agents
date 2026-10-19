// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import type { Chat, ChatAgent, GroupMessage } from "@/lib/chats";
import { renderVi } from "@/test/render";

import { ChatComposer } from "./composer";
import { ReactionChips } from "./reactions";

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

function agent(handle: string, overrides: Partial<ChatAgent> = {}): ChatAgent {
  return { handle, project: "demo", name: handle, color: "#2563eb", icon_url: null, is_default: false, deleted: false, added_by: "octo", added_at: "2026-10-14T08:00:00Z", ...overrides };
}

const CHAT = {
  id: 7,
  title: "Release",
  created_by: "octo",
  created_at: "2026-10-14T08:00:00Z",
  people: [
    { login: "octo", added_by: "octo", added_at: "2026-10-14T08:00:00Z" },
    { login: "mai", added_by: "octo", added_at: "2026-10-14T08:00:00Z" },
  ],
  agents: [agent("reviewer", { name: "Code reviewer" }), agent("fixer", { name: "Fixer" }), agent("demo", { name: "Demo", is_default: true })],
  label: [],
  restricted: false,
  last_seq: 3,
  last_read_seq: 3,
  budget_usd: 5,
  kind: "group",
} satisfies Chat;

function setup(onSent = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  renderVi(
    <QueryClientProvider client={client}>
      <ChatComposer chat={CHAT} me="octo" replyTo={null} onSent={onSent} label="Tin gửi tới Release" placeholder="Viết tin" hint="Gợi ý" testId="chat-composer" />
    </QueryClientProvider>,
  );
  return { user: userEvent.setup(), box: screen.getByTestId("chat-composer-text"), onSent };
}

beforeEach(() => {
  api.answer = null;
  api.seen = [];
});

describe("ChatComposer", () => {
  it("suggests the chat's agents, projects and people after @, moves with the arrows and puts the name in with Enter", async () => {
    const { user, box } = setup();
    await user.click(box);
    await user.keyboard("Nhờ @");
    const menu = screen.getByTestId("mention-menu");
    expect([...menu.querySelectorAll("[data-testid=mention-option]")].map((option) => option.getAttribute("data-name"))).toEqual(["fixer", "reviewer", "demo", "mai"]);
    expect(box).toHaveAttribute("aria-controls", menu.id);
    await user.keyboard("re");
    expect(screen.getAllByTestId("mention-option").map((option) => option.getAttribute("data-name"))).toEqual(["reviewer"]);
    expect(box).toHaveAttribute("aria-activedescendant", screen.getByTestId("mention-option").id);
    await user.keyboard("{Enter}");
    expect(box).toHaveValue("Nhờ @reviewer ");
    expect(screen.queryByTestId("mention-menu")).toBeNull();
    expect(screen.getByTestId("chat-composer-hint")).toHaveTextContent("Giao việc cho @reviewer.");
    await user.keyboard("và @");
    await user.keyboard("{ArrowDown}{ArrowDown}{Tab}");
    expect(box).toHaveValue("Nhờ @reviewer và @demo ");
    await user.keyboard("@m{Escape}");
    expect(screen.queryByTestId("mention-menu")).toBeNull();
    expect(box).toHaveValue("Nhờ @reviewer và @demo @m");
  });

  it("says when a message names more agents than one message hands work to", async () => {
    const chat = { ...CHAT, agents: [...CHAT.agents, agent("docs")] };
    const client = new QueryClient();
    renderVi(
      <QueryClientProvider client={client}>
        <ChatComposer chat={chat} me="octo" replyTo={null} onSent={vi.fn()} label="Tin" placeholder="Viết tin" hint="Gợi ý" testId="chat-composer" />
      </QueryClientProvider>,
    );
    await userEvent.setup().type(screen.getByTestId("chat-composer-text"), "@reviewer @fixer @demo @docs xem giúp ");
    expect(screen.getByTestId("chat-composer-hint")).toHaveTextContent("Giao việc cho @reviewer, @fixer và @demo. Một tin giao việc cho tối đa 3 agent: @docs không nhận việc.");
  });

  it("sends with the session's CSRF header and empties the box; a refusal of the hub shows with its words", async () => {
    const sent: GroupMessage = { id: 9, chat_id: 7, seq: 4, thread_root_id: null, reply_count: 0, author: { kind: "person", login: "octo", name: "octo", deleted: false }, run_id: null, body: "@reviewer xem giúp", mentions: [], reactions: [], label: [], created_at: "2026-10-14T09:00:00Z" };
    api.answer = () => Response.json(sent, { status: 201 });
    const { user, box, onSent } = setup();
    await user.click(screen.getByTestId("chat-composer-send"));
    expect(screen.getByTestId("chat-composer-problem")).toHaveTextContent("Hãy viết gì đó trước.");
    await user.type(box, "@reviewer xem giúp");
    await user.keyboard("{Escape}{Control>}{Enter}{/Control}");
    await waitFor(() => expect(onSent).toHaveBeenCalledWith(sent));
    const post = api.seen.find((call) => call.method === "POST");
    expect(post?.url).toBe("http://hub.test/v1/chats/7/messages");
    expect(post?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(JSON.parse(post?.body ?? "{}")).toEqual({ body: "@reviewer xem giúp" });
    expect(box).toHaveValue("");

    api.answer = () => Response.json({ error: "conflict", message: "@reviewer is not in this chat", request_id: "req-1" }, { status: 409 });
    await user.type(box, "lần nữa");
    await user.click(screen.getByTestId("chat-composer-send"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Hub báo: @reviewer is not in this chat");
    expect(box).toHaveValue("lần nữa");
  });
});

describe("ReactionChips", () => {
  it("names each reaction by its emoji, count and who put it, the visitor's pressed", async () => {
    const onToggle = vi.fn();
    renderVi(
      <ReactionChips
        reactions={[
          { emoji: "\u{1F440}", count: 1, mine: false, people: [], agents: ["reviewer"] },
          { emoji: "\u{1F389}", count: 5, mine: true, people: ["octo", "mai", "lan", "an", "binh"], agents: [] },
        ]}
        me="octo"
        busy={false}
        onToggle={onToggle}
      />,
    );
    const [eyes, party] = screen.getAllByTestId("reaction-chip");
    expect(eyes).toHaveAccessibleName("Đôi mắt: 1, của @reviewer");
    expect(eyes).toHaveAttribute("aria-pressed", "false");
    expect(party).toHaveAccessibleName("Pháo giấy: 5, của bạn, mai và lan và 2 người khác");
    expect(party).toHaveAttribute("aria-pressed", "true");
    await userEvent.setup().click(party);
    expect(onToggle).toHaveBeenCalledWith("\u{1F389}", false);
  });
});
