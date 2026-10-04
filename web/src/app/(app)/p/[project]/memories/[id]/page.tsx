import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { MemoryDetail } from "@/components/memories/memory-detail";
import type { SearchRecord } from "@/components/memories/memory-filters";
import {
  memoryQuery,
  memoryRevisionQuery,
  memoryRevisionsQuery,
  parseMemoryId,
  parseRevision,
} from "@/components/memories/queries";
import { NotFoundState } from "@/components/states/states";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME, projectQuery } from "@/lib/queries";

type Props = { params: Promise<{ project: string; id: string }>; searchParams: Promise<SearchRecord> };

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("memories");
  return { title: t("detail.title") };
}

/** One memory of a project, its content and its history; a memory the visitor may not read is not found. */
export default async function ProjectMemoryPage({ params, searchParams }: Props) {
  const { project: raw, id: rawId } = await params;
  const project = decodeURIComponent(raw);
  const id = parseMemoryId(rawId);
  const t = await getTranslations("memories.detail");
  if (!PROJECT_NAME.test(project) || id === null) {
    return <NotFoundState title={t("notFoundTitle")} description={t("notFoundDescription")} />;
  }
  const revision = parseRevision((await searchParams).revision);
  const api = await serverApi();
  const client = getQueryClient();
  const [error, revisionsError] = await Promise.all([
    prefetch(client, memoryQuery(() => api, id)),
    prefetch(client, memoryRevisionsQuery(() => api, id)),
    revision !== null ? prefetch(client, memoryRevisionQuery(() => api, id, revision)) : null,
    prefetch(client, projectQuery(() => api, project)),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <MemoryDetail
        scope={{ kind: "project", project }}
        id={id}
        revision={revision}
        initialError={error}
        revisionsError={revisionsError}
      />
    </HydrationBoundary>
  );
}
