import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

type Chat = components["schemas"]["Chat"];
type ChatSummary = components["schemas"]["ChatSummary"];

/**
 * What other pages need of group chats (the sidebar's unread count, the breadcrumb's chat title, a chat run's link
 * back to its chat, the Agents pages' Chat button), apart from `chat-queries.ts` and `chats.ts`, so that those pages do
 * not load the Chats pages' code.
 */
export const chatKeys = {
  all: ["chats"] as const,
  list: ["chats", "list"] as const,
  one: (id: number) => ["chats", "one", id] as const,
  page: (id: number) => ["chats", "one", id, "page"] as const,
  people: (agents: readonly string[]) => ["chats", "people", ...agents] as const,
};

/** How often the list is read while a Chats page shows it, and while only the sidebar counts its unread messages. */
export const LIST_LIVE_MS = 10_000;
export const LIST_IDLE_MS = 30_000;
/** The visitor's chats, the latest active first, each with the messages of others they have not read. */
export const chatsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: chatKeys.list,
    queryFn: ({ signal }): Promise<ChatSummary[]> => call(api().GET("/v1/chats", { signal })),
  });

/** The chat with its people and agents. */
export const chatQuery = (api: ApiSource, id: number) =>
  queryOptions({
    queryKey: chatKeys.one(id),
    queryFn: ({ signal }): Promise<Chat> => call(api().GET("/v1/chats/{chat_id}", { params: { path: { chat_id: id } }, signal })),
  });


export function chatsHref(): Route {
  return "/chats" as Route;
}

/** A chat's page; at one message when given, as a notice links to it (`chats.chat_link`). */
export function chatHref(id: number, message?: number | null): Route {
  return (message ? `/chats/${id}?message=${message}` : `/chats/${id}`) as Route;
}

/** The query parameter that opens a chat with its composer focused, then leaves the address. */
export const COMPOSE_PARAM = "compose";

/** A chat's page with its composer focused: where an agent's avatar or its Chat button leads. */
export function chatComposeHref(id: number): Route {
  return `/chats/${id}?${COMPOSE_PARAM}=1` as Route;
}

/** The visitor's direct chat with agent `handle`: the one there is, or one made now (PUT /v1/chats/direct/{handle}). */
export async function openDirectChat(api: ApiClient, handle: string): Promise<Chat> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/chats/direct/{handle}", { params: { path: { handle } }, headers }));
}
