import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { StepDetail } from "@/components/plans/step-detail";
import { readyStepsQuery, runsQuery, stepRunsQuery } from "@/components/runs/queries";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { planQuery } from "@/lib/plan-queries";
import { PLAN_ID } from "@/lib/plans";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string; planId: string; stepKey: string }> };

async function names(params: Props["params"]) {
  const { project, planId, stepKey } = await params;
  return {
    project: decodeURIComponent(project),
    planId: decodeURIComponent(planId),
    step: decodeURIComponent(stepKey),
  };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("plans");
  return { title: t("step.metaTitle", await names(params)) };
}

/**
 * One step of a plan: what, verify, note, evidence and done_at, the steps around it, and the step's runs with the Run
 * this step button for a writer. The plan itself is read-only on the web.
 */
export default async function StepPage({ params }: Props) {
  const { project, planId, step } = await names(params);
  if (!PROJECT_NAME.test(project) || !PLAN_ID.test(planId)) {
    return <StepDetail project={project} planId={planId} stepKey={step} initialError={null} invalid />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, planQuery(() => api, project, planId)),
    prefetch(client, readyStepsQuery(() => api, project, planId)),
    prefetch(client, runsQuery(() => api, project, stepRunsQuery(planId, step))),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <StepDetail project={project} planId={planId} stepKey={step} initialError={error} />
    </HydrationBoundary>
  );
}
