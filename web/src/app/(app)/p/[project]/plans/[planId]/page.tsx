import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { PlanOverview } from "@/components/plans/plan-overview";
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

/** One plan: its progress, its steps by status, its repos and the rest of what it holds. Read-only. */
export default async function PlanPage({ params }: Props) {
  const { project, planId } = await names(params);
  if (!PROJECT_NAME.test(project) || !PLAN_ID.test(planId)) {
    return <PlanOverview project={project} planId={planId} initialError={null} invalid />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, planQuery(() => api, project, planId));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <PlanOverview project={project} planId={planId} initialError={error} />
    </HydrationBoundary>
  );
}
