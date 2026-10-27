// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, describe, expect, it, vi } from "vitest";

import type { Chat, ChatAuthor, ChatPage, GroupMessage } from "@/lib/chats";
import { renderVi } from "@/test/render";

import { feedOf } from "./feed-model";
import { MessageList } from "./message-list";

const OCTO: ChatAuthor = { kind: "person", login: "octo", name: "octo", deleted: false, avatar_url: null };
const REVIEWER: ChatAuthor = { kind: "agent", handle: "reviewer", project: "demo", name: "Code reviewer", color: "#2563eb", icon_url: null, deleted: false };

function message(id: number, minute: number, overrides: Partial<GroupMessage> = {}): GroupMessage {
  return {
    id,
    chat_id: 7,
    seq: id,
    thread_root_id: null,
    reply_count: 0,
    author: OCTO,
    run_id: null,
    body: `message ${id}`,
    mentions: [],
    reactions: [],
    label: [],
    created_at: `2026-10-14T08:${String(minute).padStart(2, "0")}:00Z`,
    ...overrides,
  };
}

function chat(kind: Chat["kind"]): Chat {
  return {
    id: 7,
    title: "Code reviewer",
    created_by: "octo",
    created_at: "2026-10-14T08:00:00Z",
    people: [{ login: "octo", added_by: "octo", added_at: "2026-10-14T08:00:00Z", avatar_url: null }],
    agents: [{ handle: "reviewer", project: "demo", name: "Code reviewer", color: "#2563eb", icon_url: null, is_default: false, deleted: false, added_by: "octo", added_at: "2026-10-14T08:00:00Z" }],
    label: [],
    restricted: false,
    last_seq: 9,
    last_read_seq: 9,
    kind,
    image_url: null,
    label_admin: false,
  };
}

function render(kind: Chat["kind"], messages: GroupMessage[]) {
  const actions = { onReact: vi.fn(), onThread: vi.fn(), onReply: vi.fn(), onQuote: vi.fn() };
  const page: ChatPage = { chat: chat(kind), messages, more: false };
  renderVi(<MessageList chat={page.chat} me="octo" feed={feedOf(page)} highlight={null} loadOlder={async () => {}} reacting={null} {...actions} />);
  return { ...actions, user: userEvent.setup() };
}

const item = (id: number) => screen.getAllByTestId("chat-message").find((element) => element.dataset.messageId === String(id)) as HTMLElement;

beforeAll(() => {
  Element.prototype.scrollTo = function scrollTo() {};
  Element.prototype.scrollIntoView = function scrollIntoView() {};
});

describe("MessageList", () => {
  it("draws a direct chat as a chat app: the visitor's on the right without a face, the agent's answers right under them", async () => {
    // Octo asks (1) and adds a line a minute later (2); the agent answers 2 (3), then 1 (4) after another message.
    const { user, onReply, onQuote, onThread } = render("direct", [
      message(1, 1, { reply_count: 1 }),
      message(2, 2, { reply_count: 1 }),
      message(3, 3, { thread_root_id: 2, author: REVIEWER }),
      message(4, 4, { thread_root_id: 1, author: REVIEWER, body: "Fixed the **wait**." }),
    ]);
    expect(screen.getAllByTestId("chat-message").map((element) => element.dataset.messageId)).toEqual(["1", "2", "3", "4"]);

    // The visitor's: on the right, no face; the second runs on (its head only for a screen reader).
    expect(item(1)).toHaveAttribute("data-mine", "true");
    expect(within(item(1)).queryByTestId("person-avatar")).toBeNull();
    expect(item(1)).toHaveAccessibleName(/^Bạn\s*,\s*15:01$/);
    expect(item(2)).toHaveAttribute("data-run-on", "true");
    expect(screen.getByText("message 2").closest("[data-testid=message-bubble]")).toHaveAttribute("title");

    // The agent's: on the left with its face and name, right under what it answers: no quote.
    expect(item(3)).toHaveAttribute("data-mine", "false");
    expect(within(item(3)).getByTestId("agent-avatar")).toBeInTheDocument();
    expect(within(item(3)).getByText("Code reviewer")).toBeInTheDocument();
    expect(within(item(3)).queryByTestId("message-quote")).toBeNull();

    // The answer to 1 came after the answer to 2: it quotes what it answers, and the quote shows it.
    const quote = within(item(4)).getByTestId("message-quote");
    expect(quote).toHaveAccessibleName("Xem tin mà tin này trả lời, của Bạn: message 1");
    expect(item(4)).toHaveAttribute("data-run-on", "false");
    await user.click(quote);
    expect(onQuote).toHaveBeenCalledWith({ root: 1, message: expect.objectContaining({ id: 1 }) });

    // Reply answers from the chat's composer; there is no thread to open in a direct chat.
    expect(screen.queryByTestId("thread-summary")).toBeNull();
    await user.click(within(item(4)).getByRole("button", { name: "Trả lời" }));
    expect(onReply).toHaveBeenCalledWith(expect.objectContaining({ id: 4 }));
    expect(onThread).not.toHaveBeenCalled();
  });

  it("keeps a group chat's threads: replies in their thread, the summary and Reply in thread under the message", async () => {
    const { user, onThread, onReply } = render("group", [message(1, 1, { reply_count: 1 }), message(2, 2, { thread_root_id: 1, author: REVIEWER }), message(3, 12)]);
    expect(screen.getAllByTestId("chat-message").map((element) => element.dataset.messageId)).toEqual(["1", "3"]);
    // Ten minutes apart: the second message of the visitor heads its own run.
    expect(item(3)).toHaveAttribute("data-run-on", "false");
    const summary = within(item(1)).getByTestId("thread-summary");
    expect(summary).toHaveTextContent("1 trả lời");
    await user.click(within(item(1)).getByRole("button", { name: "Trả lời trong thread" }));
    expect(onThread).toHaveBeenCalledWith(1);
    expect(onReply).not.toHaveBeenCalled();
  });

  it("marks the members a message names, their names without the @, but not in code, a link or a path", () => {
    render("group", [
      message(1, 1, { author: REVIEWER, body: "Chào octo, octo/demo có `@reviewer` và [octo](https://github.com/octo)." }),
      message(2, 2, { body: "@reviewer làm luôn đi, @REVIEWER nhé, @nobody thì không" }),
    ]);
    const marks = (id: number) => within(item(id)).queryAllByTestId("message-mention").map((mark) => [mark.dataset.mention, mark.textContent]);
    expect(marks(1)).toEqual([["octo", "octo"]]);
    expect(marks(2)).toEqual([
      ["reviewer", "Code reviewer"],
      ["reviewer", "Code reviewer"],
    ]);
    expect(within(item(2)).getByTestId("message-body")).toHaveTextContent("Code reviewer làm luôn đi, Code reviewer nhé, @nobody thì không");
    // The agent's head: its name, then its handle (they differ) without the @.
    expect(within(item(1)).getByTestId("message-agent-handle")).toHaveTextContent(/^reviewer$/);
  });

  it("tells a direct chat with nothing yet that every message goes to its agent", () => {
    render("direct", []);
    expect(screen.getByTestId("chat-empty")).toHaveTextContent("mọi tin đều tới Code reviewer");
  });
});
