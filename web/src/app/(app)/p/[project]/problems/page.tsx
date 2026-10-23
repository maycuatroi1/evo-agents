import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { ProblemsPage } from "@/components/loop/problems-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { problemsQuery } from "@/lib/problem-queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("problems.list");
  return { title: t("meta", { project: decodeURIComponent((await params).project) }) };
}

/** A project's problems, read on the server. */
export default async function ProjectProblemsPage({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, problemsQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ProblemsPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
