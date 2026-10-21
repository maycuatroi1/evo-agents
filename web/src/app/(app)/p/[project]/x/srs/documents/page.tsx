import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { srsDocumentsQuery } from "@/components/srs/api";
import { DocumentsPage } from "@/components/srs/documents-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { requirePlugin } from "@/lib/plugins-server";
import { PROJECT_NAME } from "@/lib/queries";

import { pageTitle } from "../route-params";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("srs.meta");
  return { title: t("documents", { project: decodeURIComponent((await params).project) }) };
}

/** The BA documents of a project that enabled the core plugin srs; the layout has checked it did. */
export default async function SrsDocumentsRoute({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  const plugin = await requirePlugin("srs", project, "documents");
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, srsDocumentsQuery(() => api, project));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <DocumentsPage project={project} title={pageTitle(plugin, "documents", "SRS")} initialError={error} />
    </HydrationBoundary>
  );
}
