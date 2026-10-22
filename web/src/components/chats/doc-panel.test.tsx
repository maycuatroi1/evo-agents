// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChatAuthor, ChatFile, GroupMessage } from "@/lib/chats";
import { renderVi } from "@/test/render";

import { docVersions } from "./doc-model";
import { DocPanel } from "./doc-panel";
import { EMPTY_FEED, type Feed, withMessage } from "./feed-model";

const OCTO: ChatAuthor = { kind: "person", login: "octo", name: "octo", deleted: false, avatar_url: null };

function chatFile(id: number, name: string): ChatFile {
  return { id, name, media_type: "text/markdown", viewer: "text", size: 20, sha256: String(id).repeat(64).slice(0, 64), uploaded_by: "octo", created_at: "2026-10-14T08:01:00Z", url: `/files/${id}` };
}

function message(id: number, files: ChatFile[]): GroupMessage {
  return { id, chat_id: 7, seq: id, thread_root_id: null, reply_count: 0, author: OCTO, run_id: null, body: "", mentions: [], reactions: [], label: [], created_at: "2026-10-14T08:01:00Z", files };
}

const TEXTS: Record<string, string> = { "/files/1": "# Plan\nkeep\nold line\n", "/files/2": "# Plan\nkeep\nnew line\nadded\n" };

afterEach(() => vi.unstubAllGlobals());

function panel(feed: Feed, client: QueryClient) {
  return (
    <QueryClientProvider client={client}>
      <DocPanel feed={feed} name="plan.md" me="mai" onClose={vi.fn()} />
    </QueryClientProvider>
  );
}

describe("docVersions", () => {
  it("takes the Markdown files of the same name, any case, oldest first", () => {
    let feed = withMessage(EMPTY_FEED, message(2, [chatFile(2, "PLAN.md")]));
    feed = withMessage(feed, message(1, [chatFile(1, "plan.md"), chatFile(9, "other.md")]));
    expect(docVersions(feed, "plan.md").map((version) => version.file.id)).toEqual([1, 2]);
  });
});

describe("DocPanel", () => {
  it("reloads when a new version comes and shows the lines added and removed", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(TEXTS[url])));
    const client = new QueryClient();
    const first = withMessage(EMPTY_FEED, message(1, [chatFile(1, "plan.md")]));
    function Harness() {
      const [feed, setFeed] = useState(first);
      return (
        <>
          <button type="button" onClick={() => setFeed(withMessage(first, message(2, [chatFile(2, "plan.md")])))}>
            fix
          </button>
          {panel(feed, client)}
        </>
      );
    }
    renderVi(<Harness />);
    expect(await screen.findByTestId("doc-markdown")).toHaveTextContent("old line");
    fireEvent.click(screen.getByRole("button", { name: "fix" }));
    const diff = await screen.findByTestId("doc-diff");
    const rows = [...diff.querySelectorAll("[data-kind]")].map((row) => `${row.getAttribute("data-kind")}:${row.textContent}`);
    expect(rows.some((row) => row.startsWith("del:") && row.includes("old line"))).toBe(true);
    expect(rows.some((row) => row.startsWith("add:") && row.includes("added"))).toBe(true);
    expect(rows.some((row) => row.startsWith("same:") && row.includes("keep"))).toBe(true);
    expect(screen.getByTestId("doc-stats")).toHaveTextContent("+2 −1");
    expect(screen.getByTestId("doc-reloaded")).toHaveTextContent("tải lại");
    fireEvent.click(screen.getByTestId("doc-mode-read"));
    expect(await screen.findByTestId("doc-markdown")).toHaveTextContent("added");
  });
});
