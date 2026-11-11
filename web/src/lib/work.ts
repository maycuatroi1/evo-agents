import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

type Schemas = components["schemas"];
export type WorkCard = Schemas["WorkCard"];
export type WorkBrief = Schemas["WorkBrief"];
export type WorkAsk = Schemas["WorkAsk"];
export type WorkDecision = Schemas["WorkDecision"];
export type WorkWaiting = Schemas["WorkWaiting"];
export type WorkStatus = WorkBrief["status"];

type Summary = Schemas["ChatSummary"];
type Message = Schemas["GroupMessage"];

/**
 * The work of a trace of an agent system as the web shows it (docs/hub.md, Work chats and The work card): the card a
 * work chat pins over its messages (GET /v1/chats/{id}/work), the brief the chat list groups by system, and where in a
 * work chat's main line each sub-task and each exception of the Orchestrator sits.
 */

export const workKeys = {
  card: (chatId: number) => ["chats", "work", chatId] as const,
};

/** A work chat's card is read again this often while its work is not done; a change in its chat reads it at once too. */
export const WORK_LIVE_MS = 15_000;

export const workCardQuery = (api: ApiSource, chatId: number) =>
  queryOptions({
    queryKey: workKeys.card(chatId),
    queryFn: ({ signal }): Promise<WorkCard> => call(api().GET("/v1/chats/{chat_id}/work", { params: { path: { chat_id: chatId } }, signal })),
    refetchInterval: (query) => (query.state.data?.status === "done" || query.state.error ? false : WORK_LIVE_MS),
  });

/** Observe of the work's system, where its traces are drawn; null once the system or its project is gone. */
export function observeHref(system: WorkBrief["system"]): Route | null {
  if (system.id === null || !system.project) return null;
  return `/p/${encodeURIComponent(system.project)}/systems/${encodeURIComponent(system.slug)}?mode=observe` as Route;
}

export type WorkGroup = {
  /** The system's slug, which names the group even once the system is deleted. */
  key: string;
  name: string;
  /** Running, waiting for a person or failed: the latest active first, as the list gave them. */
  open: Summary[];
  /** Done: the list collapses them. */
  done: Summary[];
};

/**
 * The visitor's chats split into the other chats, in the list's order, and the work chats grouped under their system:
 * the groups in the order of their latest active chat, each with its open works first and the done ones apart.
 */
export function groupWorkChats(chats: readonly Summary[]): { others: Summary[]; groups: WorkGroup[] } {
  const others: Summary[] = [];
  const groups = new Map<string, WorkGroup>();
  for (const chat of chats) {
    const work = chat.work;
    if (!work) {
      others.push(chat);
      continue;
    }
    const key = `${work.system.project ?? ""}/${work.system.slug}`;
    let group = groups.get(key);
    if (!group) {
      group = { key, name: work.system.name ?? work.system.slug, open: [], done: [] };
      groups.set(key, group);
    }
    (work.status === "done" ? group.done : group.open).push(chat);
  }
  return { others, groups: [...groups.values()] };
}

/** The decision the card answers in place: open, asking a person, and asking the visitor. */
export function answerable(decision: WorkDecision, me: string | null): boolean {
  return decision.kind === "person" && decision.state === "open" && me !== null && decision.asked_of?.toLowerCase() === me.toLowerCase();
}

/** The main-line messages of a work chat that start the thread of a sub-task, by message id. */
export function askPlaces(card: Pick<WorkCard, "asks">, chatId: number): Map<number, WorkAsk> {
  const places = new Map<number, WorkAsk>();
  for (const ask of card.asks) {
    if (ask.thread && ask.thread.chat_id === chatId && !places.has(ask.thread.root_id)) places.set(ask.thread.root_id, ask);
  }
  return places;
}

/**
 * The main-line messages an exception of the Orchestrator sits under, by message id: the first message of the thread
 * where its run wrote (the hub's message that opened the thread), else the first message of its run on the main line.
 * An exception whose run wrote nothing the feed holds sits only in the card.
 */
export function exceptionPlaces(card: Pick<WorkCard, "decisions">, messages: Iterable<Message>): Map<number, WorkDecision[]> {
  const byRun = new Map<number, Message>();
  for (const message of messages) {
    if (message.run_id === null) continue;
    const held = byRun.get(message.run_id);
    if (!held || message.seq < held.seq) byRun.set(message.run_id, message);
  }
  const places = new Map<number, WorkDecision[]>();
  for (const decision of card.decisions) {
    if (decision.kind !== "orchestrator" || decision.run_id === null) continue;
    const message = byRun.get(decision.run_id);
    if (!message) continue;
    const root = message.thread_root_id ?? message.id;
    places.set(root, [...(places.get(root) ?? []), decision]);
  }
  return places;
}

/** The latest reply of the agent asked in its thread: what the ask card says of the answer; null before it answers. */
export function askAnswer(ask: Pick<WorkAsk, "agent" | "thread">, messages: Iterable<Message>): Message | null {
  if (!ask.thread || !ask.agent) return null;
  let last: Message | null = null;
  for (const message of messages) {
    if (message.thread_root_id !== ask.thread.root_id || message.author.kind !== "agent" || message.author.handle !== ask.agent) continue;
    if (!last || message.seq > last.seq) last = message;
  }
  return last;
}

/** The card's link to a sub-task's thread: in this chat, the thread opens beside it; in another, its page with the thread. */
export function threadHref(thread: NonNullable<WorkAsk["thread"]>): Route {
  return `/chats/${thread.chat_id}?thread=${thread.root_id}` as Route;
}
