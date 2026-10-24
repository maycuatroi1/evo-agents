import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { OpportunityPage } from "@/components/loop/opportunity-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { loopItemsQuery } from "@/lib/loop-queries";
import { OPPORTUNITY_REF, opportunityEvidenceQuery, opportunityQuery, opportunityRevisionsQuery } from "@/lib/opportunity-queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string; ref: string }> };

async function names(params: Props["params"]) {
  const { project, ref } = await params;
  return { project: decodeURIComponent(project), ref: decodeURIComponent(ref) };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("opportunities.page");
  const { project, ref } = await names(params);
  return { title: t("meta", { project, code: ref }) };
}

/** One opportunity of a project, with its revisions and its evidence and the Loop's work items that go from it, read on the server. */
export default async function ProjectOpportunityPage({ params }: Props) {
  const { project, ref } = await names(params);
  const t = await getTranslations("states");
  const opportunity = await getTranslations("opportunities.page");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  if (!OPPORTUNITY_REF.test(ref)) return <NotFoundState title={opportunity("notFoundTitle", { code: ref })} description={opportunity("notFoundText")} />;
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, opportunityQuery(() => api, project, ref)),
    prefetch(client, opportunityRevisionsQuery(() => api, project, ref)),
    prefetch(client, opportunityEvidenceQuery(() => api, project, ref)),
    prefetch(client, loopItemsQuery(() => api, project, { state: null, kind: null, goal: null, open: false, opportunity: ref })),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <OpportunityPage project={project} code={ref} initialError={error} />
    </HydrationBoundary>
  );
}
