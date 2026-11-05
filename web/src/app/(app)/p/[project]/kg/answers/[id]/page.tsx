import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { KgAnswersPage } from "@/components/kg/answers/kg-answers-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { kgAnswerJobQuery } from "@/lib/kg/answer-queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string; id: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("kgAnswers");
  const { project, id } = await params;
  return { title: `${t("title", { id })}: ${decodeURIComponent(project)}` };
}

/** /p/{project}/kg/answers/{id}: where answer job `id` stands, prefetched with the visitor's cookie. */
export default async function KgAnswersRoute({ params }: Props) {
  const { project: raw, id: rawId } = await params;
  const project = decodeURIComponent(raw);
  const id = Number(rawId);
  if (!PROJECT_NAME.test(project) || !Number.isSafeInteger(id) || id <= 0) notFound();
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, kgAnswerJobQuery(() => api, project, id));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <KgAnswersPage project={project} id={id} error={error} />
    </HydrationBoundary>
  );
}
