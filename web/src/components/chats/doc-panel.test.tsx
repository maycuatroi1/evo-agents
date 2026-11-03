// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OpenDocContext } from "@/components/data/doc-buttons";
import { DocPanel } from "@/components/data/doc-panel";
import { createApiClient } from "@/lib/api/client";
import type { LinkedAttachment } from "@/lib/attachments";
import type { ChatAuthor, GroupMessage } from "@/lib/chats";
import { type DocVersion, type HubDocVersion, MAX_DOC_BYTES } from "@/lib/doc-versions";
import type { RunOutput } from "@/lib/outputs";
import { renderVi } from "@/test/render";

import messages from "../../../messages/vi.json";
import { docVersions } from "./doc-model";
import { ChatDocPanel } from "./doc-panel";
import { EMPTY_FEED, type Feed, withMessage } from "./feed-model";
import { MessageItem } from "./message-item";

const doc = messages.chats.doc;
const md = messages.memories.markdown;
const OCTO: ChatAuthor = { kind: "person", login: "octo", name: "octo", deleted: false, avatar_url: null };
const AGENT: ChatAuthor = { kind: "agent", login: null, name: "Reviewer", deleted: false, avatar_url: null, handle: "reviewer", project: "demo" } as ChatAuthor;

function attachment(id: number, name: string, size = 20, mediaType = "text/markdown"): LinkedAttachment {
  return { id, name, media_type: mediaType, size, sha256: "b".repeat(64), url: `/attachments/${id}` };
}

function output(id: number, name: string, mediaType = "text/markdown"): RunOutput {
  return {
    id,
    run_id: 3,
    source: "sent",
    name,
    media_type: mediaType,
    viewer: "text",
    size: 30,
    sha256: "a".repeat(64),
    caption: null,
    tool_call_id: null,
    created_at: "2026-10-14T08:02:00Z",
    url: `/outputs/${id}`,
  };
}

function message(id: number, files: { attachments?: LinkedAttachment[]; outputs?: RunOutput[] }, author: ChatAuthor = OCTO): GroupMessage {
  return {
    id,
    chat_id: 7,
    seq: id,
    thread_root_id: null,
    reply_count: 0,
    author,
    run_id: null,
    body: "",
    mentions: [],
    reactions: [],
    label: [],
    created_at: "2026-10-14T08:01:00Z",
    ...files,
  } as GroupMessage;
}

/** A version as the hub lists it. */
function listed(source: "attached" | "sent", id: number, seq: number, author: ChatAuthor | null, name = "plan.md"): HubDocVersion {
  return {
    source,
    id,
    name,
    media_type: name.endsWith(".md") ? "text/markdown" : "application/yaml",
    viewer: "text",
    size: 20,
    sha256: "c".repeat(64),
    caption: null,
    created_at: "2026-10-14T08:01:00Z",
    url: source === "attached" ? `/attachments/${id}` : `/outputs/${id}`,
    message_id: seq,
    seq,
    thread_root_id: null,
    run_id: source === "attached" ? null : 3,
    author,
  };
}

const TEXTS: Record<string, string> = {
  "/attachments/1": "# Plan\n\nNgười dùng tải tệp lên.\n\nkeep\n\nold line\n",
  "/outputs/1": "# Plan\n\nHọc viên tải tệp lên.\n\nkeep\n\nnew line\n\nadded\n",
  "/outputs/2": "# Plan\n\nHọc viên tải tệp lên.\n\nkeep\n\nnew line\n\nadded\n\nthird <script>alert(1)</script>\n",
};

afterEach(() => vi.unstubAllGlobals());

/** The hub's API answering the chat's versions of `plan.md` with `versions`. */
function hubWith(versions: HubDocVersion[]) {
  const calls: string[] = [];
  const api = () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request: Request) => {
        calls.push(request.url);
        return new Response(JSON.stringify({ key: "plan.md", versions }), { headers: { "content-type": "application/json" } });
      },
    });
  return { api, calls };
}

function stubTexts() {
  const fetch = vi.fn(async (url: string) => new Response(TEXTS[url]));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function chatPanel(feed: Feed, client: QueryClient, api: ReturnType<typeof hubWith>["api"], name = "plan.md") {
  return (
    <QueryClientProvider client={client}>
      <ChatDocPanel chatId={7} feed={feed} name={name} me="mai" onClose={vi.fn()} api={api} />
    </QueryClientProvider>
  );
}

const marks = (kind: "add" | "del") =>
  Array.from(screen.getByTestId("doc-tracked").querySelectorAll(`[data-tracked="${kind}"]`), (element) =>
    (element.textContent ?? "").replace(kind === "add" ? md.trackedAdded : md.trackedRemoved, "").trim(),
  );

describe("docVersions", () => {
  it("takes the text files of the same name, any case and any folder, oldest first", () => {
    let feed = withMessage(EMPTY_FEED, message(2, { outputs: [output(4, "docs/PLAN.md")] }));
    feed = withMessage(feed, message(1, { attachments: [attachment(1, "plan.md"), attachment(9, "other.md")] }));
    expect(docVersions(feed, "plan.md").map((version) => version.key)).toEqual(["attachment-1", "output-4"]);
  });

  it("keeps an attachment and an output of the same id apart, and counts an output carried twice once", () => {
    let feed = withMessage(EMPTY_FEED, message(1, { attachments: [attachment(1, "plan.md")] }));
    feed = withMessage(feed, message(2, { outputs: [output(1, "plan.md")] }));
    feed = withMessage(feed, message(3, { outputs: [output(1, "plan.md")] }));
    expect(docVersions(feed, "plan.md").map((version) => version.key)).toEqual(["attachment-1", "output-1"]);
  });
});

describe("ChatDocPanel", () => {
  it("opens a document with two versions on Marked, the latest against the one before, with the original in the list", async () => {
    stubTexts();
    // The page holds only the agent's fix; the hub lists the original, which an older page of the chat carries.
    const feed = withMessage(EMPTY_FEED, message(40, { outputs: [output(1, "plan.md")] }, AGENT));
    const hub = hubWith([listed("attached", 1, 2, OCTO), listed("sent", 1, 40, AGENT)]);
    renderVi(chatPanel(feed, new QueryClient(), hub.api));

    await screen.findByTestId("doc-tracked");
    expect(hub.calls[0]).toBe("http://hub.test/v1/chats/7/doc-versions?name=plan.md");
    expect(screen.getByTestId("doc-panel")).toHaveAttribute("data-mode", "tracked");
    expect(screen.getByTestId("doc-mode-tracked")).toHaveTextContent("Đánh dấu");
    expect(screen.getByTestId("doc-mode-lines")).toHaveTextContent("Theo dòng");
    expect(marks("add")).toEqual(expect.arrayContaining(["Học viên", "new", "added"]));
    expect(marks("del")).toEqual(expect.arrayContaining(["Người dùng", "old"]));
    expect(screen.getByTestId("doc-tracked").querySelector("h3")).toHaveTextContent("Plan");
    expect(screen.getByTestId("doc-diff-legend")).toHaveAttribute("data-view", "tracked");
    const base = screen.getByTestId("doc-base");
    expect(within(base).getAllByRole("option")).toHaveLength(1);
    expect(within(base).getByRole("option")).toHaveTextContent("Bản 1, octo");
    expect(within(screen.getByTestId("doc-version")).getAllByRole("option").map((option) => option.textContent)).toEqual([
      expect.stringContaining("Bản 1, octo"),
      expect.stringContaining("Bản 2, Reviewer"),
    ]);

    fireEvent.click(screen.getByTestId("doc-mode-lines"));
    const diff = await screen.findByTestId("doc-diff");
    const removed = diff.querySelector("[data-kind=del] > span:last-child")!;
    expect(removed.className).toContain("line-through");
    expect(diff.querySelectorAll("[data-word]").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByTestId("doc-mode-read"));
    expect(await screen.findByTestId("doc-markdown")).toHaveTextContent("added");
  });

  it("moves an open panel to the version an agent sends and marks its changes against the version shown", async () => {
    stubTexts();
    const client = new QueryClient();
    const hub = hubWith([listed("attached", 1, 1, OCTO)]);
    const first = withMessage(EMPTY_FEED, message(1, { attachments: [attachment(1, "plan.md")] }));
    function Harness() {
      const [feed, setFeed] = useState(first);
      return (
        <>
          <button type="button" onClick={() => setFeed((now) => withMessage(now, message(2, { outputs: [output(1, "plan.md")] }, AGENT)))}>
            fix
          </button>
          <button type="button" onClick={() => setFeed((now) => withMessage(now, message(3, { outputs: [output(2, "plan.md")] }, AGENT)))}>
            again
          </button>
          {chatPanel(feed, client, hub.api)}
        </>
      );
    }
    renderVi(<Harness />);
    expect(await screen.findByTestId("doc-markdown")).toHaveTextContent("old line");
    expect(screen.getByTestId("doc-panel")).toHaveFocus();
    expect(screen.queryByTestId("doc-mode-tracked")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "fix" }));
    await screen.findByTestId("doc-tracked");
    expect(marks("del")).toContain("Người dùng");
    expect(screen.getByTestId("doc-reloaded")).toHaveTextContent("Reviewer vừa gửi bản mới");
    expect(screen.getByTestId("doc-stats")).toHaveTextContent(/\+\d+ −\d+/);

    // Reading the first version when the third comes: the third is marked against the first.
    fireEvent.change(screen.getByTestId("doc-version"), { target: { value: "attachment-1" } });
    await screen.findByTestId("doc-markdown");
    fireEvent.click(screen.getByRole("button", { name: "again" }));
    await screen.findByTestId("doc-tracked");
    expect((screen.getByTestId("doc-base") as HTMLSelectElement).value).toBe("attachment-1");
    expect(marks("del")).toContain("Người dùng");
    // The document's own HTML stays text in the marked view.
    expect(screen.getByTestId("doc-tracked").querySelector("script")).toBeNull();
    expect(screen.getByTestId("doc-tracked")).toHaveTextContent("<script>alert(1)</script>");
  });

  it("reads a YAML document in mono and shows its changes by line, with no Marked view", async () => {
    const texts: Record<string, string> = { "/attachments/5": "name: demo\nreplicas: 2\n", "/outputs/6": "name: demo\nreplicas: 3\n" };
    vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(texts[url])));
    const yaml = (source: "attached" | "sent", id: number, seq: number) => ({ ...listed(source, id, seq, OCTO, "deploy.yaml"), url: source === "attached" ? `/attachments/${id}` : `/outputs/${id}` });
    const hub = hubWith([yaml("attached", 5, 1), yaml("sent", 6, 2)]);
    renderVi(chatPanel(EMPTY_FEED, new QueryClient(), hub.api, "deploy.yaml"));
    const diff = await screen.findByTestId("doc-diff");
    expect(screen.getByTestId("doc-panel")).toHaveAttribute("data-mode", "lines");
    expect(screen.queryByTestId("doc-mode-tracked")).toBeNull();
    expect(diff.querySelector("[data-kind=del] [data-word=del]")).toHaveTextContent("2");
    expect(diff.querySelector("[data-kind=add] [data-word=add]")).toHaveTextContent("3");
    fireEvent.click(screen.getByTestId("doc-mode-read"));
    expect(await screen.findByTestId("doc-text")).toHaveTextContent("replicas: 3");
  });

  it("waits for the hub's list before saying a document is gone", async () => {
    stubTexts();
    let answer: ((response: Response) => void) | null = null;
    const api = () => createApiClient({ baseUrl: "http://hub.test", fetch: () => new Promise<Response>((resolve) => (answer = resolve)) });
    renderVi(chatPanel(EMPTY_FEED, new QueryClient(), api));
    await waitFor(() => expect(answer).not.toBeNull());
    expect(screen.getByTestId("doc-body")).toHaveTextContent(doc.loading);
    answer!(new Response(JSON.stringify({ key: "plan.md", versions: [] }), { headers: { "content-type": "application/json" } }));
    expect(await screen.findByText(doc.gone)).toBeTruthy();
  });
});

describe("DocPanel", () => {
  it("does not read a version too big for the panel", async () => {
    const fetch = vi.fn(async () => new Response("never"));
    vi.stubGlobal("fetch", fetch);
    const feed = withMessage(EMPTY_FEED, message(1, { attachments: [attachment(1, "plan.md", MAX_DOC_BYTES + 1)] }));
    const versions: DocVersion[] = docVersions(feed, "plan.md");
    renderVi(
      <QueryClientProvider client={new QueryClient()}>
        <DocPanel name="plan.md" versions={versions} authorOf={() => "octo"} onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId("doc-too-big")).toHaveTextContent(doc.tooBig);
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("MessageItem documents", () => {
  it("offers to open each text document of a message once, not an image, where the page has the panel", () => {
    const open = vi.fn();
    const shown = message(5, {
      attachments: [attachment(1, "plan.md"), attachment(2, "shot.png", 20, "image/png"), attachment(3, "deploy.yaml", 20, "application/yaml"), attachment(4, "notes.txt", 20, "text/plain"), attachment(5, "a.zip", 20, "application/zip")],
      outputs: [output(3, "notes/PLAN.md")],
    });
    renderVi(
      <OpenDocContext.Provider value={open}>
        <ul>
          <MessageItem message={shown} chat={{ agents: [], people: [] }} me="mai" onReact={vi.fn()} reacting={null} />
        </ul>
      </OpenDocContext.Provider>,
    );
    const buttons = within(screen.getByTestId("message-docs")).getAllByTestId("doc-open");
    expect(buttons.map((button) => button.textContent)).toEqual(["Mở plan.md cạnh khung chat", "Mở deploy.yaml cạnh khung chat", "Mở notes.txt cạnh khung chat"]);
    fireEvent.click(buttons[1]);
    expect(open).toHaveBeenCalledWith("deploy.yaml");
  });

  it("offers nothing where the page has no panel", () => {
    renderVi(
      <ul>
        <MessageItem message={message(5, { attachments: [attachment(1, "plan.md")] })} chat={{ agents: [], people: [] }} me="mai" onReact={vi.fn()} reacting={null} />
      </ul>,
    );
    expect(screen.queryByTestId("message-docs")).toBeNull();
  });
});
