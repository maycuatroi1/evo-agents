import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { srsDocumentQuery } from "@/components/srs/api";
import { VersionsPage } from "@/components/srs/versions-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { documentParams } from "../../../route-params";

type Props = { params: Promise<{ project: string; slug: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("srs.meta");
  return { title: t("versions", await documentParams(params)) };
}

/** A document's versions, the newest first, and a form that compares two. */
export default async function SrsVersionsRoute({ params }: Props) {
  const { project, slug } = await documentParams(params);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, srsDocumentQuery(() => api, project, slug, null));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <VersionsPage project={project} slug={slug} initialError={error} />
    </HydrationBoundary>
  );
}
