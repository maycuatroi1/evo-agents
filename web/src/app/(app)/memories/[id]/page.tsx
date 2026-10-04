import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { MemoryDetail } from "@/components/memories/memory-detail";
import type { SearchRecord } from "@/components/memories/memory-filters";
import {
  type Memory,
  memoryKeys,
  memoryQuery,
  memoryRevisionQuery,
  memoryRevisionsQuery,
  parseMemoryId,
  parseRevision,
} from "@/components/memories/queries";
import { projectHref } from "@/components/shell/nav";
import { NotFoundState } from "@/components/states/states";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

type Props = { params: Promise<{ id: string }>; searchParams: Promise<SearchRecord> };

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("memories");
  return { title: t("detail.title") };
}

/** One personal memory; a project's memory opened here moves to its page under the project. */
export default async function PersonalMemoryPage({ params, searchParams }: Props) {
  const id = parseMemoryId((await params).id);
  const t = await getTranslations("memories.detail");
  if (id === null) return <NotFoundState title={t("notFoundTitle")} description={t("notFoundDescription")} />;
  const revision = parseRevision((await searchParams).revision);
  const api = await serverApi();
  const client = getQueryClient();
  const [error, revisionsError] = await Promise.all([
    prefetch(client, memoryQuery(() => api, id)),
    prefetch(client, memoryRevisionsQuery(() => api, id)),
    revision !== null ? prefetch(client, memoryRevisionQuery(() => api, id, revision)) : null,
  ]);
  const memory = client.getQueryData<Memory>(memoryKeys.one(id));
  if (memory?.scope === "project" && memory.project) {
    const query = revision !== null ? `?revision=${revision}` : "";
    redirect(`${projectHref(memory.project, `memories/${id}`)}${query}`);
  }
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <MemoryDetail
        scope={{ kind: "personal" }}
        id={id}
        revision={revision}
        initialError={error}
        revisionsError={revisionsError}
      />
    </HydrationBoundary>
  );
}
