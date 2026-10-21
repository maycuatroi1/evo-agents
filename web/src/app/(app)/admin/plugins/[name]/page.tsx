import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { AdminPluginPage } from "@/components/plugins/admin-plugin-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PLUGIN_NAME } from "@/lib/plugin-queries";
import { pluginQuery } from "@/components/plugins/queries";

type Props = { params: Promise<{ name: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("plugins.admin");
  return { title: `${decodeURIComponent((await params).name)} | ${t("title")}` };
}

/** One plugin of the store, for hub admins: its versions, its backend and the hub's layer of its config. */
export default async function PluginPage({ params }: Props) {
  const name = decodeURIComponent((await params).name);
  if (!PLUGIN_NAME.test(name)) {
    const t = await getTranslations("plugins.page");
    return <NotFoundState title={t("missingTitle")} description={t("missingDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, pluginQuery(() => api, name));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AdminPluginPage name={name} initialError={error} />
    </HydrationBoundary>
  );
}
