import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { ProposalPage } from "@/components/curator/proposal-detail";
import { proposalQuery } from "@/components/curator/queries";
import { parseRunId } from "@/components/runs/queries";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME, projectQuery } from "@/lib/queries";

type Props = { params: Promise<{ project: string; id: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("curator");
  const { project, id } = await params;
  return { title: t("metaProposal", { id: decodeURIComponent(id), project: decodeURIComponent(project) }) };
}

/** One proposal of a project's Curator, read on the server with the project's repos (for links to code). */
export default async function ProposalRoute({ params }: Props) {
  const { project: rawProject, id: rawId } = await params;
  const project = decodeURIComponent(rawProject);
  const id = parseRunId(decodeURIComponent(rawId));
  const t = await getTranslations("curator.proposal");
  if (!PROJECT_NAME.test(project) || id === null) {
    return <NotFoundState title={t("notFoundTitle", { id: decodeURIComponent(rawId) })} description={t("notFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, proposalQuery(() => api, project, id));
  if (error === null) await prefetch(client, projectQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ProposalPage project={project} id={id} initialError={error} />
    </HydrationBoundary>
  );
}
