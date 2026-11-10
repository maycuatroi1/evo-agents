import { describe, expect, it } from "vitest";

import type { ChatSummary, GroupMessage } from "./chats";
import { answerable, askAnswer, askPlaces, exceptionPlaces, groupWorkChats, observeHref, threadHref, type WorkAsk, type WorkBrief, type WorkDecision } from "./work";

const SYSTEM = { id: 7, slug: "lumen-review", name: "Lumen review", project: "northwind" };

function brief(status: WorkBrief["status"], system: WorkBrief["system"] = SYSTEM): WorkBrief {
  return { trace_id: 1, system, title: "Fix the build", status, agent: null, waiting: null, coordinator: "lead" };
}

function chat(id: number, work: WorkBrief | null = null): ChatSummary {
  return { id, title: `Chat ${id}`, kind: work ? "work" : "group", work } as unknown as ChatSummary;
}

function message(id: number, seq: number, overrides: Partial<GroupMessage> = {}): GroupMessage {
  return {
    id,
    seq,
    chat_id: 5,
    thread_root_id: null,
    run_id: null,
    body: `message ${id}`,
    author: { kind: "agent", handle: "fixer", name: "Fixer", project: "northwind" },
    ...overrides,
  } as unknown as GroupMessage;
}

function ask(overrides: Partial<WorkAsk> = {}): WorkAsk {
  return { kind: "ask", from_agent: "lead", agent: "fixer", brief: "Fix the test", status: "running", run_id: 11, usd: 0, thread: { chat_id: 5, root_id: 100 }, ...overrides };
}

function decision(overrides: Partial<WorkDecision> = {}): WorkDecision {
  return { kind: "person", id: 3, rule: null, outcome: null, question: "Retry?", state: "open", answer: null, asked_of: "Octo", run_id: 11, at: "2026-11-10T08:00:00Z", ...overrides };
}

describe("groupWorkChats", () => {
  it("groups work chats under their system, done ones apart, and keeps the others in order", () => {
    const other = { id: 9, slug: "lumen-ops", name: null, project: "northwind" };
    const { others, groups } = groupWorkChats([chat(1), chat(2, brief("running")), chat(3, brief("done")), chat(4, brief("needs_person", other)), chat(5), chat(6, brief("failed"))]);
    expect(others.map((each) => each.id)).toEqual([1, 5]);
    expect(groups.map((group) => [group.name, group.open.map((each) => each.id), group.done.map((each) => each.id)])).toEqual([
      ["Lumen review", [2, 6], [3]],
      ["lumen-ops", [4], []],
    ]);
  });
});

describe("links", () => {
  it("leads to Observe of the system, and to nothing once it is gone", () => {
    expect(observeHref(SYSTEM)).toBe("/p/northwind/systems/lumen-review?mode=observe");
    expect(observeHref({ ...SYSTEM, id: null })).toBeNull();
    expect(threadHref({ chat_id: 5, root_id: 100 })).toBe("/chats/5?thread=100");
  });
});

describe("answerable", () => {
  it("is an open person decision that asks the visitor", () => {
    expect(answerable(decision(), "octo")).toBe(true);
    expect(answerable(decision(), "mona")).toBe(false);
    expect(answerable(decision({ state: "answered" }), "octo")).toBe(false);
    expect(answerable(decision({ kind: "orchestrator" }), "octo")).toBe(false);
    expect(answerable(decision(), null)).toBe(false);
  });
});

describe("places", () => {
  it("puts each ask under the main-line question that starts its thread in this chat", () => {
    const places = askPlaces({ asks: [ask(), ask({ agent: "writer", thread: { chat_id: 6, root_id: 200 } }), ask({ agent: null, thread: null, status: "refused" })] }, 5);
    expect([...places.keys()]).toEqual([100]);
  });

  it("puts an exception under the first message of the thread its run wrote in", () => {
    const messages = [message(40, 1, { author: { kind: "hub" } as GroupMessage["author"] }), message(41, 2, { thread_root_id: 40, run_id: 77 }), message(42, 3, { run_id: 78 })];
    const places = exceptionPlaces({ decisions: [decision({ kind: "orchestrator", run_id: 77, rule: "failed" }), decision({ kind: "orchestrator", id: 4, run_id: 78 }), decision({ kind: "orchestrator", id: 5, run_id: 99 }), decision()] }, messages);
    expect([...places.entries()].map(([root, list]) => [root, list.map((each) => each.id)])).toEqual([
      [40, [3]],
      [42, [4]],
    ]);
  });

  it("answers an ask with the latest reply of the agent asked in its thread", () => {
    const messages = [message(100, 1), message(101, 2, { thread_root_id: 100 }), message(102, 4, { thread_root_id: 100 }), message(103, 3, { thread_root_id: 100, author: { kind: "person", login: "octo" } as GroupMessage["author"] })];
    expect(askAnswer(ask(), messages)?.id).toBe(102);
    expect(askAnswer(ask({ agent: "writer" }), messages)).toBeNull();
    expect(askAnswer(ask({ thread: null }), messages)).toBeNull();
  });
});
