import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { srsDocumentQuery, srsFindingsQuery } from "@/components/srs/api";
import { versionParam } from "@/components/srs/model";
import { ReviewPage } from "@/components/srs/review-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { documentParams } from "../../../../route-params";

type Props = { params: Promise<{ project: string; slug: string; number: string }> };

async function names(params: Props["params"]) {
  const number = versionParam((await params).number);
  if (number === null) notFound();
  return { ...(await documentParams(params)), number };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("srs.meta");
  return { title: t("review", await names(params)) };
}

/** A version's review: its approvals, the document's findings, the reviewer's form, what it changes and its checks. */
export default async function SrsReviewRoute({ params }: Props) {
  const { project, slug, number } = await names(params);
  const api = await serverApi();
  const client = getQueryClient();
  const latest = srsDocumentQuery(() => api, project, slug, null);
  // The findings' failure is the card's to show: the browser asks again, and only the document's fails the page.
  const [error] = await Promise.all([prefetch(client, latest), prefetch(client, srsFindingsQuery(() => api, project, slug))]);
  const held = client.getQueryData(latest.queryKey);
  if (held && held.latest_version !== number && held.versions.some((version) => version.number === number)) {
    await prefetch(client, srsDocumentQuery(() => api, project, slug, number));
  }
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ReviewPage project={project} slug={slug} number={number} initialError={error} />
    </HydrationBoundary>
  );
}
