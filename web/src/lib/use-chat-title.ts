"use client";

import { useQuery } from "@tanstack/react-query";

import { browserApi } from "@/lib/api/browser";
import { chatQuery } from "@/lib/chat-keys";
import { workCardQuery } from "@/lib/work";

/**
 * The title of the chat a path names (`/chats/{id}`), as the page read it, for the breadcrumb; null while unknown. A
 * work chat (kind `work`) goes by its work's title once the page read its card, as its head and the chat list name it.
 * It asks the hub nothing: the chat's page seeds what it reads.
 */
export function useChatTitle(part: string | undefined): string | null {
  const id = part && /^[1-9][0-9]{0,17}$/.test(part) ? Number(part) : null;
  const { data } = useQuery({ ...chatQuery(browserApi, id ?? 0), enabled: false });
  const { data: card } = useQuery({ ...workCardQuery(browserApi, id ?? 0), enabled: false });
  if (id === null || !data) return null;
  return data.kind === "work" && card ? card.title : data.title;
}
