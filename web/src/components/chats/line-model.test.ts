import { describe, expect, it } from "vitest";

import type { Chat, ChatAuthor, ChatPage, GroupMessage } from "@/lib/chats";

import { feedOf, readInto, withMessage } from "./feed-model";
import { authorKey, lineMessages, preview, quotes, runsOn, threadOf } from "./line-model";

const OCTO: ChatAuthor = { kind: "person", login: "octo", name: "octo", deleted: false };
const REVIEWER: ChatAuthor = { kind: "agent", handle: "reviewer", project: "demo", name: "Reviewer", color: "#2563eb", deleted: false };

function message(id: number, seq: number, overrides: Partial<GroupMessage> = {}): GroupMessage {
  return {
    id,
    chat_id: 1,
    seq,
    thread_root_id: null,
    reply_count: 0,
    author: OCTO,
    run_id: null,
    body: `message ${id}`,
    mentions: [],
    reactions: [],
    label: [],
    created_at: `2026-10-14T08:${String(seq).padStart(2, "0")}:00Z`,
    ...overrides,
  };
}

function page(messages: GroupMessage[], lastSeq: number, more = false): ChatPage {
  return { chat: { id: 1, title: "Reviewer", last_seq: lastSeq } as Chat, messages, more };
}

const ids = (messages: readonly GroupMessage[]) => messages.map((item) => item.id);

describe("a chat's line", () => {
  // Octo asks (1), the agent answers in its thread (2), Octo asks again (3), the agent answers 3 (4), then 1 again (5).
  const asked = message(1, 1, { reply_count: 2 });
  const answer = message(2, 2, { thread_root_id: 1, author: REVIEWER });
  const again = message(3, 3, { reply_count: 1 });
  const answerAgain = message(4, 4, { thread_root_id: 3, author: REVIEWER });
  const late = message(5, 5, { thread_root_id: 1, author: REVIEWER });

  it("shows a group chat's messages that are no reply, and every message of a direct chat in order", () => {
    const feed = feedOf(page([asked, answer, again, answerAgain, late], 5));
    expect(ids(lineMessages(feed, "group"))).toEqual([1, 3]);
    expect(ids(lineMessages(feed, "direct"))).toEqual([1, 2, 3, 4, 5]);
    // A reply that arrives live takes its place at the end of a direct chat's line.
    const later = withMessage(feed, message(7, 7, { thread_root_id: 3, author: REVIEWER }));
    expect(ids(lineMessages(later, "direct"))).toEqual([1, 2, 3, 4, 5, 7]);
  });

  it("leaves out of a direct chat's line what only a thread read brought from before the oldest page", () => {
    const feed = readInto(feedOf(page([again, answerAgain], 5, true)), page([asked, answer, late], 5), "thread");
    expect(ids(lineMessages(feed, "direct"))).toEqual([3, 4, 5]);
  });

  it("quotes what a reply answers only when another thread came between, the latest of someone else's", () => {
    const feed = feedOf(page([asked, answer, again, answerAgain, late], 5));
    const found = quotes(lineMessages(feed, "direct"), feed);
    // 2 sits under 1 and 4 under 3: no quote. 5 comes after 4, of another thread: it answers Octo's 1, not its own 2.
    expect([...found.keys()]).toEqual([5]);
    expect(found.get(5)).toEqual({ root: 1, message: asked });
    // Octo's own reply in the thread after the agent's answer answers that answer.
    const reply = message(6, 6, { thread_root_id: 3 });
    const more = withMessage(feed, reply);
    expect(quotes(lineMessages(more, "direct"), more).get(6)).toEqual({ root: 3, message: answerAgain });
  });

  it("quotes nothing it does not hold: the thread's root stands in for what the page never read", () => {
    const feed = feedOf(page([again, late], 5, true));
    expect(quotes(lineMessages(feed, "direct"), feed).get(5)).toEqual({ root: 1, message: null });
  });

  it("runs a message on from the one above by the same author, the same day, within five minutes", () => {
    const first = message(1, 1);
    expect(runsOn(undefined, first, true)).toBe(false);
    expect(runsOn(first, message(2, 2), true)).toBe(true);
    expect(runsOn(first, message(2, 2), false)).toBe(false);
    expect(runsOn(first, message(2, 2, { author: REVIEWER }), true)).toBe(false);
    expect(runsOn(first, message(2, 2, { created_at: "2026-10-14T08:06:30Z" }), true)).toBe(false);
    expect(runsOn(first, message(2, 2, { author: { ...OCTO, login: "OCTO" } }), true)).toBe(true);
  });

  it("keys authors by kind and name, and a message's thread by its first message", () => {
    expect(authorKey(OCTO)).toBe("person:octo");
    expect(authorKey(REVIEWER)).toBe("agent:reviewer");
    expect(authorKey({ kind: "hub", name: "Hub" })).toBe("hub");
    expect([threadOf(asked), threadOf(late)]).toEqual([1, 1]);
  });

  it("previews a message as one line of plain words", () => {
    expect(preview("## Fixed\n\nThe **wait** on `chat/12`, see [PR #7](https://github.com/example-org/api/pull/7).\n\n- one\n- _two_")).toBe(
      "Fixed The wait on chat/12, see PR #7. one two",
    );
    expect(preview("```ts\nconst a = 1;\n```")).toBe("const a = 1;");
    expect(preview("__init__.py stays")).toBe("__init__.py stays");
    expect(preview("x".repeat(200), 10)).toBe(`${"x".repeat(9)}…`);
  });
});
