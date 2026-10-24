import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { KgReviewPage } from "@/components/kg/kg-review-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { kgReviewQuery, parseReviewFilter } from "@/lib/kg/review";
import { PROJECT_NAME } from "@/lib/queries";

type Props = {
  params: Promise<{ project: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("kg.review");
  return { title: t("title") };
}

/** /p/{project}/kg/review?rule=&cluster=&state=: the review queue, prefetched with the visitor's cookie. */
export default async function KgReviewRoute({ params, searchParams }: Props) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  const filter = parseReviewFilter(await searchParams);
  const client = getQueryClient();
  const api = await serverApi();
  const error = await prefetch(client, kgReviewQuery(() => api, project, filter));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <KgReviewPage project={project} filter={filter} error={error} />
    </HydrationBoundary>
  );
}
