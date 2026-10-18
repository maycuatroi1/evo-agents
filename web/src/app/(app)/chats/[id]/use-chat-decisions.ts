"use client";

import { useQueries } from "@tanstack/react-query";
import { useCallback, useMemo, useState } from "react";

import type { Decision } from "@/components/inbox/queries";
import { runDecisionsQuery } from "@/components/runs/run-decisions";
import { CHAT_REFRESH_MS } from "@/components/runs/queries";
import { browserApi } from "@/lib/api/browser";
import type { GroupMessage } from "@/lib/chats";

import type { Feed } from "@/components/chats/feed-model";
import type { MessageSlot } from "@/components/chats/message-item";
import { createElement } from "react";

import { MessageDecisions } from "./message-decisions";

/** The runs whose decisions a chat reads: the latest ones, since an older run's question was answered or expired. */
const MAX_RUNS = 12;
/** Of those, how many are read again every few seconds while the chat has not ended. */
const LIVE_RUNS = 3;

export type ChatDecisions = {
  /** The decisions the agent of this message asked in its run, when it is the message that ended the run's turn. */
  of: (message: GroupMessage) => Decision[];
  /** A decision as an answer here (or a 409) left it, until the list says it is no longer open. */
  keep: (decision: Decision) => void;
};

/** The agent message of each run that comes last: the one a decision of the run is shown under. */
export function lastOfRun(messages: Iterable<GroupMessage>): Map<number, GroupMessage> {
  const last = new Map<number, GroupMessage>();
  for (const message of messages) {
    if (message.author.kind !== "agent" || !message.author.project || message.run_id === null) continue;
    const held = last.get(message.run_id);
    if (!held || held.seq < message.seq) last.set(message.run_id, message);
  }
  return last;
}

/**
 * The decisions of the agents' runs in a chat (docs/workers.md, Failure triage): a chat run that asks ends its turn with
 * the decision open, and the card shows right under the agent's last message of that run, in the chat or in its thread,
 * so one click answers it. Read for the latest runs only; the newest are read again while the chat has not ended.
 */
export function useChatDecisions(feed: Feed, ended: boolean): MessageSlot {
  const last = useMemo(() => lastOfRun([...feed.entries.values()].map((entry) => entry.message)), [feed.entries]);
  const runs = useMemo(
    () =>
      [...last.values()]
        .sort((a, b) => b.seq - a.seq)
        .slice(0, MAX_RUNS)
        .map((message, index) => ({ run: message.run_id as number, project: message.author.project as string, live: index < LIVE_RUNS })),
    [last],
  );
  const lists = useQueries({
    queries: runs.map(({ run, project, live }) => ({ ...runDecisionsQuery(browserApi, project, run), refetchInterval: live && !ended ? CHAT_REFRESH_MS : false })),
  });
  const [kept, setKept] = useState<ReadonlyMap<number, Decision>>(new Map());
  const keep = useCallback((decision: Decision) => setKept((map) => new Map(map).set(decision.id, decision)), []);
  const byRun = new Map<number, Decision[]>();
  lists.forEach((query, index) => {
    const decisions = (query.data?.decisions ?? []).map((decision) => (decision.state === "open" ? (kept.get(decision.id) ?? decision) : decision));
    if (decisions.length) byRun.set(runs[index].run, decisions.sort((a, b) => a.id - b.id));
  });
  const of = (message: GroupMessage) => (message.run_id !== null && last.get(message.run_id)?.id === message.id ? (byRun.get(message.run_id) ?? []) : []);
  return function decisionsUnder(message) {
    return createElement(MessageDecisions, { decisions: of(message), onSettled: keep });
  };
}
