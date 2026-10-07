import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { fromRecord } from "@/components/admin/data";
import { listQuery, readFilters } from "@/components/runs/model";
import { runsQuery, runsSummaryQuery } from "@/components/runs/queries";
import { RunsPage } from "@/components/runs/runs-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME } from "@/lib/queries";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("runs");
  return { title: t("metaTitle", { project: decodeURIComponent((await params).project) }) };
}

/**
 * The runs of a project, for any member with a grant on it: the summary and the page of the list the URL's filters
 * name, both read on the server so the page renders complete, then refreshed in the browser.
 */
export default async function ProjectRunsPage({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const filters = readFilters(fromRecord(await searchParams));
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, runsSummaryQuery(() => api, project)),
    prefetch(client, runsQuery(() => api, project, listQuery(filters))),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <RunsPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
