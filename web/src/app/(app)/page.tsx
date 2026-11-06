import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { chatAgentsQuery } from "@/lib/chat-keys";
import { featureOn, featuresQuery } from "@/lib/hub-settings";

import { HomeChat } from "./home-chat";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("chatHome");
  return { title: t("title") };
}

/**
 * Home: the chat home (docs/hub.md, Chat home), a chat with one of the agents the signed-in member may give work to.
 * The agents are read on the server (GET /v1/me/chat-agents) so the page renders with the agent chosen; the chat's
 * messages load in the browser. While a hub admin has chats off, the page says so instead (and reads no agent). The
 * overview across projects that Home used to be is Today, at /today.
 */
export default async function HomeRoute() {
  const api = await serverApi();
  const client = getQueryClient();
  let chatsOn = true;
  try {
    chatsOn = featureOn(await client.fetchQuery(featuresQuery(() => api)), "chats");
  } catch {
    // The features cannot be read: the page goes on, and the agents' read answers for itself.
  }
  const error = chatsOn ? await prefetch(client, chatAgentsQuery(() => api)) : null;
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <HomeChat initialError={error} chatsOn={chatsOn} />
    </HydrationBoundary>
  );
}
