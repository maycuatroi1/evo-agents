import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { srsDiffQuery, srsDocumentQuery } from "@/components/srs/api";
import { ComparePage } from "@/components/srs/compare-page";
import { comparePair, versionParam } from "@/components/srs/model";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { documentParams } from "../../../route-params";

type Props = {
  params: Promise<{ project: string; slug: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("srs.meta");
  return { title: t("compare", await documentParams(params)) };
}

/** Two versions of a document compared item by item: `from` and `to`, else the approved version and the latest. */
export default async function SrsCompareRoute({ params, searchParams }: Props) {
  const { project, slug } = await documentParams(params);
  const query = await searchParams;
  const from = versionParam(query.from);
  const to = versionParam(query.to);
  const api = await serverApi();
  const client = getQueryClient();
  const latest = srsDocumentQuery(() => api, project, slug, null);
  const error = await prefetch(client, latest);
  const held = client.getQueryData(latest.queryKey);
  const pair = held ? comparePair(held, from, to) : null;
  if (pair) await prefetch(client, srsDiffQuery(() => api, project, slug, pair.from, pair.to));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ComparePage project={project} slug={slug} from={from} to={to} initialError={error} />
    </HydrationBoundary>
  );
}
