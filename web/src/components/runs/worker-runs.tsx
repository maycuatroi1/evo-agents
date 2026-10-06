"use client";

import { useQueries } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";

import { Skeleton } from "@/components/ui/skeleton";
import type { Worker } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";

import { useViewer } from "./hooks";
import { type Run, runsQuery } from "./queries";
import { RunsTable } from "./runs-table";

/** Runs listed on a worker's page, newest first across the projects it serves. */
export const WORKER_RUNS = 10;

/**
 * The latest runs a worker took, read from each project it serves (GET .../runs?worker_id=). Run ids grow across the
 * hub, so the newest come first when the projects' pages are merged by id. A project the visitor holds no grant on
 * (a hub admin reading another member's worker) answers 403 or 404; its runs are left out and the card says so.
 */
export function WorkerRuns({ worker }: { worker: Worker }) {
  const t = useTranslations("workers.detail.runs");
  const format = useFormatter();
  const viewer = useViewer();
  const results = useQueries({
    queries: worker.projects.map((project) => ({
      ...runsQuery(browserApi, project, { workerId: worker.id, limit: WORKER_RUNS }),
      retry: false,
    })),
  });
  if (worker.projects.length === 0) return <p className="text-sm text-muted-foreground">{t("empty")}</p>;
  if (results.some((result) => result.isPending)) return <Skeleton className="h-24 w-full" />;
  const runs: Run[] = results
    .flatMap((result) => result.data?.runs ?? [])
    .sort((a, b) => b.id - a.id)
    .slice(0, WORKER_RUNS);
  const hidden = worker.projects.filter((_, index) => results[index].isError);
  return (
    <div className="flex flex-col gap-2" data-testid="worker-recent-runs">
      {runs.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="worker-runs-empty">
          {t("empty")}
        </p>
      ) : (
        <RunsTable runs={runs} caption={t("caption", { name: worker.name })} viewer={viewer} variant="worker" testId="worker-runs-table" />
      )}
      {hidden.length > 0 ? (
        <p className="text-xs text-muted-foreground" data-testid="worker-runs-hidden">
          {t("hidden", { count: hidden.length, projects: format.list(hidden, { type: "conjunction" }) })}
        </p>
      ) : null}
    </div>
  );
}
