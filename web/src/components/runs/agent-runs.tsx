"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";

import { useViewer } from "./hooks";
import { runsQuery } from "./queries";
import { RunsTable } from "./runs-table";

/** Runs listed on an agent's page, and how many of the project's latest runs are searched for them. */
export const AGENT_RUNS = 10;
export const AGENT_RUNS_SEARCHED = 200;

/**
 * The latest runs that went under agent `handle` of `project`, newest first: the API lists a project's runs without a
 * filter by agent, so the card reads the project's latest AGENT_RUNS_SEARCHED runs (those of the plans the visitor may
 * read) and keeps the agent's, and says so. It asks again every 5 seconds while the project has an active run.
 */
export function AgentRuns({ project, handle }: { project: string; handle: string }) {
  const t = useTranslations("agents.detail.runs");
  const viewer = useViewer();
  const state = useQuery(runsQuery(browserApi, project, { limit: AGENT_RUNS_SEARCHED }));
  if (state.isPending) return <Skeleton className="h-24 w-full" />;
  if (state.isError) {
    return (
      <p className="text-sm text-danger" role="alert" data-testid="agent-runs-failed">
        {t("failed")}
      </p>
    );
  }
  const runs = state.data.runs.filter((run) => run.profile === handle).slice(0, AGENT_RUNS);
  return (
    <div className="flex flex-col gap-2" data-testid="agent-recent-runs">
      {runs.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="agent-runs-empty">
          {t("empty", { searched: AGENT_RUNS_SEARCHED })}
        </p>
      ) : (
        <RunsTable runs={runs} caption={t("caption", { handle })} viewer={viewer} testId="agent-runs-table" />
      )}
      <p className="text-xs text-fg-subtle">{t("scope", { searched: AGENT_RUNS_SEARCHED })}</p>
    </div>
  );
}
