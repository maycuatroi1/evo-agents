"use client";

import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import { useHubQuery } from "@/components/states/query-view";
import { ApiErrorState, NotFoundState } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { parseScope, type Scope, scopeKey, withScope } from "@/lib/kg/explorer";
import { cleanQuery, kgBuildsQuery, kgClusterSummaryQuery, kgGraphQuery } from "@/lib/kg/queries";
import type { KgNode } from "@/lib/kg/types";

import { type ExplorerLayout, SCOPE_LAYOUTS } from "./explorer-layout";
import { RailKinds, RailSearch, RailStats } from "./explorer-rail";
import { NodeScope } from "./explorer-node-scope";
import { ClusterScope, OverviewScope } from "./explorer-scopes";
import { KgHeader } from "./kg-header";

export type KgExplorerErrors = {
  builds: ApiErrorInfo | null;
  graph: ApiErrorInfo | null;
  search: ApiErrorInfo | null;
};

/**
 * /p/{project}/kg: the knowledge graph explorer, where the sidebar's Knowledge graph lands. The graph shows at once:
 * the overview of its clusters, which opens into a cluster's members and into a node's neighbourhood, with the
 * graph's figures, its kinds as filters and a search in the rail, and the inspector for what is selected. The scope
 * lives in the URL (`?cluster=`, `?node=`), so the browser's back and forward walk the exploration and a view can be
 * shared; the search too (`?q=`). The kinds hidden and the layout last for the visit. Read-only.
 */
export function KgExplorer({ project, errors }: { project: string; errors: KgExplorerErrors }) {
  const tStates = useTranslations("states");
  const params = useSearchParams();
  const pathname = usePathname();
  const scope = useMemo(() => parseScope(params), [params]);
  const query = cleanQuery(params.get("q") ?? undefined);
  const kind = cleanQuery(params.get("kind") ?? undefined);
  const [hiddenKinds, setHiddenKinds] = useState<ReadonlySet<string>>(new Set());
  const [chosen, setChosen] = useState<ExplorerLayout>("force");
  const layout = SCOPE_LAYOUTS[scope.kind].includes(chosen) ? chosen : "force";
  // Builds in progress are the page's live work: the top bar follows their query while it polls.
  const builds = useHubQuery(kgBuildsQuery(browserApi, project), errors.builds, { live: true });
  const graph = useHubQuery(kgGraphQuery(browserApi, project), errors.graph);
  const summary = useHubQuery(kgClusterSummaryQuery(browserApi, project), null);

  const write = useCallback(
    (next: URLSearchParams, push: boolean) => {
      const text = next.toString();
      const url = `${pathname}${text ? `?${text}` : ""}`;
      if (push) window.history.pushState(null, "", url);
      else window.history.replaceState(null, "", url);
    },
    [pathname],
  );
  const navigate = useCallback((next: Scope) => write(withScope(params, next), true), [params, write]);
  const commitQuery = (text: string) => {
    const next = new URLSearchParams(params.toString());
    if (text) next.set("q", text);
    else next.delete("q");
    write(next, false);
  };
  const pick = (node: KgNode) => navigate({ kind: "node", id: node.id });
  const toggleKind = (name: string) =>
    setHiddenKinds((now) => (now.has(name) ? new Set([...now].filter((k) => k !== name)) : new Set([...now, name])));

  if (builds.status === "error" && builds.error.status === 404) {
    return (
      <NotFoundState title={tStates("projectNotFoundTitle", { name: project })} description={tStates("projectNotFoundDescription")} />
    );
  }
  const header = <KgHeader project={project} current="explore" />;
  if (builds.status === "error") {
    return (
      <>
        {header}
        <ApiErrorState error={builds.error} onRetry={builds.retry} />
      </>
    );
  }
  const common = {
    project,
    hiddenKinds,
    layout,
    onLayout: setChosen,
    navigate,
    summary,
    search: (
      <RailSearch project={project} query={query} kind={kind} initialError={errors.search} onCommit={commitQuery} onPick={pick} />
    ),
    shared: (present: ReadonlySet<string> | null) => (
      <>
        <RailStats
          project={project}
          summary={graph.status === "success" ? graph.data : undefined}
          clusters={summary.status === "success" ? summary.data.clusters.length : null}
          latest={builds.status === "success" ? builds.data.builds[0] : undefined}
        />
        <RailKinds
          kinds={graph.status === "success" ? graph.data.kinds : []}
          hidden={hiddenKinds}
          present={present}
          onToggle={toggleKind}
          onShowAll={() => setHiddenKinds(new Set())}
        />
      </>
    ),
  };
  const key = scopeKey(scope);
  return (
    <div className="flex flex-col gap-4 lg:h-[calc(100dvh-6.25rem)] lg:min-h-[36rem]" data-testid="kg-explorer-page">
      {header}
      {scope.kind === "overview" ? (
        <OverviewScope key={key} {...common} />
      ) : scope.kind === "cluster" ? (
        <ClusterScope key={key} id={scope.id} {...common} />
      ) : (
        <NodeScope key={key} seed={scope.id} {...common} />
      )}
    </div>
  );
}
