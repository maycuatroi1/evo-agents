import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { fromRecord } from "@/components/admin/data";
import { InboxPage } from "@/components/inbox/inbox-page";
import { inboxQuery, readInboxFilters } from "@/components/inbox/model";
import { notificationCountQuery, notificationsQuery } from "@/components/inbox/queries";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

type Props = { searchParams: Promise<Record<string, string | string[] | undefined>> };

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("inbox");
  return { title: t("title") };
}

/**
 * The signed-in member's Inbox: the page of notifications the URL's filters name and the unread count, read on the
 * server so the page renders complete, then refreshed in the browser every 10 seconds. A decision the URL opens
 * (`?decision=ID`, the link of every decision notification) is read in the browser beside the list.
 */
export default async function InboxRoute({ searchParams }: Props) {
  const filters = readInboxFilters(fromRecord(await searchParams));
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, notificationsQuery(() => api, inboxQuery(filters))),
    prefetch(client, notificationCountQuery(() => api)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <InboxPage initialError={error} />
    </HydrationBoundary>
  );
}
