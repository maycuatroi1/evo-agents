import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { KgUploadPage } from "@/components/kg/upload/kg-upload-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { kgUploadsQuery } from "@/lib/kg/upload-queries";
import { PROJECT_NAME, projectQuery } from "@/lib/queries";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("kg");
  return { title: `${t("tabs.upload")}: ${t("title")}: ${decodeURIComponent((await params).project)}` };
}

/**
 * /p/{project}/kg/upload: an archive sent as a source of the graph, and the reports of the project's uploads; the
 * project (its labels and hub sink) and the uploads are prefetched with the visitor's cookie.
 */
export default async function KgUploadRoute({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  const api = await serverApi();
  const client = getQueryClient();
  const [info, uploads] = await Promise.all([
    prefetch(client, projectQuery(() => api, project)),
    prefetch(client, kgUploadsQuery(() => api, project)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <KgUploadPage project={project} errors={{ project: info, uploads }} />
    </HydrationBoundary>
  );
}
