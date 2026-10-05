import { dehydrate, HydrationBoundary } from "@tanstack/react-query";
import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { getTranslations } from "next-intl/server";

import { MAX_DIFF_BYTES, readDiffBlob } from "@/components/runs/diff-blob";
import { parseDiff } from "@/components/runs/diff-model";
import { parseRunId, runDiffLink, runQuery } from "@/components/runs/queries";
import { type DiffState, RunDiff } from "@/components/runs/run-diff";
import { NotFoundState } from "@/components/states/states";
import { isApiError, toInfo } from "@/lib/api/errors";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { LOGIN_PATH } from "@/lib/config";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string; id: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("runs.diff");
  const { project, id } = await params;
  return { title: t("metaTitle", { id: decodeURIComponent(id), project: decodeURIComponent(project) }) };
}

/**
 * The diff a run's worker uploaded when the run ended (blob kind run-diff). The API signs a GET of the blob store; this
 * server reads it (diff-blob.ts) and the page renders it, so nothing in the browser talks to the store but the
 * download, which is a navigation.
 */
export default async function RunDiffRoute({ params }: Props) {
  const { project: rawProject, id: rawId } = await params;
  const project = decodeURIComponent(rawProject);
  const id = parseRunId(decodeURIComponent(rawId));
  const t = await getTranslations("runs.detail");
  if (!PROJECT_NAME.test(project) || id === null) {
    return <NotFoundState title={t("notFoundTitle", { id: decodeURIComponent(rawId) })} description={t("notFoundDescription")} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, runQuery(() => api, project, id));
  let diff: DiffState = { status: "none" };
  if (!error) {
    try {
      const link = await runDiffLink(api, project, id);
      const blob = await readDiffBlob(link.url);
      diff =
        blob.status === "ok"
          ? {
              status: "ok",
              diff: parseDiff(blob.text),
              bytes: link.size ?? blob.bytes,
              cutBytes: blob.cut,
              maxBytes: MAX_DIFF_BYTES,
              sha256: link.sha256,
            }
          : { status: "unreadable", reason: blob.reason, sha256: link.sha256 };
    } catch (failure) {
      if (isApiError(failure) && failure.kind === "unauthorized") redirect(LOGIN_PATH);
      const info = toInfo(failure);
      diff = info.status === 404 ? { status: "none" } : info.status === 503 ? { status: "unavailable" } : { status: "error", error: info };
    }
  }
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <RunDiff project={project} runId={id} initialError={error} diff={diff} />
    </HydrationBoundary>
  );
}
