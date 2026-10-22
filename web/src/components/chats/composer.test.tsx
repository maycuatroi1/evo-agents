// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import type { Chat, ChatAgent, ChatFile, GroupMessage } from "@/lib/chats";
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
    { login: "octo", added_by: "octo", added_at: "2026-10-14T08:00:00Z", avatar_url: null },
    { login: "mai", added_by: "octo", added_at: "2026-10-14T08:00:00Z", avatar_url: null },
  ],
  agents: [agent("reviewer", { name: "Code reviewer" }), agent("fixer", { name: "Fixer" }), agent("demo", { name: "Demo", is_default: true })],
  label: [],
  restricted: false,
  last_seq: 3,
  last_read_seq: 3,
  kind: "group",
  image_url: null,
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

afterEach(() => {
  delete (window as Partial<Window>).matchMedia;
});

/** A browser whose main pointer is a touch screen, as a phone's is: `(hover: none) and (pointer: coarse)` matches. */
function onTouchScreen() {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: (query: string) => ({ matches: query.includes("pointer: coarse"), media: query, addEventListener: () => {}, removeEventListener: () => {} }),
  });
}

function message(body: string): GroupMessage {
  return { id: 11, chat_id: 7, seq: 6, thread_root_id: null, reply_count: 0, author: { kind: "person", login: "octo", name: "octo", deleted: false }, run_id: null, body, mentions: [], reactions: [], label: [], created_at: "2026-10-14T09:02:00Z" };
}

const posts = () => api.seen.filter((call) => call.method === "POST").map((call) => JSON.parse(call.body));

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
    expect(screen.getByTestId("chat-composer-problem")).toHaveTextContent("Hãy viết gì đó hoặc đính kèm file trước.");
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

describe("ChatComposer keys", () => {
  it("sends with Enter, adds a line with Shift Enter, and sends nothing on the Enter that ends an input method's word", async () => {
    api.answer = () => Response.json(message("dòng một\ndòng hai"), { status: 201 });
    const { user, box, onSent } = setup();
    expect(box).toHaveAttribute("aria-keyshortcuts", "Enter Meta+Enter Control+Enter");
    expect(box).toHaveAccessibleDescription("Gợi ý Enter để gửi, Shift Enter để xuống dòng.");
    await user.type(box, "dòng một{Shift>}{Enter}{/Shift}dòng hai");
    expect(box).toHaveValue("dòng một\ndòng hai");
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    fireEvent.keyDown(box, { key: "Enter", keyCode: 229 });
    expect(posts()).toEqual([]);
    await user.keyboard("{Enter}");
    await waitFor(() => expect(onSent).toHaveBeenCalledTimes(1));
    expect(posts()).toEqual([{ body: "dòng một\ndòng hai" }]);
    expect(box).toHaveValue("");
  });

  it("refuses a blank message on Enter instead of sending it", async () => {
    const { user, box } = setup();
    await user.type(box, "   {Enter}");
    expect(screen.getByTestId("chat-composer-problem")).toHaveTextContent("Hãy viết gì đó hoặc đính kèm file trước.");
    expect(posts()).toEqual([]);
  });

  it("adds a line with Enter on a touch screen, where Send sends", async () => {
    onTouchScreen();
    api.answer = () => Response.json(message("dòng một\ndòng hai"), { status: 201 });
    const { user, box, onSent } = setup();
    expect(box).toHaveAttribute("aria-keyshortcuts", "Meta+Enter Control+Enter");
    expect(box).toHaveAccessibleDescription("Gợi ý");
    await user.type(box, "dòng một{Enter}dòng hai");
    expect(box).toHaveValue("dòng một\ndòng hai");
    expect(posts()).toEqual([]);
    await user.click(screen.getByTestId("chat-composer-send"));
    await waitFor(() => expect(onSent).toHaveBeenCalledTimes(1));
    expect(posts()).toEqual([{ body: "dòng một\ndòng hai" }]);
  });
});

describe("ChatComposer answering one message", () => {
  it("says which message it answers, sends the reply to it, and Escape or Cancel the reply leaves it", async () => {
    const sent: GroupMessage = { id: 10, chat_id: 7, seq: 5, thread_root_id: 8, reply_count: 0, author: { kind: "person", login: "octo", name: "octo", deleted: false }, run_id: null, body: "còn retries?", mentions: [], reactions: [], label: [], created_at: "2026-10-14T09:01:00Z" };
    api.answer = () => Response.json(sent, { status: 201 });
    const onCancel = vi.fn();
    const onSent = vi.fn();
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    renderVi(
      <QueryClientProvider client={client}>
        <ChatComposer
          chat={{ ...CHAT, kind: "direct" }}
          me="octo"
          replyTo={9}
          onSent={onSent}
          label="Tin"
          placeholder="Nhắn cho Code reviewer"
          hint="Đi tiếp cùng session"
          testId="chat-composer"
          replying={{ name: "Code reviewer", text: "Đã sửa wait test.", onCancel }}
        />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    const box = screen.getByTestId("chat-composer-text");
    expect(screen.getByTestId("chat-composer-replying")).toHaveTextContent("Đang trả lời Code reviewer Đã sửa wait test.");
    expect(box).toHaveAccessibleDescription("Đang trả lời Code reviewer Đã sửa wait test. Đi tiếp cùng session Enter để gửi, Shift Enter để xuống dòng.");
    await user.type(box, "còn retries?");
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(onSent).toHaveBeenCalledWith(sent));
    expect(JSON.parse(api.seen.find((call) => call.method === "POST")?.body ?? "{}")).toEqual({ body: "còn retries?", reply_to: 9 });

    await user.click(box);
    await user.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Huỷ trả lời" }));
    expect(onCancel).toHaveBeenCalledTimes(2);
    expect(box).toHaveFocus();
  });
});

function chatFile(id: number, name: string, mediaType = "image/png", size = 4): ChatFile {
  return { id, name, media_type: mediaType, viewer: mediaType.startsWith("image/") ? "image" : "none", size, sha256: "a".repeat(64), uploaded_by: "octo", created_at: "2026-10-14T09:03:00Z", url: `/v1/chats/7/files/${id}/content` };
}

/** A file whose size says `size` without holding that many bytes. */
function sized(name: string, size: number, type = "application/pdf"): File {
  const file = new File(["%PDF"], name, { type });
  Object.defineProperty(file, "size", { value: size });
  return file;
}

const uploads = () => api.seen.filter((call) => call.method === "POST" && call.url.includes("/files"));
const messagePosts = () => api.seen.filter((call) => call.method === "POST" && call.url.endsWith("/messages")).map((call) => JSON.parse(call.body));

describe("ChatComposer files", () => {
  it("uploads each file picked at once, shows it as a chip, and sends it with no text", async () => {
    let next = 30;
    const finish: (() => void)[] = [];
    api.answer = async (request) => {
      if (request.url.includes("/files?")) {
        const name = new URL(request.url).searchParams.get("name") ?? "";
        const id = next++;
        await new Promise<void>((resolve) => finish.push(resolve));
        return Response.json(chatFile(id, name, request.headers.get("content-type") ?? ""), { status: 201 });
      }
      return Response.json({ ...message(""), files: [chatFile(30, "shot.png")] }, { status: 201 });
    };
    const { user, box, onSent } = setup();
    await user.upload(screen.getByTestId("chat-composer-file-input"), [new File(["png!"], "shot.png", { type: "image/png" }), new File(["x"], "notes")]);
    await waitFor(() => expect(uploads()).toHaveLength(2));
    const [image, plain] = uploads();
    expect(image.url).toBe("http://hub.test/v1/chats/7/files?name=shot.png");
    expect(image.headers.get("content-type")).toBe("image/png");
    expect(image.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(image.body).toBe("png!");
    expect(plain.headers.get("content-type")).toBe("application/octet-stream");

    const chips = screen.getAllByTestId("chat-composer-file");
    expect(chips.map((chip) => chip.getAttribute("data-state"))).toEqual(["uploading", "uploading"]);
    expect(within(screen.getByRole("list", { name: "File sẽ gửi" })).getAllByRole("listitem")).toHaveLength(2);
    expect(chips[0]).toHaveTextContent("shot.png");
    expect(screen.getByTestId("chat-composer-send")).toBeDisabled();
    await user.click(box);
    await user.keyboard("{Enter}");
    expect(messagePosts()).toEqual([]);

    finish.forEach((resolve) => resolve());
    await waitFor(() => expect(screen.getAllByTestId("chat-composer-file").map((chip) => chip.getAttribute("data-state"))).toEqual(["uploaded", "uploaded"]));
    await user.click(screen.getByTestId("chat-composer-send"));
    await waitFor(() => expect(onSent).toHaveBeenCalledTimes(1));
    expect(messagePosts()).toEqual([{ body: "", file_ids: [30, 31] }]);
    expect(screen.queryByTestId("chat-composer-file")).toBeNull();
  });

  it("refuses a file over 25 MiB, an empty one and the files past ten, with the reason", async () => {
    let next = 1;
    api.answer = (request) => Response.json(chatFile(next++, new URL(request.url).searchParams.get("name") ?? ""), { status: 201 });
    const { user } = setup();
    const input = screen.getByTestId("chat-composer-file-input");
    await user.upload(input, [sized("video.mp4", 25 * 1024 * 1024 + 1), sized("empty.txt", 0), sized("ok.pdf", 25 * 1024 * 1024)]);
    expect(screen.getByTestId("chat-composer-files-problem")).toHaveTextContent("video.mp4: mỗi file tối đa 25 MB. empty.txt: file rỗng.");
    await waitFor(() => expect(uploads()).toHaveLength(1));
    await user.upload(input, Array.from({ length: 11 }, (_, index) => new File(["x"], `f${index}.txt`, { type: "text/plain" })));
    expect(screen.getByTestId("chat-composer-files-problem")).toHaveTextContent("Một tin mang tối đa 10 file: 2 file không được đính kèm.");
    await waitFor(() => expect(uploads()).toHaveLength(10));
    expect(screen.getAllByTestId("chat-composer-file")).toHaveLength(10);
  });

  it("shows an upload the hub refused on its chip, holds Send until it is removed", async () => {
    api.answer = (request) =>
      request.url.includes("/files?")
        ? Response.json({ error: "too_large", message: "the file is too big", request_id: "req-2" }, { status: 413 })
        : Response.json(message("xem file"), { status: 201 });
    const { user, box, onSent } = setup();
    await user.upload(screen.getByTestId("chat-composer-file-input"), new File(["x"], "big.zip", { type: "application/zip" }));
    await waitFor(() => expect(screen.getByTestId("chat-composer-file")).toHaveAttribute("data-state", "failed"));
    expect(screen.getByTestId("chat-composer-file-error")).toHaveTextContent("Quá 25 MB");
    await user.type(box, "xem file");
    await user.click(screen.getByTestId("chat-composer-send"));
    expect(screen.getByTestId("chat-composer-problem")).toHaveTextContent("Có file chưa tải lên được");
    expect(messagePosts()).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Bỏ big.zip" }));
    expect(screen.queryByTestId("chat-composer-file")).toBeNull();
    await user.click(screen.getByTestId("chat-composer-send"));
    await waitFor(() => expect(onSent).toHaveBeenCalledTimes(1));
    expect(messagePosts()).toEqual([{ body: "xem file" }]);
  });

  it("attaches the files of a paste and of a drop on the box", async () => {
    let next = 50;
    api.answer = (request) => Response.json(chatFile(next++, new URL(request.url).searchParams.get("name") ?? ""), { status: 201 });
    const { box } = setup();
    const pasted = new File(["png"], "paste.png", { type: "image/png" });
    fireEvent.paste(box, { clipboardData: { files: [pasted], types: ["Files"] } });
    await waitFor(() => expect(screen.getAllByTestId("chat-composer-file")).toHaveLength(1));
    const dropped = new File(["pdf"], "drop.pdf", { type: "application/pdf" });
    const zone = box.parentElement as HTMLElement;
    fireEvent.dragOver(zone, { dataTransfer: { types: ["Files"], files: [], dropEffect: "none" } });
    expect(zone).toHaveAttribute("data-dragging", "true");
    fireEvent.drop(zone, { dataTransfer: { types: ["Files"], files: [dropped] } });
    expect(zone).not.toHaveAttribute("data-dragging");
    await waitFor(() => expect(screen.getAllByTestId("chat-composer-file").map((chip) => chip.getAttribute("data-state"))).toEqual(["uploaded", "uploaded"]));
    expect(uploads().map((call) => new URL(call.url).searchParams.get("name"))).toEqual(["paste.png", "drop.pdf"]);
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
