"use client";

import { useQueries } from "@tanstack/react-query";
import { Tags } from "lucide-react";
import dynamic from "next/dynamic";
import { useFormatter, useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import { Segmented } from "@/components/data/segmented";
import { useHubQuery } from "@/components/states/query-view";
import { ApiErrorState } from "@/components/states/states";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import {
  DEPTHS,
  type Depth,
  EXPAND_LIMIT,
  LIMITS,
  mergeGraphs,
  splitShown,
  typeCounts,
  type WorkingSet,
} from "@/lib/kg/entity-graph";
import { entityAroundQuery, entityGraphQuery } from "@/lib/kg/entity-queries";
import { cn } from "@/lib/utils";

import { EntitiesEmpty } from "./entity-empty";
import { type EntityActions, EntityInspector, RelationInspector } from "./entity-inspector";
import { EntitySearch, EntityStats, ExpandedEntities, IsolatedList, TypeDot, TypeFilter } from "./entity-rail";
import { Trail, Workspace } from "./explorer-frame";
import { CanvasState } from "./explorer-scopes";
import { NoGraph } from "./no-graph";

// sigma needs a window and WebGL: the canvas loads in the browser only.
const EntityCanvas = dynamic(() => import("./entity-canvas"), { ssr: false, loading: () => <Skeleton className="absolute inset-0 rounded-none" /> });

type Selection = { type: "node" | "edge"; id: string } | null;

/** The neighbourhoods opened, as one value that keeps its identity while none of them changes (TanStack's combine). */
function combineOpened(results: { data?: WorkingSet; isFetching: boolean }[]) {
  return { data: results.map((result) => result.data), fetching: results.some((result) => result.isFetching) };
}

/** What a keyboard or screen reader user is told about the canvas. */
const ENTITY_HELP_ID = "kg-entity-help";

export type EntityViewState = { focus: string | null; depth: Depth; limit: number };

const SELECT =
  "h-7 cursor-pointer rounded-sm border bg-card px-2 text-xs font-medium text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none max-md:h-11 max-md:text-base";

/**
 * The explorer's Entities view: the graph of entities and relations the entities step read from the project's
 * documents, as LightRAG's web UI shows it. By default the entities the most documents mention with the relations
 * between them; with a focus (`?entity=`), the entities within a depth of it. A double click opens a node's
 * neighbours into the same canvas. The focus, depth and node count live in the URL; the kinds hidden, the selection
 * and the neighbourhoods opened last for the visit. Read-only.
 */
export function EntityView({
  project,
  state,
  documents,
  onChange,
}: {
  project: string;
  state: EntityViewState;
  /** The documents the reader can see, for the estimate of the empty state. */
  documents: number | null;
  onChange: (next: Partial<EntityViewState>) => void;
}) {
  const t = useTranslations("kg.explorer.entities");
  const format = useFormatter();
  const { focus, depth, limit } = state;
  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set());
  const [selection, setSelection] = useState<Selection>(null);
  const [expanded, setExpanded] = useState<string[]>([]);
  const [allLabels, setAllLabels] = useState(false);
  const [centre, setCentre] = useState<{ id: string; nonce: number } | null>(null);

  const overview = useHubQuery({ ...entityGraphQuery(browserApi, project, limit), enabled: focus === null }, null);
  const around = useHubQuery({ ...entityAroundQuery(browserApi, project, focus ?? "", depth, limit), enabled: focus !== null }, null);
  const base = focus === null ? overview : around;
  const opened = useQueries({ queries: expanded.map((id) => entityAroundQuery(browserApi, project, id, 1, EXPAND_LIMIT)), combine: combineOpened });
  const baseData = base.status === "success" ? base.data : null;
  const working = useMemo<WorkingSet>(() => mergeGraphs([baseData, ...opened.data]), [baseData, opened.data]);
  const { linked, isolated } = useMemo(() => splitShown(working, hidden), [working, hidden]);
  const kinds = useMemo(() => typeCounts(working.nodes), [working.nodes]);
  const names = useMemo(() => new Map(working.nodes.map((node) => [node.id, node])), [working.nodes]);
  const reading = opened.fetching;

  const selectNode = useCallback((id: string | null) => setSelection(id ? { type: "node", id } : null), []);
  const selectEdge = useCallback((id: string | null) => setSelection(id ? { type: "edge", id } : null), []);
  const expand = useCallback((id: string) => {
    setExpanded((now) => (now.includes(id) ? now : [...now, id]));
    setSelection({ type: "node", id });
  }, []);
  const focusOn = (id: string) => {
    setExpanded([]);
    setSelection({ type: "node", id });
    onChange({ focus: id });
  };
  const show = (id: string) => {
    setSelection({ type: "node", id });
    setCentre((now) => ({ id, nonce: (now?.nonce ?? 0) + 1 }));
  };
  const toggleKind = (kind: string) => setHidden((now) => (now.has(kind) ? new Set([...now].filter((k) => k !== kind)) : new Set([...now, kind])));

  const actions: EntityActions = {
    onClose: () => setSelection(null),
    onSelectNode: show,
    onSelectEdge: (id) => setSelection({ type: "edge", id }),
    onExpand: expand,
    onFocus: focusOn,
    expanded: (id) => expanded.includes(id) || id === focus,
  };

  const graphMissing = overview.status === "success" && overview.data.graph === null;
  const noEntities = focus === null && overview.status === "success" && overview.data.graph !== null && overview.data.total_nodes === 0;
  const focusName = focus ? (names.get(focus)?.name ?? focus) : null;
  const canvas =
    base.status === "loading" ? (
      <CanvasState>
        <p className="text-sm text-muted-foreground" role="status">
          {t("loading")}
        </p>
      </CanvasState>
    ) : base.status === "error" ? (
      <CanvasState>
        <ApiErrorState error={base.error} onRetry={base.retry} />
      </CanvasState>
    ) : graphMissing ? (
      <CanvasState>
        <NoGraph />
      </CanvasState>
    ) : noEntities ? (
      <CanvasState>
        <EntitiesEmpty project={project} documents={documents} />
      </CanvasState>
    ) : (
      <>
        <EntityCanvas
          set={linked}
          selectedNode={selection?.type === "node" ? selection.id : null}
          selectedEdge={selection?.type === "edge" ? selection.id : null}
          allEdgeLabels={allLabels}
          centre={centre}
          label={t("canvas", { nodes: linked.nodes.length, edges: linked.edges.length })}
          describedBy={ENTITY_HELP_ID}
          onSelectNode={selectNode}
          onSelectEdge={selectEdge}
          onExpand={expand}
        />
        <p id={ENTITY_HELP_ID} className="sr-only">
          {t("help")}
        </p>
        {linked.nodes.length === 0 ? (
          <CanvasState>
            <p className="max-w-sm text-center text-sm text-muted-foreground">{hidden.size > 0 ? t("allHidden") : t("noRelations")}</p>
          </CanvasState>
        ) : null}
      </>
    );

  const total = base.status === "success" && "total_nodes" in base.data ? base.data.total_nodes : null;
  const totalEdges = base.status === "success" && "total_edges" in base.data ? base.data.total_edges : null;
  const leftOut = base.status === "success" && "left_out" in base.data ? base.data.left_out : 0;
  const toolbar = noEntities || graphMissing ? null : (
    <>
      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        {t("nodes")}
        <select className={SELECT} value={limit} onChange={(event) => onChange({ limit: Number(event.target.value) })} data-testid="kg-entity-limit">
          {LIMITS.map((value) => (
            <option key={value} value={value}>
              {format.number(value)}
            </option>
          ))}
        </select>
      </label>
      {focus ? (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <span id="kg-entity-depth">{t("depth")}</span>
          <Segmented
            label={t("depth")}
            value={String(depth)}
            onChange={(value) => onChange({ depth: Number(value) as Depth })}
            options={DEPTHS.map((value) => ({ value: String(value), label: String(value), testId: `kg-entity-depth-${value}` }))}
          />
        </div>
      ) : null}
      <button
        type="button"
        aria-pressed={allLabels}
        onClick={() => setAllLabels(!allLabels)}
        className={cn(
          "inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-sm border px-2.5 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none max-md:h-11",
          allLabels ? "border-border-strong bg-card text-foreground" : "bg-muted text-muted-foreground hover:text-foreground",
        )}
        data-testid="kg-entity-edge-labels"
      >
        <Tags className="size-3.5" aria-hidden="true" />
        {t("edgeLabels")}
      </button>
      {leftOut > 0 || (total !== null && total > working.nodes.length) ? (
        <span className="text-xs text-muted-foreground" data-testid="kg-entity-truncated">
          {leftOut > 0 ? t("leftOut", { count: leftOut }) : t("truncated", { shown: working.nodes.length, total: total ?? 0 })}
        </span>
      ) : null}
    </>
  );

  const inspector =
    selection?.type === "node" ? (
      <EntityInspector key={selection.id} project={project} id={selection.id} actions={actions} />
    ) : selection?.type === "edge" ? (
      <RelationInspector key={selection.id} project={project} id={selection.id} actions={actions} />
    ) : null;

  return (
    <Workspace
      scope="entities"
      search={
        <EntitySearch
          project={project}
          nodes={working.nodes}
          kinds={kinds.map((row) => row.kind)}
          onPickDrawn={show}
          onPickOther={focusOn}
        />
      }
      rail={
        <>
          <EntityStats shown={working.nodes.length} relations={working.edges.length} total={total} totalRelations={totalEdges} />
          <TypeFilter kinds={kinds} hidden={hidden} onToggle={toggleKind} onShowAll={() => setHidden(new Set())} />
          <ExpandedEntities
            nodes={expanded.map((id) => names.get(id) ?? { id, kind: "Other", name: id })}
            onPick={show}
            onCollapse={(id) => setExpanded((now) => now.filter((other) => other !== id))}
          />
          <IsolatedList nodes={isolated} selected={selection?.type === "node" ? selection.id : null} onPick={(id) => setSelection({ type: "node", id })} />
        </>
      }
      trail={
        <Trail
          crumbs={[
            { label: t("title"), onClick: focus ? () => onChange({ focus: null }) : undefined },
            ...(focusName ? [{ label: focusName }] : []),
          ]}
        />
      }
      toolbar={toolbar}
      canvas={canvas}
      status={reading ? t("reading") : null}
      legend={
        kinds.length > 0 && !noEntities ? (
          <details className="group pointer-events-auto max-w-[min(22rem,calc(100%-4.5rem))] rounded-md border bg-card/95 text-xs shadow-raised" data-testid="kg-entity-legend">
            <summary className="flex cursor-pointer list-none items-center gap-2 px-2.5 py-1.5 text-muted-foreground select-none max-md:min-h-11 [&::-webkit-details-marker]:hidden">
              <span className="font-medium text-foreground">{t("legend")}</span>
              <span className="whitespace-nowrap tabular-nums">{t("counts", { nodes: linked.nodes.length, edges: linked.edges.length })}</span>
            </summary>
            <div className="flex flex-col gap-2 border-t px-2.5 py-2 text-muted-foreground">
              <ul className="flex flex-wrap gap-x-3 gap-y-1">
                {kinds.map(({ kind }) => (
                  <li key={kind} className={cn("inline-flex items-center gap-1.5", hidden.has(kind) && "line-through opacity-60")}>
                    <TypeDot kind={kind} />
                    {kind}
                  </li>
                ))}
              </ul>
              <p>{t("legendSize")}</p>
              <p>{t("help")}</p>
            </div>
          </details>
        ) : null
      }
      inspector={inspector}
      announcement={selection?.type === "node" && names.get(selection.id) ? t("announce", { name: names.get(selection.id)?.name ?? "", kind: names.get(selection.id)?.kind ?? "" }) : ""}
    />
  );
}
