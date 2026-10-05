import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { parseRunId, runQuery } from "@/components/runs/queries";
import { RunDetail } from "@/components/runs/run-detail";
import { NotFoundState } from "@/components/states/states";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string; id: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("runs.detail");
  const { project, id } = await params;
  return { title: t("metaTitle", { id: decodeURIComponent(id), project: decodeURIComponent(project) }) };
}

/**
 * One run of a project, for any member who may read its plan: the run is read on the server so the page renders with
 * it, then the browser follows its log live and refreshes it while it is active.
 */
export default async function RunRoute({ params }: Props) {
  const { project: rawProject, id: rawId } = await params;
  const project = decodeURIComponent(rawProject);
  const id = parseRunId(decodeURIComponent(rawId));
  const t = await getTranslations("runs.detail");
  if (!PROJECT_NAME.test(project) || id === null) {
    return <NotFoundState title={t("notFoundTitle", { id: decodeURIComponent(rawId) })} description={t("notFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, runQuery(() => api, project, id));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <RunDetail project={project} runId={id} initialError={error} />
    </HydrationBoundary>
  );
}
