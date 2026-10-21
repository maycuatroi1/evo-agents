import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { endedCyclesQuery, releaseStatusQuery } from "@/components/releases/queries";
import { ReleasesPage } from "@/components/releases/releases-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("releases");
  return { title: t("metaTitle", { project: decodeURIComponent((await params).project) }) };
}

/**
 * A project's daily release, for any member with a grant on it: its schedule, its checks, the cycle open now and the
 * cycles that ended, read on the server so the page renders complete; the browser keeps them current.
 */
export default async function ProjectReleasesPage({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  const t = await getTranslations("states");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, releaseStatusQuery(() => api, project));
  if (error === null) await prefetch(client, endedCyclesQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ReleasesPage project={project} initialError={error} />
    </HydrationBoundary>
  );
}
