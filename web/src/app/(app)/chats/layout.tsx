import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";
import type { ReactNode } from "react";

import { ChatsShell } from "@/components/chats/chats-shell";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { chatsQuery } from "@/lib/chat-queries";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("chats");
  return { title: t("metaTitle") };
}

/**
 * The Chats area: the visitor's chats, read on the server so the list renders complete, beside the page shown (no
 * chat, or one). A layout, so the list stays mounted while the visitor moves from chat to chat.
 */
export default async function ChatsLayout({ children }: { children: ReactNode }) {
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, chatsQuery(() => api));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ChatsShell initialError={error}>{children}</ChatsShell>
    </HydrationBoundary>
  );
}
