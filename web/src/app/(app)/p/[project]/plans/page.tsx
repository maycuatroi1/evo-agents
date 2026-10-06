import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { PlansList } from "@/components/plans/plans-list";
import { runsSummaryQuery } from "@/components/runs/queries";
import { NotFoundState } from "@/components/states/states";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { plansQuery } from "@/lib/plan-queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("plans");
  return { title: t("list.metaTitle", { project: decodeURIComponent((await params).project) }) };
}

/** The plans of a project, active and completed, with their progress and active plan runs. Read-only. */
export default async function PlansPage({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) return <NotFoundState />;
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, plansQuery(() => api, project)),
    prefetch(client, runsSummaryQuery(() => api, project)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <PlansList project={project} initialError={error} />
    </HydrationBoundary>
  );
}
