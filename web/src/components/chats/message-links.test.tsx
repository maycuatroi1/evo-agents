// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { GroupMessage } from "@/lib/chats";
import { renderVi } from "@/test/render";

import { MessageLinks } from "./message-links";

function message(kind: "person" | "agent", refusal: string): GroupMessage {
  const author = kind === "person" ? { kind, login: "octo" } : { kind, handle: "lead" };
  const mention = { name: "dev", handle: "dev", login: null, run_id: null, refusal };
  return { id: 1, chat_id: 7, body: "@dev see this", author, mentions: [mention] } as unknown as GroupMessage;
}

describe("MessageLinks", () => {
  it("says under a person's message why a mention took no work", () => {
    renderVi(<MessageLinks message={message("person", "@dev is not in this chat")} />);
    expect(screen.getByTestId("mention-refused")).toHaveTextContent("@dev is not in this chat");
  });

  it("shows nothing under an agent's message for an @ that is a name in its words", () => {
    renderVi(<MessageLinks message={message("agent", "an agent's @ in a message hands no work")} />);
    expect(screen.queryByTestId("mention-refused")).toBeNull();
    expect(screen.queryByTestId("message-links")).toBeNull();
  });
});
