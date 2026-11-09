import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { srsDocumentQuery, srsOpenFindingsQuery, srsReviewQueueQuery } from "@/components/srs/api";
import { CaseReviewPage } from "@/components/srs/case-review-page";
import { versionParam } from "@/components/srs/model";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { documentParams } from "../../../../../../route-params";

type Props = { params: Promise<{ project: string; slug: string; number: string; key: string }> };

async function names(params: Props["params"]) {
  const { number: raw, key: rawKey } = await params;
  const number = versionParam(raw);
  const key = decodeURIComponent(rawKey);
  if (number === null || !key || key.length > 100) notFound();
  return { ...(await documentParams(params)), number, key };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("srs.meta");
  const found = await names(params);
  // The tab names the use case by its code, as the page does; its key until the version is read.
  const api = await serverApi();
  const client = getQueryClient();
  const shown = srsDocumentQuery(() => api, found.project, found.slug, found.number);
  await prefetch(client, shown);
  const item = client.getQueryData(shown.queryKey)?.shown?.items.find((each) => each.key === found.key);
  return { title: t("caseReview", { ...found, key: item?.code ?? item?.title ?? found.key }) };
}

/**
 * The review of one use case of a version, by the use case's key: what it says, what this version changed, @ba's
 * points to settle and the decision. The hub's notices link here when a version has one use case to review.
 */
export default async function SrsCaseReviewRoute({ params }: Props) {
  const { project, slug, number, key } = await names(params);
  const api = await serverApi();
  const client = getQueryClient();
  const latest = srsDocumentQuery(() => api, project, slug, null);
  // Only the document's failure fails the page; the findings and the queue are the cards' to retry in the browser.
  const [error] = await Promise.all([
    prefetch(client, latest),
    prefetch(client, srsOpenFindingsQuery(() => api, project, slug)),
    prefetch(client, srsReviewQueueQuery(() => api, project)),
  ]);
  const held = client.getQueryData(latest.queryKey);
  if (held && held.versions.some((version) => version.number === number)) {
    const approved = held.approved_version !== null && held.approved_version < number ? held.approved_version : null;
    await Promise.all([
      held.latest_version !== number ? prefetch(client, srsDocumentQuery(() => api, project, slug, number)) : null,
      approved !== null ? prefetch(client, srsDocumentQuery(() => api, project, slug, approved)) : null,
    ]);
  }
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <CaseReviewPage project={project} slug={slug} number={number} caseKey={key} initialError={error} />
    </HydrationBoundary>
  );
}
