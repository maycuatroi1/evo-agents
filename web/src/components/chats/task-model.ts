import type { GroupMessage } from "@/lib/chats";
import { askAnswer, type WorkAsk, type WorkCard, type WorkStatus } from "@/lib/work";

import { threadOf } from "./line-model";

/**
 * A task as Home draws it (web/DESIGN.md, Chat home, The task): the work chat's line folded into hand-offs, the hub's
 * lines that repeat a message folded into one row, long pasted logs cut to their lines that matter, the coordinator's
 * last word marked as the answer, and the direct chat cut to the person's messages, one card a task and the answer.
 * Pure functions over the messages a chat's feed holds, which they never change.
 */

/** A pasted log (a fenced block) longer than this shows only this many lines until it is opened. */
export const LOG_LINES = 6;
/** A brief shows this many lines until Read all. */
export const BRIEF_LINES = 3;

const bySeq = (a: GroupMessage, b: GroupMessage) => a.seq - b.seq;

/** A row of a line: a message, or hub lines in a row that each repeat another message (`repeats`), folded into one. */
export type FoldedItem = { kind: "message"; message: GroupMessage } | { kind: "notices"; key: number; messages: GroupMessage[] };

/** `messages` (oldest first) with each run of the hub's lines that repeat a message folded into one row of notices. */
export function foldRepeats(messages: readonly GroupMessage[]): FoldedItem[] {
  const items: FoldedItem[] = [];
  for (const message of messages) {
    const repeats = message.author.kind === "hub" && message.repeats != null;
    const last = items.at(-1);
    if (repeats && last?.kind === "notices") last.messages.push(message);
    else if (repeats) items.push({ kind: "notices", key: message.id, messages: [message] });
    else items.push({ kind: "message", message });
  }
  return items;
}

const FENCE = /^[ \t]{0,3}(`{3,}|~{3,})/;

/** The `keep` lines of a log that show while it is folded: those that say "error" first, then its last ones. */
export function logExcerpt(lines: readonly string[], keep = LOG_LINES): string[] {
  if (lines.length <= keep) return [...lines];
  const chosen = new Set<number>();
  lines.forEach((line, index) => {
    if (chosen.size < keep && /error/i.test(line)) chosen.add(index);
  });
  for (let index = lines.length - 1; index >= 0 && chosen.size < keep; index -= 1) chosen.add(index);
  return [...chosen].sort((a, b) => a - b).map((index) => lines[index]);
}

/**
 * `markdown` with each fenced block of more than `LOG_LINES` lines cut to its excerpt (`logExcerpt`), and how many
 * lines that hid; a body with no long block comes back as it was, with 0. A fence left open runs to the end.
 */
export function foldLogs(markdown: string): { body: string; hidden: number } {
  const lines = markdown.split("\n");
  const out: string[] = [];
  let hidden = 0;
  let index = 0;
  while (index < lines.length) {
    const open = FENCE.exec(lines[index]);
    if (!open) {
      out.push(lines[index]);
      index += 1;
      continue;
    }
    const marker = open[1];
    const close = new RegExp(`^[ \\t]{0,3}${marker[0] === "`" ? "`" : "~"}{${marker.length},}[ \\t]*$`);
    let end = index + 1;
    while (end < lines.length && !close.test(lines[end])) end += 1;
    const inside = lines.slice(index + 1, end);
    const shown = logExcerpt(inside);
    hidden += inside.length - shown.length;
    out.push(lines[index], ...shown);
    if (end < lines.length) out.push(lines[end]);
    index = end + 1;
  }
  return hidden ? { body: out.join("\n"), hidden } : { body: markdown, hidden: 0 };
}

/**
 * A hand-off of the task: the thread of the agent asked (one thread for every ask of the same agent from the same
 * place), its question on the main line, the asks it carries, the thread's messages, the agent's result once its last
 * ask is done, and the hand-offs that agent opened itself.
 */
export type Delegation = {
  root: number;
  question: GroupMessage | null;
  asks: WorkAsk[];
  agent: string | null;
  from: string;
  thread: GroupMessage[];
  result: GroupMessage | null;
  children: Delegation[];
};

/**
 * The hand-offs of `card` in work chat `chatId`, as a tree: a hand-off an asked agent opened sits in the latest earlier
 * hand-off to that agent; the others are the coordinator's, at the top. Each list is in the order of its question.
 */
export function delegations(card: Pick<WorkCard, "asks">, chatId: number, messages: readonly GroupMessage[]): Delegation[] {
  const byRoot = new Map<number, Delegation>();
  for (const ask of card.asks) {
    if (!ask.thread || ask.thread.chat_id !== chatId) continue;
    const root = ask.thread.root_id;
    const held = byRoot.get(root);
    if (held) {
      held.asks.push(ask);
      continue;
    }
    byRoot.set(root, { root, question: null, asks: [ask], agent: ask.agent, from: ask.from_agent, thread: [], result: null, children: [] });
  }
  for (const message of messages) {
    const node = byRoot.get(threadOf(message));
    if (!node) continue;
    if (message.id === node.root) node.question = message;
    else node.thread.push(message);
  }
  const seqOf = (node: Delegation) => node.question?.seq ?? Number.MAX_SAFE_INTEGER;
  const nodes = [...byRoot.values()].sort((a, b) => seqOf(a) - seqOf(b) || a.root - b.root);
  const top: Delegation[] = [];
  for (const node of nodes) {
    node.thread.sort(bySeq);
    const last = node.asks.at(-1)!;
    node.result = last.status === "done" ? askAnswer(last, node.thread) : null;
    const parent = nodes.filter((other) => other !== node && other.agent === node.from && seqOf(other) < seqOf(node)).at(-1);
    (parent ? parent.children : top).push(node);
  }
  return top;
}

/** The coordinator's last message on the main line once the work is done: what the task answers; null before. */
export function finalAnswer(card: Pick<WorkCard, "status" | "coordinator">, line: readonly GroupMessage[]): GroupMessage | null {
  if (card.status !== "done" || !card.coordinator) return null;
  return line.findLast((message) => message.author.kind === "agent" && message.author.handle === card.coordinator && !message.forwarded_by) ?? null;
}

/** A row of the task's main line: a message (the answer marked), folded notices, or a hand-off with its thread. */
export type TaskItem = FoldedItem | { kind: "delegation"; delegation: Delegation };

/**
 * The task's line: the work chat's main line (`messages` that are no reply), oldest first, each hand-off in the place
 * of its question (one an agent opened sits inside the hand-off to that agent instead), the hub's lines that repeat a
 * message folded, and the answer (`finalAnswer`) named.
 */
export function taskLine(card: Pick<WorkCard, "asks" | "status" | "coordinator">, chatId: number, messages: readonly GroupMessage[]): { items: TaskItem[]; answer: number | null } {
  const tree = delegations(card, chatId, messages);
  const nested = new Set<number>();
  const walk = (nodes: Delegation[]) => nodes.forEach((node) => (nested.add(node.root), walk(node.children)));
  walk(tree);
  const top = new Map(tree.map((node) => [node.root, node]));
  const main = messages.filter((message) => message.thread_root_id === null).sort(bySeq);
  const answer = finalAnswer(card, main.filter((message) => !nested.has(message.id)));
  const items: TaskItem[] = [];
  let run: GroupMessage[] = [];
  const flush = () => {
    items.push(...foldRepeats(run));
    run = [];
  };
  for (const message of main) {
    const node = top.get(message.id);
    if (node) {
      flush();
      items.push({ kind: "delegation", delegation: node });
    } else if (!nested.has(message.id)) run.push(message);
  }
  flush();
  // A hand-off whose question the feed does not hold yet still shows, after the line.
  for (const node of tree) if (!node.question) items.push({ kind: "delegation", delegation: node });
  return { items, answer: answer?.id ?? null };
}

/** A member of the task's team: the coordinator, with the work's state, or an agent asked, with its last ask's. */
export type TeamRow = { agent: string; coordinator: boolean; work: WorkStatus | null; status: string | null; usd: number; runs: number; result: GroupMessage | null };

/**
 * The task's team: the coordinator first (the work's state, the cost and runs the asks do not account for, the answer),
 * then each agent asked in the order it was first asked (its last ask's state, the cost and runs of its asks, its last
 * answer). A refused ask names no agent and adds no one.
 */
export function team(card: Pick<WorkCard, "asks" | "status" | "coordinator" | "usd" | "runs">, messages: readonly GroupMessage[], answer: GroupMessage | null): TeamRow[] {
  const rows = new Map<string, TeamRow & { last: WorkAsk }>();
  for (const ask of card.asks) {
    if (!ask.agent || ask.agent === card.coordinator) continue;
    const held = rows.get(ask.agent);
    const runs = ask.run_id === null ? 0 : 1;
    if (held) rows.set(ask.agent, { ...held, status: ask.status, usd: held.usd + ask.usd, runs: held.runs + runs, last: ask });
    else rows.set(ask.agent, { agent: ask.agent, coordinator: false, work: null, status: ask.status, usd: ask.usd, runs, result: null, last: ask });
  }
  const agents = [...rows.values()].map(({ last, ...row }) => ({ ...row, result: askAnswer(last, messages) }));
  if (!card.coordinator) return agents;
  const usd = Math.max(0, card.usd - agents.reduce((sum, row) => sum + row.usd, 0));
  const runs = Math.max(0, card.runs - agents.reduce((sum, row) => sum + row.runs, 0));
  return [{ agent: card.coordinator, coordinator: true, work: card.status, status: null, usd, runs, result: answer }, ...agents];
}

/** The agents of a task: its coordinator and each agent asked, once. */
export function agentCount(card: Pick<WorkCard, "asks" | "coordinator">): number {
  const agents = new Set(card.asks.map((ask) => ask.agent).filter((agent): agent is string => agent !== null));
  if (card.coordinator) agents.add(card.coordinator);
  return agents.size;
}

/** A row of a direct chat on Home: a message, or the card of a task a message opened, where the hub's card of it sits. */
export type DirectItem = { kind: "message"; message: GroupMessage } | { kind: "task"; message: GroupMessage; work: WorkCard };

/**
 * A direct chat's line on Home (`line`: every message, oldest first): a thread that opened a task keeps only the
 * person's messages, one card for each task (where the hub's message that carries its work card sits) and, once the
 * task has ended, the coordinator's last message after its card; the rest of that thread (what the coordinator said
 * meanwhile, the answers it forwarded, the hub's lines) is the task's. A thread that opened no task shows whole.
 */
export function directTaskLine(line: readonly GroupMessage[]): DirectItem[] {
  const threads = new Map<number, GroupMessage[]>();
  for (const message of line) threads.set(threadOf(message), [...(threads.get(threadOf(message)) ?? []), message]);
  const kept = new Set<number>();
  const cards = new Map<number, WorkCard>();
  for (const messages of threads.values()) {
    const carriers = messages.filter((message) => message.work);
    if (carriers.length === 0) {
      for (const message of messages) kept.add(message.id);
      continue;
    }
    const traces = new Set<number>();
    for (const carrier of carriers) {
      const work = carrier.work!;
      if (traces.has(work.trace_id)) continue;
      traces.add(work.trace_id);
      cards.set(carrier.id, work);
      if (work.status !== "done" && work.status !== "failed") continue;
      const answer = messages.findLast(
        (message) => message.seq > carrier.seq && message.author.kind === "agent" && message.author.handle === work.coordinator && !message.forwarded_by && !message.work,
      );
      if (answer) kept.add(answer.id);
    }
    for (const message of messages) if (message.author.kind === "person") kept.add(message.id);
  }
  const items: DirectItem[] = [];
  for (const message of line) {
    const work = cards.get(message.id);
    if (work) items.push({ kind: "task", message, work });
    else if (kept.has(message.id)) items.push({ kind: "message", message });
  }
  return items;
}
