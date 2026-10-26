import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { docsDocumentQuery } from "@/components/docs/api";
import { HistoryPage } from "@/components/docs/history-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { documentParams } from "../../../route-params";

type Props = { params: Promise<{ project: string; slug: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("docs.meta");
  return { title: t("history", await documentParams(params)) };
}

/** Every version of a document, the newest first. */
export default async function DocsHistoryRoute({ params }: Props) {
  const { project, slug } = await documentParams(params);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, docsDocumentQuery(() => api, project, slug, null));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <HistoryPage project={project} slug={slug} initialError={error} />
    </HydrationBoundary>
  );
}
