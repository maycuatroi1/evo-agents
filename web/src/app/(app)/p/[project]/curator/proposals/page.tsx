import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { fromRecord } from "@/components/admin/data";
import { proposalListQuery, readProposalFilters } from "@/components/curator/model";
import { ProposalsPage } from "@/components/curator/proposals-page";
import { curatorStatusQuery, proposalsQuery } from "@/components/curator/queries";
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
  const t = await getTranslations("curator");
  return { title: t("metaProposals", { project: decodeURIComponent((await params).project) }) };
}

/** The proposals of a project's review runs, with the filters the URL names, read on the server. */
export default async function ProjectProposalsPage({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const filters = readProposalFilters(fromRecord(await searchParams));
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, proposalsQuery(() => api, project, proposalListQuery(filters)));
  if (error === null) await prefetch(client, curatorStatusQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ProposalsPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
