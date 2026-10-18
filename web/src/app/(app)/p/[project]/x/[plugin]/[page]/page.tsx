import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { PluginPageView } from "@/components/plugins/plugin-page-view";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PLUGIN_NAME, PAGE_NAME, projectPluginQuery } from "@/lib/plugin-queries";
import { requirePlugin } from "@/lib/plugins-server";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string; plugin: string; page: string }> };

async function names(params: Props["params"]) {
  const { project, plugin, page } = await params;
  return { project: decodeURIComponent(project), plugin: decodeURIComponent(plugin), page: decodeURIComponent(page) };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { project, plugin, page } = await names(params);
  const t = await getTranslations("plugins.page");
  return { title: t("metaTitle", { page, plugin, project }) };
}

/**
 * A page an isolated plugin adds to a project, framed in a sandbox (web/DESIGN.md, Plugins). A 404 where the project
 * has not enabled the plugin or the plugin has no such page, as the plugin's routes answer; a core plugin's native
 * page has a route of its own under x/<plugin>, which wins over this one, and this web carries each of them.
 */
export default async function PluginPageRoute({ params }: Props) {
  const { project, plugin, page } = await names(params);
  if (!PROJECT_NAME.test(project) || !PLUGIN_NAME.test(plugin) || !PAGE_NAME.test(page)) notFound();
  const found = await requirePlugin(plugin, project, page);
  if (found?.pages.find((item) => item.name === page)?.native) notFound(); // a core plugin's page this web lacks
  const api = await serverApi();
  const client = getQueryClient();
  const error = found ? null : await prefetch(client, projectPluginQuery(() => api, project, plugin));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <PluginPageView project={project} plugin={plugin} page={page} initialError={error} />
    </HydrationBoundary>
  );
}
