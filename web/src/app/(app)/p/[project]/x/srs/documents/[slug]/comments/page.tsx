import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { srsCommentsQuery, srsDocumentQuery } from "@/components/srs/api";
import { CommentsPage } from "@/components/srs/comments-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { documentParams } from "../../../route-params";

type Props = { params: Promise<{ project: string; slug: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("srs.meta");
  return { title: t("comments", await documentParams(params)) };
}

/** A document's comments as threads. */
export default async function SrsCommentsRoute({ params }: Props) {
  const { project, slug } = await documentParams(params);
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, srsDocumentQuery(() => api, project, slug, null)),
    prefetch(client, srsCommentsQuery(() => api, project, slug)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <CommentsPage project={project} slug={slug} initialError={error} />
    </HydrationBoundary>
  );
}
