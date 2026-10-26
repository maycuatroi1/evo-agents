// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LinkedAttachment } from "@/lib/attachments";
import type { ChatAuthor, GroupMessage } from "@/lib/chats";
import type { RunOutput } from "@/lib/outputs";
import { renderVi } from "@/test/render";

import messages from "../../../messages/vi.json";
import { docVersions, MAX_DOC_BYTES, OpenDocContext } from "./doc-model";
import { DocPanel } from "./doc-panel";
import { EMPTY_FEED, type Feed, withMessage } from "./feed-model";
import { MessageItem } from "./message-item";

const doc = messages.chats.doc;
const OCTO: ChatAuthor = { kind: "person", login: "octo", name: "octo", deleted: false, avatar_url: null };

function attachment(id: number, name: string, size = 20): LinkedAttachment {
  return { id, name, media_type: "text/markdown", size, sha256: "b".repeat(64), url: `/attachments/${id}` };
}

function output(id: number, name: string): RunOutput {
  return {
    id,
    run_id: 3,
    source: "sent",
    name,
    media_type: "text/markdown",
    viewer: "text",
    size: 30,
    sha256: "a".repeat(64),
    caption: null,
    tool_call_id: null,
    created_at: "2026-10-14T08:02:00Z",
    url: `/outputs/${id}`,
  };
}

function message(id: number, files: { attachments?: LinkedAttachment[]; outputs?: RunOutput[] }): GroupMessage {
  return {
    id,
    chat_id: 7,
    seq: id,
    thread_root_id: null,
    reply_count: 0,
    author: OCTO,
    run_id: null,
    body: "",
    mentions: [],
    reactions: [],
    label: [],
    created_at: "2026-10-14T08:01:00Z",
    ...files,
  } as GroupMessage;
}

const TEXTS: Record<string, string> = {
  "/attachments/1": "# Plan\nkeep\nold line\n",
  "/outputs/1": "# Plan\nkeep\nnew line\nadded\n",
};

afterEach(() => vi.unstubAllGlobals());

function panel(feed: Feed, client: QueryClient, onClose = vi.fn()) {
  return (
    <QueryClientProvider client={client}>
      <DocPanel feed={feed} name="plan.md" me="mai" onClose={onClose} />
    </QueryClientProvider>
  );
}

describe("docVersions", () => {
  it("takes the Markdown files of the same name, any case and any folder, oldest first", () => {
    let feed = withMessage(EMPTY_FEED, message(2, { outputs: [output(4, "docs/PLAN.md")] }));
    feed = withMessage(feed, message(1, { attachments: [attachment(1, "plan.md"), attachment(9, "other.md")] }));
    expect(docVersions(feed, "plan.md").map((version) => version.key)).toEqual(["attachment-1", "output-4"]);
  });

  it("keeps an attachment and an output of the same id apart", () => {
    let feed = withMessage(EMPTY_FEED, message(1, { attachments: [attachment(1, "plan.md")] }));
    feed = withMessage(feed, message(2, { outputs: [output(1, "plan.md")] }));
    expect(new Set(docVersions(feed, "plan.md").map((version) => version.key)).size).toBe(2);
  });
});

describe("DocPanel", () => {
  it("reloads when an agent sends the document back and shows the lines added and removed", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(TEXTS[url])));
    const client = new QueryClient();
    const first = withMessage(EMPTY_FEED, message(1, { attachments: [attachment(1, "plan.md")] }));
    function Harness() {
      const [feed, setFeed] = useState(first);
      return (
        <>
          <button type="button" onClick={() => setFeed(withMessage(first, message(2, { outputs: [output(1, "plan.md")] })))}>
            fix
          </button>
          {panel(feed, client)}
        </>
      );
    }
    renderVi(<Harness />);
    expect(await screen.findByTestId("doc-markdown")).toHaveTextContent("old line");
    expect(screen.getByTestId("doc-panel")).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "fix" }));
    const diff = await screen.findByTestId("doc-diff");
    const rows = [...diff.querySelectorAll("[data-kind]")].map((row) => `${row.getAttribute("data-kind")}:${row.textContent}`);
    expect(rows.some((row) => row.startsWith("del:") && row.includes("old line"))).toBe(true);
    expect(rows.some((row) => row.startsWith("add:") && row.includes("added"))).toBe(true);
    expect(rows.some((row) => row.startsWith("same:") && row.includes("keep"))).toBe(true);
    expect(screen.getByTestId("doc-stats")).toHaveTextContent("+2 −1");
    expect(screen.getByTestId("doc-reloaded")).toHaveTextContent("tải lại");
    const version = screen.getByTestId("doc-version");
    expect(within(version).getAllByRole("option")).toHaveLength(2);
    expect(version.textContent).not.toContain("\u00b7");
    fireEvent.click(screen.getByTestId("doc-mode-read"));
    expect(await screen.findByTestId("doc-markdown")).toHaveTextContent("added");
  });

  it("does not read a version too big for the panel", async () => {
    const fetch = vi.fn(async () => new Response("never"));
    vi.stubGlobal("fetch", fetch);
    const feed = withMessage(EMPTY_FEED, message(1, { attachments: [attachment(1, "plan.md", MAX_DOC_BYTES + 1)] }));
    renderVi(panel(feed, new QueryClient()));
    expect(await screen.findByTestId("doc-too-big")).toHaveTextContent(doc.tooBig);
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("MessageItem documents", () => {
  it("offers to open each Markdown document of a message once, where the page has the panel", () => {
    const open = vi.fn();
    const shown = message(5, { attachments: [attachment(1, "plan.md"), attachment(2, "shot.png")], outputs: [output(3, "notes/PLAN.md")] });
    shown.attachments![1].media_type = "image/png";
    renderVi(
      <OpenDocContext.Provider value={open}>
        <ul>
          <MessageItem message={shown} chat={{ agents: [], people: [] }} me="mai" onReact={vi.fn()} reacting={null} />
        </ul>
      </OpenDocContext.Provider>,
    );
    const buttons = within(screen.getByTestId("message-docs")).getAllByTestId("doc-open");
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    expect(open).toHaveBeenCalledWith("plan.md");
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
