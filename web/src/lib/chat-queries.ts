import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { chatKeys } from "@/lib/chat-keys";
import type { Chat, ChatCandidate, ChatCreate, ChatEvents, ChatPage, GroupMessage } from "@/lib/chats";
import type { ApiSource } from "@/lib/queries";

export { chatComposeHref, chatKeys, chatQuery, chatsQuery, COMPOSE_PARAM, LIST_IDLE_MS, LIST_LIVE_MS, openDirectChat } from "@/lib/chat-keys";

/**
 * The reads and writes of group chats (GET, POST /v1/chats and below). Only a chat's members read it and write to it;
 * each reads the messages whose label their grants clear. The list (with each chat's unread count) is read by the
 * Chats pages and by the sidebar's count (`chat-keys.ts`), so they share one cache entry; a chat's page of messages is
 * read once, and its stream (`chatStreamPath`) keeps it current after that.
 */

/** Messages one page holds (`chats.PAGE`), and a thread at most (`chats.MAX_PAGE`). */
export const PAGE_SIZE = 50;
export const THREAD_SIZE = 200;

/** The chat and its latest messages, oldest first: what its page opens with; its stream takes over from there. */
export const chatPageQuery = (api: ApiSource, id: number) =>
  queryOptions({
    queryKey: chatKeys.page(id),
    queryFn: ({ signal }): Promise<ChatPage> =>
      call(api().GET("/v1/chats/{chat_id}/messages", { params: { path: { chat_id: id }, query: { limit: PAGE_SIZE } }, signal })),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

/**
 * The people the visitor may add to a chat (those they share a project or a chat with), by login, each with why they
 * could not be in it with the agents `agents` (handles). The list a change of the agents asks for again keeps showing
 * the last one until it comes.
 */
export const chatPeopleQuery = (api: ApiSource, agents: readonly string[]) => {
  const handles = [...new Set(agents)].sort();
  return queryOptions({
    queryKey: chatKeys.people(handles),
    queryFn: ({ signal }): Promise<ChatCandidate[]> => call(api().GET("/v1/chats/people", { params: { query: { agents: handles } }, signal })),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
};

/** The messages before `before` (a seq), oldest first. */
export function olderMessages(api: ApiClient, id: number, before: number, signal?: AbortSignal): Promise<ChatPage> {
  return call(api.GET("/v1/chats/{chat_id}/messages", { params: { path: { chat_id: id }, query: { before, limit: PAGE_SIZE } }, signal }));
}

/** A thread: its first message and the replies, oldest first. */
export function threadMessages(api: ApiClient, id: number, root: number, signal?: AbortSignal): Promise<ChatPage> {
  return call(api.GET("/v1/chats/{chat_id}/messages", { params: { path: { chat_id: id }, query: { thread: root, limit: THREAD_SIZE } }, signal }));
}

/**
 * The chat's events after `after`, oldest first, with the progress of the agents' runs under the messages they took:
 * what the page reads while its stream cannot be used.
 */
export function chatEventsAfter(api: ApiClient, id: number, after: number, signal?: AbortSignal): Promise<ChatEvents> {
  return call(api.GET("/v1/chats/{chat_id}/events", { params: { path: { chat_id: id }, query: { after, progress: true } }, signal }));
}

/**
 * The chat's server-sent events, from the event after `after` on a first connection, and its `progress` events: the
 * progress of the agents' runs under the messages they took, at once and each time it changed.
 */
export function chatStreamPath(id: number, after: number): string {
  return `/v1/chats/${id}/stream?after=${after}&progress=true`;
}

// Writes, each with the session's X-Evo-CSRF header.

export async function createChat(api: ApiClient, body: ChatCreate): Promise<Chat> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/chats", { body, headers }));
}

/** Set a group chat's image to one given as base64 (PNG, JPEG or WebP, at most 512 KiB). */
export async function setChatImage(api: ApiClient, id: number, data: string): Promise<Chat> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/chats/{chat_id}/image", { params: { path: { chat_id: id } }, body: { data }, headers }));
}

/** Take a group chat's image off: its face is its members' again. */
export async function removeChatImage(api: ApiClient, id: number): Promise<Chat> {
  const headers = await csrfHeaders(api);
  return call(api.DELETE("/v1/chats/{chat_id}/image", { params: { path: { chat_id: id } }, headers }));
}

/** Send a message, in the thread of `replyTo` when given. */
export async function sendChatMessage(api: ApiClient, id: number, body: string, replyTo: number | null, files: number[] = []): Promise<GroupMessage> {
  const headers = await csrfHeaders(api);
  const payload = { body, ...(replyTo ? { reply_to: replyTo } : {}), ...(files.length ? { attachment_ids: files } : {}) };
  return call(api.POST("/v1/chats/{chat_id}/messages", { params: { path: { chat_id: id } }, body: payload, headers }));
}

/** Put the visitor's emoji on a message, or take it off. */
export async function toggleReaction(api: ApiClient, id: number, message: number, emoji: string, on: boolean): Promise<GroupMessage> {
  const headers = await csrfHeaders(api);
  const path = { chat_id: id, message_id: message };
  if (on) return call(api.POST("/v1/chats/{chat_id}/messages/{message_id}/reactions", { params: { path }, body: { emoji }, headers }));
  return call(api.DELETE("/v1/chats/{chat_id}/messages/{message_id}/reactions", { params: { path, query: { emoji } }, headers }));
}

/** Say the visitor read the chat up to event `seq`: the list's unread count starts after it. */
export async function markChatRead(api: ApiClient, id: number, seq: number): Promise<{ last_read_seq: number; unread: number }> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/chats/{chat_id}/read", { params: { path: { chat_id: id } }, body: { seq }, headers }));
}

export async function addChatPerson(api: ApiClient, id: number, login: string): Promise<Chat> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/chats/{chat_id}/people/{login}", { params: { path: { chat_id: id, login } }, headers }));
}

/** Leave the chat (the visitor's own login), or, for its creator, take a person out of it. */
export async function removeChatPerson(api: ApiClient, id: number, login: string): Promise<void> {
  const headers = await csrfHeaders(api);
  await call(api.DELETE("/v1/chats/{chat_id}/people/{login}", { params: { path: { chat_id: id, login } }, headers }));
}

export async function addChatAgent(api: ApiClient, id: number, handle: string): Promise<Chat> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/chats/{chat_id}/agents/{handle}", { params: { path: { chat_id: id, handle } }, headers }));
}

export async function removeChatAgent(api: ApiClient, id: number, handle: string): Promise<void> {
  const headers = await csrfHeaders(api);
  await call(api.DELETE("/v1/chats/{chat_id}/agents/{handle}", { params: { path: { chat_id: id, handle } }, headers }));
}
