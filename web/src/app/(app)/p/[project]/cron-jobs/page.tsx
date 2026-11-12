import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { CronJobsPage } from "@/components/cron/cron-jobs-page";
import { cronJobsQuery } from "@/components/cron/queries";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("cron.list");
  return { title: t("meta", { project: decodeURIComponent((await params).project) }) };
}

/** A project's cron jobs, read on the server. */
export default async function ProjectCronJobsPage({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, cronJobsQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <CronJobsPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
