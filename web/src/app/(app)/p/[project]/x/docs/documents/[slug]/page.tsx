import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { docsDocumentQuery } from "@/components/docs/api";
import { DocumentPage } from "@/components/docs/document-page";
import { versionParam } from "@/components/docs/model";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { documentParams } from "../../route-params";

type Props = {
  params: Promise<{ project: string; slug: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("docs.meta");
  return { title: t("document", await documentParams(params)) };
}

/** A document at the version asked (`v`), the latest by default. */
export default async function DocsDocumentRoute({ params, searchParams }: Props) {
  const { project, slug } = await documentParams(params);
  const version = versionParam((await searchParams).v);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, docsDocumentQuery(() => api, project, slug, version));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <DocumentPage project={project} slug={slug} version={version} initialError={error} />
    </HydrationBoundary>
  );
}
