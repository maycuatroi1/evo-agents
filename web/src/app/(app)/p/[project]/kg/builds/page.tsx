import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { KgBuildsPage } from "@/components/kg/kg-builds";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { kgBuildsQuery, kgFailedBuildsQuery, kgGraphQuery, parseBuildFilter } from "@/lib/kg/queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("kg");
  return { title: `${t("tabs.builds")}: ${t("title")}: ${decodeURIComponent((await params).project)}` };
}

/**
 * /p/{project}/kg/builds?builds=: the graph's status, the commits its sources read, its shape and the build history
 * (`builds=failed` for the failed builds only), prefetched with the visitor's cookie.
 */
export default async function KgBuildsRoute({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  const failedOnly = parseBuildFilter((await searchParams).builds) === "failed";
  const api = await serverApi();
  const client = getQueryClient();
  const [builds, graph, failed] = await Promise.all([
    prefetch(client, kgBuildsQuery(() => api, project)),
    prefetch(client, kgGraphQuery(() => api, project)),
    failedOnly ? prefetch(client, kgFailedBuildsQuery(() => api, project)) : Promise.resolve(null),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <KgBuildsPage project={project} errors={{ builds, graph, failed }} />
    </HydrationBoundary>
  );
}
