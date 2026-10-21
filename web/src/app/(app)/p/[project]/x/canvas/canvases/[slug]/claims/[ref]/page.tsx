import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { claimQuery, judgingsQuery } from "@/components/canvas/api";
import { ClaimPage } from "@/components/canvas/claim-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { canvasParams, claimCode } from "../../../../route-params";

type Props = { params: Promise<{ project: string; slug: string; ref: string }> };

async function names(params: Props["params"]) {
  return { ...(await canvasParams(params)), code: claimCode((await params).ref) };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("canvas.meta");
  return { title: t("claim", await names(params)) };
}

/** A claim whole: its revs, evidence and test card, and the verdicts of the judging runs on it. */
export default async function ClaimRoute({ params }: Props) {
  const { project, slug, code } = await names(params);
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, claimQuery(() => api, project, slug, code));
  if (!error) await prefetch(client, judgingsQuery(() => api, project, slug, code));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ClaimPage project={project} slug={slug} code={code} initialError={error} />
    </HydrationBoundary>
  );
}
