import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { KgOverview } from "@/components/kg/kg-overview";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { cleanQuery, kgBuildsQuery, kgFailedBuildsQuery, kgGraphQuery, kgSearchQuery, parseBuildFilter } from "@/lib/kg/queries";
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
 * /p/{project}/kg?q=&kind=&builds=: build status, queue, node search and the build history (`builds=failed` for the
 * failed builds only), prefetched with the visitor's cookie.
 */
export default async function KgPage({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  const search = await searchParams;
  const query = cleanQuery(search.q);
  const kind = cleanQuery(search.kind);
  const failedOnly = parseBuildFilter(search.builds) === "failed";
  const api = await serverApi();
  const client = getQueryClient();
  const [builds, graph, found, failed] = await Promise.all([
    prefetch(client, kgBuildsQuery(() => api, project)),
    prefetch(client, kgGraphQuery(() => api, project)),
    query ? prefetch(client, kgSearchQuery(() => api, project, query, kind)) : Promise.resolve(null),
    failedOnly ? prefetch(client, kgFailedBuildsQuery(() => api, project)) : Promise.resolve(null),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <KgOverview project={project} query={query} kind={kind} errors={{ builds, graph, search: found, failed }} />
    </HydrationBoundary>
  );
}
