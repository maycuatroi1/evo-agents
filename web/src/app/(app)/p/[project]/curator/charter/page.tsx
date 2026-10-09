import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { fromRecord } from "@/components/admin/data";
import { CharterPage } from "@/components/curator/charter-page";
import { readCharterView } from "@/components/curator/model";
import { charterQuery, charterRevisionsQuery, curatorStatusQuery } from "@/components/curator/queries";
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
  return { title: t("metaCharter", { project: decodeURIComponent((await params).project) }) };
}

/** A project's charter, at its newest revision or the one the URL names, and its revisions, read on the server. */
export default async function ProjectCharterPage({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const { revision } = readCharterView(fromRecord(await searchParams));
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, curatorStatusQuery(() => api, project));
  if (error === null) {
    await Promise.all([prefetch(client, charterQuery(() => api, project, revision)), prefetch(client, charterRevisionsQuery(() => api, project))]);
  }
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <CharterPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
