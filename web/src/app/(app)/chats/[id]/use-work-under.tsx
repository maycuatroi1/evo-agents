"use client";

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useRef } from "react";

import type { Feed } from "@/components/chats/feed-model";
import type { MessageSlot } from "@/components/chats/message-item";
import { WorkAskCard, WorkExceptionCard } from "@/components/chats/work-ask";
import type { Chat } from "@/lib/chats";
import { askAnswer, askPlaces, exceptionPlaces, type WorkCard, workKeys } from "@/lib/work";

import { useChatDecisions } from "./use-chat-decisions";

/** The work chat shown and its card, for what the chat puts under its messages; null in any other chat. */
export const WorkChatContext = createContext<{ chat: Pick<Chat, "id" | "agents" | "people">; card: WorkCard | null } | null>(null);

/** A change in the chat reads the card again this long after the last one, so a burst of events reads it once. */
const REREAD_MS = 800;

/**
 * Under a work chat's messages (web/DESIGN.md, Work card): the decisions of the agents' runs as in any chat, the card of
 * the sub-task a main-line question starts, and the exception of the Orchestrator its thread is about. Each event of the
 * chat reads the work card again, shortly after, so the card, the asks and the exceptions follow the chat.
 */
export function useWorkUnder(feed: Feed, ended: boolean): MessageSlot {
  const decisionsUnder = useChatDecisions(feed, ended);
  const work = useContext(WorkChatContext);
  const queryClient = useQueryClient();
  const chatId = work?.chat.id ?? null;
  const seen = useRef(feed.lastSeq);
  useEffect(() => {
    if (chatId === null || feed.lastSeq === seen.current) return;
    seen.current = feed.lastSeq;
    const timer = setTimeout(() => void queryClient.invalidateQueries({ queryKey: workKeys.card(chatId) }), REREAD_MS);
    return () => clearTimeout(timer);
  }, [chatId, feed.lastSeq, queryClient]);

  const card = work?.card ?? null;
  const messages = useMemo(() => [...feed.entries.values()].map((entry) => entry.message), [feed.entries]);
  const places = useMemo(
    () => (card && chatId !== null ? { asks: askPlaces(card, chatId), exceptions: exceptionPlaces(card, messages) } : null),
    [card, chatId, messages],
  );
  return function workUnder(message) {
    const decisions = decisionsUnder(message);
    if (!places || !work || message.thread_root_id !== null) return decisions;
    const ask = places.asks.get(message.id);
    const exceptions = places.exceptions.get(message.id) ?? [];
    if (!ask && exceptions.length === 0) return decisions;
    return (
      <>
        {decisions}
        {ask ? <WorkAskCard ask={ask} answer={askAnswer(ask, messages)} chat={work.chat} /> : null}
        {exceptions.map((decision) => (
          <WorkExceptionCard key={decision.id} decision={decision} />
        ))}
      </>
    );
  };
}
