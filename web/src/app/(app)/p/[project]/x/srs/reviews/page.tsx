import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { srsReviewQueueQuery } from "@/components/srs/api";
import { ReviewQueuePage } from "@/components/srs/review-queue-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { requirePlugin } from "@/lib/plugins-server";
import { PROJECT_NAME } from "@/lib/queries";

import { pageTitle } from "../route-params";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("srs.meta");
  return { title: t("reviews", { project: decodeURIComponent((await params).project) }) };
}

/** The review queue of a project that enabled the core plugin srs: the use cases of every version in review. */
export default async function SrsReviewsRoute({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  const plugin = await requirePlugin("srs", project, "reviews");
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, srsReviewQueueQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ReviewQueuePage project={project} title={pageTitle(plugin, "reviews", "SRS reviews")} initialError={error} />
    </HydrationBoundary>
  );
}
