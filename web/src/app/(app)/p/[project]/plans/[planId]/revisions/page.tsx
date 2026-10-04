import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { PlanHistory } from "@/components/plans/plan-history";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { planDiffQuery, planQuery, planRevisionsQuery } from "@/lib/plan-queries";
import { comparedPair, PLAN_ID, type PlanRevision, revisionParam } from "@/lib/plans";
import { PROJECT_NAME } from "@/lib/queries";

type Props = {
  params: Promise<{ project: string; planId: string }>;
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
};

async function names(params: Props["params"]) {
  const { project, planId } = await params;
  return { project: decodeURIComponent(project), planId: decodeURIComponent(planId) };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("plans");
  return { title: t("history.metaTitle", await names(params)) };
}

/**
 * The revisions of a plan (actor, time, summary) and the diff between two of them: ?from=&to= when both are
 * revisions the visitor sees, else the latest against the one before it. Read-only.
 */
export default async function PlanRevisionsPage({ params, searchParams }: Props) {
  const { project, planId } = await names(params);
  const query = await searchParams;
  const from = revisionParam(query.from);
  const to = revisionParam(query.to);
  const empty = { plan: null, revisions: null };
  if (!PROJECT_NAME.test(project) || !PLAN_ID.test(planId)) {
    return <PlanHistory project={project} planId={planId} from={from} to={to} initialErrors={empty} invalid />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const [plan, revisions] = await Promise.all([
    prefetch(client, planQuery(() => api, project, planId)),
    prefetch(client, planRevisionsQuery(() => api, project, planId)),
  ]);
  const held = client.getQueryData<PlanRevision[]>(planRevisionsQuery(() => api, project, planId).queryKey);
  const pair = held ? comparedPair(held, from, to) : null;
  if (pair && pair.from !== pair.to) {
    // A failed diff is not kept: the browser asks again and shows the failure where the diff goes.
    await prefetch(client, planDiffQuery(() => api, project, planId, pair.from, pair.to));
  }
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <PlanHistory project={project} planId={planId} from={from} to={to} initialErrors={{ plan, revisions }} />
    </HydrationBoundary>
  );
}
