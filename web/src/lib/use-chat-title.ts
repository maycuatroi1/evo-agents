"use client";

import { useQuery } from "@tanstack/react-query";

import { browserApi } from "@/lib/api/browser";
import { chatQuery } from "@/lib/chat-keys";

/**
 * The title of the chat a path names (`/chats/{id}`), as the page read it, for the breadcrumb; null while unknown. It
 * asks the hub nothing: the chat's page seeds what it reads.
 */
export function useChatTitle(part: string | undefined): string | null {
  const id = part && /^[1-9][0-9]{0,17}$/.test(part) ? Number(part) : null;
  const { data } = useQuery({ ...chatQuery(browserApi, id ?? 0), enabled: false });
  return id !== null && data ? data.title : null;
}
