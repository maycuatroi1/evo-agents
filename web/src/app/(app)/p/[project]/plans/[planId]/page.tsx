import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { PlanOverview } from "@/components/plans/plan-overview";
import { planActiveRunsQuery, runsQuery } from "@/components/runs/queries";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { planQuery } from "@/lib/plan-queries";
import { PLAN_ID } from "@/lib/plans";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string; planId: string }> };

async function names(params: Props["params"]) {
  const { project, planId } = await params;
  return { project: decodeURIComponent(project), planId: decodeURIComponent(planId) };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("plans");
  return { title: t("overview.metaTitle", await names(params)) };
}

/**
 * One plan: its progress, its steps by status, its repos and the rest of what it holds, read-only, with its active plan
 * run (prefetched with the plan, so the banner renders with the page) and Run plan for a writer.
 */
export default async function PlanPage({ params }: Props) {
  const { project, planId } = await names(params);
  if (!PROJECT_NAME.test(project) || !PLAN_ID.test(planId)) {
    return <PlanOverview project={project} planId={planId} initialError={null} invalid />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, planQuery(() => api, project, planId)),
    prefetch(client, runsQuery(() => api, project, planActiveRunsQuery(planId))),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <PlanOverview project={project} planId={planId} initialError={error} />
    </HydrationBoundary>
  );
}
