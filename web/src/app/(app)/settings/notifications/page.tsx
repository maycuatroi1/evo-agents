import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { NotificationsPage } from "@/components/notifications/notifications-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { notificationSettingsQuery, pushConfigQuery, pushDevicesQuery } from "@/lib/notification-queries";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("notifications");
  return { title: t("title") };
}

/**
 * The visitor's own notification settings, their push devices and the hub's push configuration, read on the server
 * so the page renders complete. A failed read of the devices or the configuration is not the page's failure: its card
 * asks again on its own.
 */
export default async function NotificationsRoute() {
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, notificationSettingsQuery(() => api)),
    prefetch(client, pushConfigQuery(() => api)),
    prefetch(client, pushDevicesQuery(() => api)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <NotificationsPage initialError={error} />
    </HydrationBoundary>
  );
}
