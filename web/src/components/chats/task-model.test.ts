import { describe, expect, it } from "vitest";

import type { ChatAuthor, GroupMessage } from "@/lib/chats";
import type { WorkAsk, WorkCard } from "@/lib/work";

import { agentCount, delegations, directTaskLine, foldLogs, foldRepeats, logExcerpt, taskLine, team } from "./task-model";

const OCTO: ChatAuthor = { kind: "person", login: "octo", name: "octo", deleted: false };
const HUB: ChatAuthor = { kind: "hub", name: "hub", deleted: false };
const agent = (handle: string): ChatAuthor => ({ kind: "agent", handle, project: "lumen-desk", name: handle, color: "#2563eb", deleted: false });
const LEAD = agent("lead");
const HELPER = agent("helper");
const CHECKER = agent("checker");

function message(id: number, seq: number, overrides: Partial<GroupMessage> = {}): GroupMessage {
  return {
    id,
    chat_id: 7,
    seq,
    thread_root_id: null,
    reply_count: 0,
    author: OCTO,
    run_id: null,
    body: `message ${id}`,
    mentions: [],
    reactions: [],
    label: [],
    created_at: `2026-11-20T08:${String(seq).padStart(2, "0")}:00Z`,
    ...overrides,
  };
}

function ask(agentHandle: string | null, from: string, root: number | null, overrides: Partial<WorkAsk> = {}): WorkAsk {
  return { kind: "ask", from_agent: from, agent: agentHandle, brief: `brief for ${agentHandle}`, status: "done", run_id: 100 + (root ?? 0), usd: 0.5, thread: root === null ? null : { chat_id: 7, root_id: root }, ...overrides };
}

function card(overrides: Partial<WorkCard> = {}): WorkCard {
  return {
    trace_id: 41,
    system: { id: 1, slug: "northwind-desk", name: "Northwind desk", project: "lumen-desk" },
    title: "Refund backlog",
    status: "running",
    agent: "lead",
    waiting: null,
    coordinator: "lead",
    outcome: null,
    left: null,
    chat_id: 3,
    message_id: 30,
    work_chat_id: 7,
    goal: "Every refund ticket of Northwind has a reply.",
    done_when: "No refund ticket is left without a reply.",
    asks: [],
    decisions: [],
    runs: 4,
    usd: 2,
    hop: 2,
    trace_runs: 4,
    trace_usd: 2,
    started_at: "2026-11-20T08:00:00Z",
    ended_at: null,
    seconds: 60,
    ...overrides,
  } as WorkCard;
}

describe("a task's line", () => {
  it("folds hub lines that repeat", () => {
    const items = foldRepeats([
      message(1, 1, { author: LEAD }),
      message(2, 2, { author: HUB, repeats: 1, body: "lead asked helper" }),
      message(3, 3, { author: HUB, repeats: 1, body: "helper answered" }),
      message(4, 4, { author: HUB, body: "The work started." }),
      message(5, 5, { author: HUB, repeats: 4 }),
    ]);
    expect(items.map((item) => (item.kind === "message" ? item.message.id : item.messages.map((one) => one.id)))).toEqual([1, [2, 3], 4, [5]]);
    expect(items[1]).toMatchObject({ kind: "notices", key: 2 });
    // An agent's message that repeats nothing of the hub stays a message, even with the field set.
    expect(foldRepeats([message(6, 6, { author: LEAD, repeats: 1 })])[0].kind).toBe("message");
  });

  it("puts each hand-off in the place of its question, the ones an agent opened inside it, and marks the answer", () => {
    const messages = [
      message(10, 1, { author: OCTO, body: "Clear the refund backlog" }),
      message(11, 2, { author: LEAD, body: "@helper draft the replies" }),
      message(12, 3, { author: HUB, repeats: 11 }),
      message(13, 4, { author: HELPER, thread_root_id: 11, body: "On it" }),
      message(14, 5, { author: HELPER, body: "@checker check the tone" }),
      message(15, 6, { author: CHECKER, thread_root_id: 14, body: "Tone is fine." }),
      message(16, 7, { author: HELPER, thread_root_id: 11, body: "The refund replies are drafted." }),
      message(17, 8, { author: LEAD, body: "Every refund ticket has a reply." }),
      message(18, 9, { author: LEAD, body: "forwarded", forwarded_by: "lead" }),
    ];
    const work = card({ status: "done", asks: [ask("helper", "lead", 11), ask("checker", "helper", 14)] });
    const { items, answer } = taskLine(work, 7, messages);
    expect(items.map((item) => item.kind)).toEqual(["message", "delegation", "notices", "message", "message"]);
    const top = items[1].kind === "delegation" ? items[1].delegation : null;
    expect(top?.agent).toBe("helper");
    expect(top?.thread.map((one) => one.id)).toEqual([13, 16]);
    expect(top?.result?.id).toBe(16);
    expect(top?.children.map((child) => [child.agent, child.result?.id])).toEqual([["checker", 15]]);
    expect(answer).toBe(17);
    expect(taskLine({ ...work, status: "running" }, 7, messages).answer).toBeNull();
  });

  it("keeps a hand-off with no result while its ask runs, and one whose question is not read yet", () => {
    const tree = delegations({ asks: [ask("helper", "lead", 11, { status: "running" }), ask("checker", "lead", 90)] }, 7, [message(11, 1, { author: LEAD }), message(13, 2, { author: HELPER, thread_root_id: 11 })]);
    expect(tree.map((node) => [node.agent, node.result?.id ?? null, node.question?.id ?? null])).toEqual([
      ["helper", null, 11],
      ["checker", null, null],
    ]);
  });

  it("names the team: the coordinator with what the asks leave, then each agent asked once", () => {
    const work = card({ status: "done", usd: 2, runs: 4, asks: [ask("helper", "lead", 11, { usd: 0.5 }), ask("helper", "lead", 11, { usd: 0.25 }), ask(null, "lead", null, { status: "refused", run_id: null })] });
    const reply = message(16, 3, { author: HELPER, thread_root_id: 11 });
    const rows = team(work, [reply], null);
    expect(rows.map((row) => [row.agent, row.coordinator, row.usd, row.runs, row.result?.id ?? null])).toEqual([
      ["lead", true, 1.25, 2, null],
      ["helper", false, 0.75, 2, 16],
    ]);
    expect(agentCount(work)).toBe(2);
  });
});

describe("a pasted log", () => {
  it("shows six lines, the ones that say error first, until it is opened", () => {
    const lines = Array.from({ length: 12 }, (_, index) => (index === 2 ? "ERROR: refund 17 failed" : `line ${index}`));
    expect(logExcerpt(lines)).toEqual(["ERROR: refund 17 failed", "line 7", "line 8", "line 9", "line 10", "line 11"]);
    const { body, hidden } = foldLogs(["Here is the log:", "```", ...lines, "```", "Done."].join("\n"));
    expect(hidden).toBe(6);
    expect(body.split("\n")).toEqual(["Here is the log:", "```", "ERROR: refund 17 failed", "line 7", "line 8", "line 9", "line 10", "line 11", "```", "Done."]);
    const short = "```\na\nb\n```";
    expect(foldLogs(short)).toEqual({ body: short, hidden: 0 });
  });
});

describe("the direct chat on Home", () => {
  it("keeps one card per task in the direct chat", () => {
    const opened = card({ trace_id: 41, status: "done" });
    const later = card({ trace_id: 41, status: "done", title: "Refund backlog, later" });
    const line = [
      message(1, 1, { author: OCTO, body: "Hello" }),
      message(2, 2, { author: LEAD, thread_root_id: 1, body: "Hi" }),
      message(30, 3, { author: OCTO, body: "Clear the refund backlog" }),
      message(31, 4, { author: HUB, thread_root_id: 30, work: opened }),
      message(32, 5, { author: LEAD, thread_root_id: 30, body: "Asking helper" }),
      message(33, 6, { author: LEAD, thread_root_id: 30, body: "The refund replies are drafted.", forwarded_by: "helper" }),
      message(34, 7, { author: HUB, thread_root_id: 30, repeats: 32, body: "lead asked helper" }),
      message(35, 8, { author: HUB, thread_root_id: 30, work: later }),
      message(36, 9, { author: LEAD, thread_root_id: 30, body: "Every refund ticket has a reply." }),
      message(37, 10, { author: OCTO, thread_root_id: 30, body: "Thanks" }),
    ];
    const items = directTaskLine(line);
    expect(items.map((item) => `${item.kind}:${item.message.id}`)).toEqual(["message:1", "message:2", "message:30", "task:31", "message:36", "message:37"]);
    // While the task runs, the coordinator's words are the task's alone.
    const running = line.map((one) => (one.work ? { ...one, work: { ...one.work, status: "running" as const } } : one));
    expect(directTaskLine(running).map((item) => item.message.id)).toEqual([1, 2, 30, 31, 37]);
  });
});
