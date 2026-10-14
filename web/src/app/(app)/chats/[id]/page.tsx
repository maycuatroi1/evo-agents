import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { ChatView } from "@/components/chats/chat-view";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { chatKeys, chatPageQuery } from "@/lib/chat-queries";
import type { ChatPage } from "@/lib/chats";

type Props = { params: Promise<{ id: string }> };

const CHAT_ID = /^[1-9][0-9]{0,17}$/;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("chats");
  return { title: t("chatMetaTitle", { id: (await params).id }) };
}

/**
 * One chat, for its members: its latest messages read on the server, and the chat itself seeded from the same answer
 * so its head and members render complete. Anyone else gets the API's 403 or 404 as the page's state.
 */
export default async function ChatRoute({ params }: Props) {
  const raw = (await params).id;
  if (!CHAT_ID.test(raw)) {
    const t = await getTranslations("chats.view");
    return <NotFoundState title={t("notFoundTitle")} description={t("notFoundBody")} />;
  }
  const id = Number(raw);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, chatPageQuery(() => api, id));
  const page = client.getQueryData<ChatPage>(chatKeys.page(id));
  if (page) client.setQueryData(chatKeys.one(id), page.chat);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ChatView chatId={id} initialError={error} />
    </HydrationBoundary>
  );
}
