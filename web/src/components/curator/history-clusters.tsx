"use client";

import { ListFilter } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import type { HubQueryState } from "@/components/states/query-view";
import { ApiErrorState } from "@/components/states/states";
import { Button } from "@/components/ui/button";

import { EvidenceOpener, useModeText } from "./history-sessions";
import type { FacetCluster, FacetClusterList } from "./insight-queries";
import { useInsightFormat } from "./insight-format";
import { Panel, PanelLine, PanelSkeleton } from "./panel";

function ClusterItem({ project, cluster, selected, onSelect }: { project: string; cluster: FacetCluster; selected: boolean; onSelect: () => void }) {
  const t = useTranslations("curator.history.clusters");
  const tTask = useTranslations("curator.history.task");
  const mode = useModeText();
  return (
    <li className="flex min-w-0 flex-col gap-1.5 px-4 py-3" data-testid="history-cluster" data-key={cluster.key} aria-current={selected || undefined}>
      <div className="flex min-w-0 flex-wrap items-start gap-x-3 gap-y-1.5">
        <div className="flex min-w-0 flex-1 basis-56 flex-col gap-1">
          <p className="text-sm font-medium text-pretty text-foreground [overflow-wrap:anywhere]">{cluster.name}</p>
          {cluster.description ? <p className="text-[13px] text-pretty text-muted-foreground [overflow-wrap:anywhere]">{cluster.description}</p> : null}
        </div>
        <Button
          type="button"
          variant={selected ? "secondary" : "ghost"}
          size="sm"
          aria-pressed={selected}
          onClick={onSelect}
          data-testid="history-cluster-filter"
        >
          <ListFilter aria-hidden="true" />
          {selected ? t("shown") : t("show")}
        </Button>
      </div>
      <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs text-fg-subtle">
        <Tag>{tTask.has(cluster.task_type as never) ? tTask(cluster.task_type as never) : cluster.task_type}</Tag>
        <Tag title={cluster.failure_mode === "none" ? undefined : mode(cluster.failure_mode)}>
          {cluster.failure_mode === "none" ? t("noMode") : <span className="font-mono">{cluster.failure_mode}</span>}
          {cluster.failure_mode === "none" ? null : <span className="sr-only">{mode(cluster.failure_mode)}</span>}
        </Tag>
        <span className="tabular-nums" data-testid="history-cluster-counts">
          {t("counts", { sessions: cluster.sessions, runs: cluster.runs, members: cluster.members })}
        </span>
        {cluster.named ? null : <span>{t("unnamed")}</span>}
      </div>
      {cluster.representative ? <EvidenceOpener project={project} evidence={cluster.representative} testId="history-cluster-open" /> : null}
    </li>
  );
}

/**
 * The clusters of the project's sessions as the hub last counted them: the sessions the cheap model read, grouped by
 * their kind of task and their first failure mode and named by the model, each with its sessions, runs and members and
 * its latest session to read. Only clusters of enough sessions are listed; the smaller ones are counted. Show its
 * sessions filters the list of sessions below by the cluster.
 */
export function ClustersPanel({
  project,
  state,
  selected,
  onSelect,
}: {
  project: string;
  state: HubQueryState<FacetClusterList>;
  selected: string | null;
  onSelect: (key: string | null) => void;
}) {
  const t = useTranslations("curator.history.clusters");
  const format = useInsightFormat();
  const list = state.status === "success" ? state.data : null;
  let body;
  if (state.status === "loading") body = <PanelSkeleton rows={4} />;
  else if (state.status === "error") body = <ApiErrorState error={state.error} onRetry={state.retry} />;
  else if (state.data.clusters.length === 0) body = <PanelLine testId="history-clusters-empty">{t("empty", { min: state.data.min_sessions })}</PanelLine>;
  else {
    body = (
      <ul className="flex flex-col divide-y" aria-label={t("title")} data-testid="history-clusters-list">
        {state.data.clusters.map((cluster) => {
          const chosen = selected !== null && (cluster.key === selected || cluster.keys.includes(selected));
          return <ClusterItem key={cluster.key} project={project} cluster={cluster} selected={chosen} onSelect={() => onSelect(chosen ? null : cluster.key)} />;
        })}
      </ul>
    );
  }
  const sub = list
    ? [
        list.counted_at ? t("counted", { at: format.moment(list.counted_at) }) : t("notCounted"),
        ...(list.hidden > 0 ? [t("hidden", { count: list.hidden, min: list.min_sessions })] : []),
      ].join(" ")
    : undefined;
  return (
    <Panel title={t("title")} sub={sub} testId="history-clusters">
      {body}
    </Panel>
  );
}
