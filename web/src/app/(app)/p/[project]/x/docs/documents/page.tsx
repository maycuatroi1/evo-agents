import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { docsDocumentsQuery } from "@/components/docs/api";
import { DocumentsPage } from "@/components/docs/documents-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { requirePlugin } from "@/lib/plugins-server";

import { projectParam } from "../route-params";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("docs.meta");
  return { title: t("documents", { project: decodeURIComponent((await params).project) }) };
}

/** The documents of a project the feature docs is on in; the layout has checked it is. */
export default async function DocsDocumentsRoute({ params }: Props) {
  const project = await projectParam(params);
  const plugin = await requirePlugin("docs", project, "documents");
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, docsDocumentsQuery(() => api, project));
  const title = plugin?.pages.find((page) => page.name === "documents")?.title ?? "Docs";
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <DocumentsPage project={project} title={title} initialError={error} />
    </HydrationBoundary>
  );
}
