import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { MarketPluginPage } from "@/components/market/plugin-page";
import { marketPluginQuery } from "@/components/market/queries";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PLUGIN_NAME } from "@/lib/plugin-queries";

type Props = { params: Promise<{ source: string; plugin: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("market");
  return { title: `${decodeURIComponent((await params).plugin)} | ${t("title")}` };
}

/** One plugin of one market, for every member; a hub admin installs it from here. */
export default async function MarketPluginRoute({ params }: Props) {
  const found = await params;
  const plugin = decodeURIComponent(found.plugin);
  const source = Number(found.source);
  if (!Number.isInteger(source) || source < 1 || !PLUGIN_NAME.test(plugin)) {
    const t = await getTranslations("market.detail");
    return <NotFoundState title={t("missingTitle")} description={t("missingDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, marketPluginQuery(() => api, source, plugin));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <MarketPluginPage source={source} plugin={plugin} initialError={error} />
    </HydrationBoundary>
  );
}
