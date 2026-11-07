"use client";

import { Boxes, Waypoints } from "lucide-react";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import { useCallback, useMemo, useState } from "react";

import { Segmented } from "@/components/data/segmented";

import { useHubQuery } from "@/components/states/query-view";
import { ApiErrorState, NotFoundState } from "@/components/states/states";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { parseDepth, parseLimit } from "@/lib/kg/entity-params";
import { entityGraphQuery } from "@/lib/kg/entity-queries";
import { parseGraph } from "@/lib/kg/graphs";
import { parseScope, type Scope, scopeKey, withScope } from "@/lib/kg/explorer";
import { cleanQuery, kgBuildsQuery, kgClusterSummaryQuery, kgGraphQuery } from "@/lib/kg/queries";
import type { KgNode } from "@/lib/kg/types";

import type { EntityViewState } from "./entity-view";
import { type ExplorerLayout, SCOPE_LAYOUTS } from "./explorer-layout";
import { RailKinds, RailSearch, RailStats } from "./explorer-rail";
import { NodeScope } from "./explorer-node-scope";
import { GraphPicker } from "./graph-picker";
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
 * shared; the search too (`?q=`), and the named graph it is narrowed to (`?graph=`, ./graph-picker.tsx). The kinds hidden and the layout last for the visit. Read-only.
 *
 * `?view=entities` shows the Entities view instead (./entity-view.tsx): the entities and relations a model read from
 * the documents, drawn with sigma.js, its focus, depth and node count in `?entity=`, `?depth=` and `?limit=`. A URL
 * that names no view, scope or search opens on the Entities view when the graph has entities the reader can see, and
 * on the clusters otherwise; once the reader picks the clusters, `?view=clusters` keeps them, back on the overview too.
 */
type View = "clusters" | "entities";
// The Entities view, its rail, inspector and graphology, loads when it is opened, not with the clusters.
const EntityView = dynamic(() => import("./entity-view").then((module) => module.EntityView));
const ENTITY_PARAMS = ["entity", "depth", "limit"];

function askedView(value: string | null): View | null {
  return value === "entities" || value === "clusters" ? value : null;
}

export function KgExplorer({ project, errors }: { project: string; errors: KgExplorerErrors }) {
  const tStates = useTranslations("states");
  const params = useSearchParams();
  const pathname = usePathname();
  const scope = useMemo(() => parseScope(params), [params]);
  const query = cleanQuery(params.get("q") ?? undefined);
  const kind = cleanQuery(params.get("kind") ?? undefined);
  const named = parseGraph(params.get("graph"));
  const entityState = useMemo<EntityViewState>(
    () => ({ focus: params.get("entity")?.trim() || null, depth: parseDepth(params.get("depth")), limit: parseLimit(params.get("limit")) }),
    [params],
  );
  const asked = askedView(params.get("view"));
  // No view, scope or search in the URL: the entity graph (the Entities view's own first read, so it is read once)
  // says which view opens.
  const undecided = asked === null && scope.kind === "overview" && !query;
  const entities = useHubQuery({ ...entityGraphQuery(browserApi, project, entityState.limit), enabled: undecided }, null);
  const view: View = asked ?? (undecided && entities.status === "success" && entities.data.total_nodes > 0 ? "entities" : "clusters");
  const choosing = undecided && entities.status === "loading";
  const tExplorer = useTranslations("kg.explorer");
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
  const navigate = useCallback(
    (next: Scope) => {
      const search = withScope(params, next);
      // Back on the overview the clusters stay: without a view it would open on the entities.
      if (next.kind === "overview") search.set("view", "clusters");
      write(search, true);
    },
    [params, write],
  );
  const commitQuery = (text: string) => {
    const next = new URLSearchParams(params.toString());
    if (text) next.set("q", text);
    else next.delete("q");
    write(next, false);
  };
  const pickGraph = (name: string) => {
    const next = new URLSearchParams(params.toString());
    if (name) next.set("graph", name);
    else next.delete("graph");
    write(next, false);
  };
  const switchView = (next: View) => {
    const search = withScope(params, { kind: "overview" });
    search.set("view", next);
    if (next === "clusters") for (const name of ENTITY_PARAMS) search.delete(name);
    write(search, true);
  };
  const changeEntities = (next: Partial<EntityViewState>) => {
    const search = new URLSearchParams(params.toString());
    const merged = { ...entityState, ...next };
    if (merged.focus) search.set("entity", merged.focus);
    else search.delete("entity");
    if (merged.focus && merged.depth !== 1) search.set("depth", String(merged.depth));
    else search.delete("depth");
    if (merged.limit !== parseLimit(null)) search.set("limit", String(merged.limit));
    else search.delete("limit");
    // A new focus is a step the browser's back undoes; the depth and the node count only refine the view.
    write(search, next.focus !== undefined && next.focus !== entityState.focus);
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
      <RailSearch
        project={project}
        query={query}
        kind={kind}
        graph={named}
        initialError={errors.search}
        onCommit={commitQuery}
        onPick={pick}
      />
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
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Segmented
        label={tExplorer("view")}
        value={view}
        onChange={switchView}
        options={[
          { value: "clusters", label: tExplorer("views.clusters"), icon: Boxes, testId: "kg-view-clusters" },
          { value: "entities", label: tExplorer("views.entities"), icon: Waypoints, testId: "kg-view-entities" },
          ]}
        />
        <GraphPicker project={project} graph={named} onPick={pickGraph} />
      </div>
      {choosing ? (
        <Skeleton className="h-[62vh] w-full rounded-md lg:h-auto lg:flex-1" />
      ) : view === "entities" ? (
        <EntityView
          project={project}
          state={entityState}
          documents={graph.status === "success" ? (graph.data.kinds.find((row) => row.kind === "Document")?.count ?? 0) : null}
          onChange={changeEntities}
        />
      ) : scope.kind === "overview" ? (
        <OverviewScope key={key} {...common} />
      ) : scope.kind === "cluster" ? (
        <ClusterScope key={key} id={scope.id} {...common} />
      ) : (
        <NodeScope key={key} seed={scope.id} {...common} />
      )}
    </div>
  );
}
