import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { srsDocumentQuery } from "@/components/srs/api";
import { ItemPage } from "@/components/srs/item-page";
import { shownNumber, versionParam } from "@/components/srs/model";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { documentParams } from "../../../../route-params";

type Props = {
  params: Promise<{ project: string; slug: string; ref: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

async function names(params: Props["params"]) {
  const ref = decodeURIComponent((await params).ref);
  if (!ref || ref.length > 100) notFound();
  return { ...(await documentParams(params)), ref };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("srs.meta");
  return { title: t("item", await names(params)) };
}

/** One coded item of a document, by code, alias or key, at the approved version unless `v` asks another. */
export default async function SrsItemRoute({ params, searchParams }: Props) {
  const { project, slug, ref } = await names(params);
  const asked = versionParam((await searchParams).v);
  const api = await serverApi();
  const client = getQueryClient();
  const latest = srsDocumentQuery(() => api, project, slug, null);
  const error = await prefetch(client, latest);
  const held = client.getQueryData(latest.queryKey);
  const shown = held ? shownNumber(held, asked) : null;
  if (held && shown !== null && shown !== held.latest_version) await prefetch(client, srsDocumentQuery(() => api, project, slug, shown));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ItemPage project={project} slug={slug} itemRef={ref} asked={asked} initialError={error} />
    </HydrationBoundary>
  );
}
