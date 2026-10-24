import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { KgExplorer } from "@/components/kg/kg-explorer";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { cleanQuery, kgBuildsQuery, kgGraphQuery, kgSearchQuery, parseBuildFilter } from "@/lib/kg/queries";
import { buildsHref } from "@/lib/kg/routes";
import { PROJECT_NAME } from "@/lib/queries";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("kg");
  return { title: `${t("title")}: ${decodeURIComponent((await params).project)}` };
}

/**
 * /p/{project}/kg?cluster=&node=&q=&kind=: the explorer, with the builds, the graph's summary and the search the URL
 * names prefetched with the visitor's cookie; the clusters, a cluster and a neighbourhood are read in the browser, so
 * the page shows before the graph is drawn. The builds' filter lived here before the builds had a page of their own:
 * `?builds=` goes there.
 */
export default async function KgPage({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  const search = await searchParams;
  if (search.builds !== undefined) redirect(buildsHref(project, parseBuildFilter(search.builds) === "failed"));
  const query = cleanQuery(search.q);
  const kind = cleanQuery(search.kind);
  const api = await serverApi();
  const client = getQueryClient();
  const [builds, graph, found] = await Promise.all([
    prefetch(client, kgBuildsQuery(() => api, project)),
    prefetch(client, kgGraphQuery(() => api, project)),
    query ? prefetch(client, kgSearchQuery(() => api, project, query, kind)) : Promise.resolve(null),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <KgExplorer project={project} errors={{ builds, graph, search: found }} />
    </HydrationBoundary>
  );
}
