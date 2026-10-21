import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { readPolicyView } from "@/components/merge-queue/policy-model";
import { PolicyPage } from "@/components/merge-queue/policy-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { policyQuery, policyRevisionsQuery } from "@/lib/merge-queue";
import { PROJECT_NAME } from "@/lib/queries";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("policy");
  return { title: t("metaTitle", { project: decodeURIComponent((await params).project) }) };
}

/**
 * A project's autonomy policy, at its newest revision or the one the URL names, and its revisions, read on the server.
 * The form (`?edit=1`) reads the newest revision; whether the visitor may write it is the client's to say.
 */
export default async function ProjectPolicyPage({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const { revision, edit } = readPolicyView(await searchParams);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, policyQuery(() => api, project, edit ? null : revision));
  if (error === null) {
    await Promise.all([
      prefetch(client, policyRevisionsQuery(() => api, project)),
      edit && revision !== null ? prefetch(client, policyQuery(() => api, project, revision)) : Promise.resolve(null),
    ]);
  }
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <PolicyPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
