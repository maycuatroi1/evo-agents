import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { InsightsPage } from "@/components/insights/insights-page";
import { readRange, runStatsQuery } from "@/components/insights/queries";
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
  const t = await getTranslations("insights");
  return { title: t("metaTitle", { project: decodeURIComponent((await params).project) }) };
}

/**
 * A project's Insights, for any member with a grant on it: the runs by day over the range the URL names (`?days=7`,
 * 30 without it, or 90), read on the server so the page renders its figures and tables complete; the charts follow
 * in the browser.
 */
export default async function ProjectInsightsPage({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const range = readRange((await searchParams).days);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, runStatsQuery(() => api, project, range));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <InsightsPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
