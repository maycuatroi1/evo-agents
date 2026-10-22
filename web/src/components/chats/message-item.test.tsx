// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ChatAuthor, ChatFile, GroupMessage } from "@/lib/chats";
import { renderVi } from "@/test/render";

import { messagePreview } from "./line-model";
import { MessageItem } from "./message-item";

const OCTO: ChatAuthor = { kind: "person", login: "octo", name: "octo", deleted: false, avatar_url: null };

function chatFile(id: number, name: string, mediaType: string, viewer: ChatFile["viewer"]): ChatFile {
  return { id, name, media_type: mediaType, viewer, size: 2048, sha256: "b".repeat(64), uploaded_by: "octo", created_at: "2026-10-14T08:01:00Z", url: `/v1/chats/7/files/${id}/content` };
}

function message(overrides: Partial<GroupMessage> = {}): GroupMessage {
  return { id: 5, chat_id: 7, seq: 5, thread_root_id: null, reply_count: 0, author: OCTO, run_id: null, body: "", mentions: [], reactions: [], label: [], created_at: "2026-10-14T08:01:00Z", ...overrides };
}

function render(shown: GroupMessage) {
  renderVi(
    <ul>
      <MessageItem message={shown} chat={{ agents: [] }} me="mai" onReact={vi.fn()} reacting={null} />
    </ul>,
  );
}

const FILES = [chatFile(1, "screen.png", "image/png", "image"), chatFile(2, "report.pdf", "application/pdf", "pdf")];

describe("MessageItem files", () => {
  it("shows the files a person attached under the message, an image as its thumbnail, with no bubble when it has no text", () => {
    render(message({ files: FILES }));
    expect(screen.queryByTestId("message-bubble")).toBeNull();
    const strip = screen.getByRole("list", { name: "File đính kèm của tin nhắn này" });
    const chips = within(strip).getAllByTestId("output-chip");
    expect(chips.map((chip) => chip.dataset.outputId)).toEqual(["1", "2"]);
    expect(within(chips[0]).getByTestId("output-thumb-image")).toHaveAttribute("src", "/v1/chats/7/files/1/content");
    expect(chips[1]).toHaveAccessibleName(/^Mở report\.pdf, PDF/);
    expect(chips[1]).toHaveTextContent("report.pdf");
  });

  it("keeps the bubble of a message with text and files, and shows no strip without files", () => {
    render(message({ body: "ảnh lỗi đây", files: FILES.slice(0, 1) }));
    expect(screen.getByTestId("message-bubble")).toHaveTextContent("ảnh lỗi đây");
    expect(screen.getByTestId("message-attachments")).toBeInTheDocument();
  });

  it("shows no strip for a message without files", () => {
    render(message({ body: "chào" }));
    expect(screen.queryByTestId("message-attachments")).toBeNull();
  });

  it("quotes a message of files only by their names", () => {
    expect(messagePreview(message({ files: FILES }))).toBe("screen.png, report.pdf");
    expect(messagePreview(message({ body: "**xem** giúp", files: FILES }))).toBe("xem giúp");
  });
});
