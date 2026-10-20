import { describe, expect, it } from "vitest";

import type { Chat, ChatAuthor, ChatEvent, ChatPage, ChatProgress, GroupMessage } from "@/lib/chats";

import {
  applyEvents,
  feedOf,
  mainMessages,
  reacted,
  readInto,
  replyCount,
  threadMessages,
  threadTail,
  withMessage,
  withProgress,
  withReactions,
} from "./feed-model";

const OCTO: ChatAuthor = { kind: "person", login: "octo", name: "octo", deleted: false };
const MAI: ChatAuthor = { kind: "person", login: "mai", name: "mai", deleted: false };
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
  const chat = { id: 1, title: "Release", last_seq: lastSeq } as Chat;
  return { chat, messages, more };
}

const event = (seq: number, body: Partial<ChatEvent>): ChatEvent => ({ seq, kind: "message", created_at: "2026-10-14T09:00:00Z", ...body }) as ChatEvent;

describe("a chat's feed", () => {
  it("opens with the first page, as of the chat's last event then", () => {
    const feed = feedOf(page([message(1, 1, { reply_count: 2 }), message(2, 2, { thread_root_id: 1 }), message(4, 4)], 5, true));
    expect(feed.lastSeq).toBe(5);
    expect(feed.oldestSeq).toBe(1);
    expect(feed.more).toBe(true);
    expect(mainMessages(feed).map((item) => item.id)).toEqual([1, 4]);
    // The reply in the page is in the count the page gave: not counted again.
    expect(replyCount(feed, 1)).toBe(2);
  });

  it("adds new messages from events once, and counts a new reply on top of what the page said", () => {
    const start = feedOf(page([message(1, 1, { reply_count: 1 })], 3));
    const reply = message(5, 4, { thread_root_id: 1, author: REVIEWER });
    const { feed, members } = applyEvents(start, [event(4, { message: reply }), event(4, { message: reply }), event(2, { message: message(9, 2) })], "octo");
    expect(members).toBe(false);
    expect(feed.lastSeq).toBe(4);
    expect(replyCount(feed, 1)).toBe(2);
    expect(threadMessages(feed, 1).map((item) => item.id)).toEqual([1, 5]);
    expect(threadTail(feed, 1)).toEqual({ last: reply, authors: [REVIEWER] });
    expect(mainMessages(feed).map((item) => item.id)).toEqual([1]);
  });

  it("counts the replies of a message that came as an event from none, since they all come after it", () => {
    const start = feedOf(page([], 0));
    const root = message(1, 1, { reply_count: 3 });
    const { feed } = applyEvents(start, [event(1, { message: root }), event(2, { message: message(2, 2, { thread_root_id: 1 }) })], "octo");
    expect(replyCount(feed, 1)).toBe(1);
  });

  it("takes a thread's count from its read, and leaves a message read for its thread out of the chat above the others", () => {
    const start = feedOf(page([message(10, 10, { reply_count: 0 })], 12));
    const thread = page([message(3, 3, { reply_count: 2 }), message(4, 4, { thread_root_id: 3 }), message(11, 11, { thread_root_id: 3 })], 12);
    const feed = readInto(start, thread, "thread");
    expect(replyCount(feed, 3)).toBe(2);
    expect(mainMessages(feed).map((item) => item.id)).toEqual([10]);
    expect(feed.oldestSeq).toBe(10);
    const older = readInto(feed, page([message(3, 3, { reply_count: 2 }), message(6, 6)], 12, false), "older");
    expect(older.oldestSeq).toBe(3);
    expect(older.more).toBe(false);
    expect(mainMessages(older).map((item) => item.id)).toEqual([3, 6, 10]);
  });

  it("applies reactions put and taken off, by count, people and agents, and tells member changes apart", () => {
    const start = feedOf(page([message(1, 1)], 1));
    const { feed, members } = applyEvents(
      start,
      [
        event(2, { kind: "reaction_added", reaction: { message_id: 1, emoji: "\u{1F440}", by: REVIEWER, count: 1 } }),
        event(3, { kind: "reaction_added", reaction: { message_id: 1, emoji: "\u{1F389}", by: OCTO, count: 1 } }),
        event(4, { kind: "reaction_added", reaction: { message_id: 1, emoji: "\u{1F389}", by: MAI, count: 2 } }),
        event(5, { kind: "reaction_removed", reaction: { message_id: 1, emoji: "\u{1F440}", by: REVIEWER, count: 0 } }),
        event(6, { kind: "member_added", member: { member: MAI, by: "octo" } }),
        event(7, { kind: "reaction_added", reaction: { message_id: 99, emoji: "✅", by: REVIEWER, count: 1 } }),
      ],
      "octo",
    );
    expect(members).toBe(true);
    expect(feed.entries.get(1)?.message.reactions).toEqual([{ emoji: "\u{1F389}", count: 2, mine: true, people: ["octo", "mai"], agents: [] }]);
  });

  it("says the chat's image a batch of events leaves, the latest one, and none for a batch without an image event", () => {
    const start = feedOf(page([message(1, 1)], 1));
    const changed = applyEvents(
      start,
      [
        event(2, { kind: "image_changed", image: { image_url: "/v1/chats/1/image?v=aaaa", by: "mai" } }),
        event(3, { kind: "image_changed", image: { image_url: null, by: "octo" } }),
        event(4, { kind: "image_changed", image: { image_url: "/v1/chats/1/image?v=bbbb", by: "mai" } }),
      ],
      "octo",
    );
    expect(changed.image).toEqual({ image_url: "/v1/chats/1/image?v=bbbb", by: "mai" });
    expect([changed.members, changed.feed.lastSeq]).toEqual([false, 4]);
    expect(changed.feed.entries).toBe(start.entries);
    const removed = applyEvents(changed.feed, [event(5, { kind: "image_changed", image: { image_url: null, by: "octo" } })], "octo");
    expect(removed.image).toEqual({ image_url: null, by: "octo" });
    // A repeat of an event already applied changes nothing, the image included.
    expect(applyEvents(removed.feed, [event(4, { kind: "image_changed", image: { image_url: "/v1/chats/1/image?v=bbbb", by: "mai" } })], "octo").image).toBeNull();
    expect(applyEvents(start, [event(2, { message: message(2, 2) })], "octo").image).toBeNull();
  });

  it("keeps a reaction's others when the visitor takes theirs off, and its count from the event", () => {
    const reactions = [{ emoji: "\u{1F44D}", count: 2, mine: true, people: ["octo", "mai"], agents: [] }];
    expect(reacted(reactions, { emoji: "\u{1F44D}", by: OCTO, count: 1 }, false, "octo")).toEqual([
      { emoji: "\u{1F44D}", count: 1, mine: false, people: ["mai"], agents: [] },
    ]);
    expect(reacted(reactions, { emoji: "✅", by: REVIEWER, count: 1 }, true, "octo")).toHaveLength(2);
  });

  it("takes a message the visitor sent, and the reactions a write answered", () => {
    const start = feedOf(page([message(1, 1)], 1));
    const sent = withMessage(start, message(2, 2));
    expect(mainMessages(sent).map((item) => item.id)).toEqual([1, 2]);
    expect(withMessage(sent, message(2, 2, { body: "changed" })).entries.get(2)?.message.body).toBe("message 2");
    const answered = withReactions(sent, message(1, 1, { body: "ignored", reactions: [{ emoji: "\u{1F44D}", count: 1, mine: true, people: ["octo"], agents: [] }] }));
    expect(answered.entries.get(1)?.message.reactions).toHaveLength(1);
    expect(answered.entries.get(1)?.message.body).toBe("message 1");
  });

  it("takes the progress the stream sent for every message of the chat, and replaces only the messages it changed", () => {
    const step = (message_id: number, run_id: number, value: ChatProgress["step"]): ChatProgress => ({
      message_id,
      run_id,
      handle: "reviewer",
      step: value,
      worker: "studio",
      repo: null,
      since: "2026-10-14T08:00:00Z",
    });
    const start = feedOf(page([message(1, 1, { progress: [step(1, 7, "waiting")] }), message(2, 2), message(3, 3)], 3));
    const claimed = withProgress(start, [step(1, 7, "claimed"), step(2, 8, "inbox")]);
    expect(claimed.entries.get(1)?.message.progress?.map((item) => item.step)).toEqual(["claimed"]);
    expect(claimed.entries.get(2)?.message.progress?.map((item) => item.step)).toEqual(["inbox"]);
    expect(claimed.entries.get(3)).toBe(start.entries.get(3)); // no progress before or after: the same entry
    expect(withProgress(claimed, [step(1, 7, "claimed"), step(2, 8, "inbox")])).toBe(claimed); // nothing changed
    const answered = withProgress(claimed, [step(9, 9, "working")]); // a message the page does not hold
    expect([1, 2, 3].map((id) => answered.entries.get(id)?.message.progress ?? [])).toEqual([[], [], []]);
    expect(answered.entries.has(9)).toBe(false);
  });
});
