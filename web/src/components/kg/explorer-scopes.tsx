"use client";

import { FileQuestion, Network } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useCallback, useMemo, useState } from "react";

import { type HubQueryState, useHubQuery } from "@/components/states/query-view";
import { ApiErrorState, EmptyState, StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import {
  clusterElements,
  kindsOfMembers,
  linkedClusters,
  OVERVIEW,
  overviewElements,
  type Scope,
  type Selection,
  shownSize,
} from "@/lib/kg/explorer";
import { clusterColour, clusterIndex } from "@/lib/kg/map";
import { kgClusterQuery } from "@/lib/kg/queries";
import type { ClusterMap, ClusterSummary, Relation } from "@/lib/kg/types";

import { ExplorerCanvas } from "./explorer-canvas";
import { HELP_ID, Legend, Trail, Workspace } from "./explorer-frame";
import type { ExplorerLayout } from "./explorer-layout";
import { ClusterInspector, type NodeActions, NodeInspector } from "./explorer-inspector";
import { ClusterList, type ClusterRow } from "./explorer-rail";
import { NoGraph } from "./no-graph";

export type ScopeProps = {
  project: string;
  hiddenKinds: ReadonlySet<string>;
  layout: ExplorerLayout;
  onLayout: (layout: ExplorerLayout) => void;
  /** The rail's search, and its sections every scope shares (the graph's figures and kinds), given the kinds it draws. */
  search: ReactNode;
  shared: (present: ReadonlySet<string> | null) => ReactNode;
  navigate: (scope: Scope) => void;
  /** The overview's clusters, which the other scopes read for colours and names. */
  summary: HubQueryState<ClusterMap>;
};

/** A state in place of the canvas: loading, an error, nothing to draw. */
export function CanvasState({ children }: { children: ReactNode }) {
  return <div className="absolute inset-0 flex items-center justify-center overflow-y-auto bg-background/80 p-6">{children}</div>;
}

function useColours(summary: HubQueryState<ClusterMap>) {
  const data = summary.status === "success" ? summary.data : null;
  const clusters = useMemo(() => data?.clusters ?? [], [data]);
  const index = useMemo(() => clusterIndex(clusters), [clusters]);
  return {
    clusters,
    colour: (id: string | null | undefined) => {
      const at = id ? index.get(id) : undefined;
      return at === undefined ? undefined : clusterColour(at);
    },
  };
}

function rowsOf(list: readonly Pick<ClusterSummary, "id" | "label" | "size">[], colour: (id: string) => string | undefined): ClusterRow[] {
  return list.map((cluster) => ({ id: cluster.id, label: cluster.label, size: cluster.size, colour: colour(cluster.id) }));
}

export function NotFound({ text, navigate }: { text: string; navigate: (scope: Scope) => void }) {
  const t = useTranslations("kg.explorer");
  return (
    <StatePanel icon={FileQuestion} title={text} testId="state-not-found">
      <Button type="button" variant="outline" className="cursor-pointer" onClick={() => navigate(OVERVIEW)}>
        {t("backToOverview")}
      </Button>
    </StatePanel>
  );
}

// -- Overview ------------------------------------------------------------------------------------------------------

export function OverviewScope({ project, hiddenKinds, layout, onLayout, search, shared, navigate, summary }: ScopeProps) {
  const t = useTranslations("kg.explorer");
  const [selection, setSelection] = useState<Selection>(null);
  const [pointed, setPointed] = useState<string | null>(null);
  const { clusters, colour } = useColours(summary);
  const map = summary.status === "success" ? summary.data : null;
  const elements = useMemo(() => (map ? overviewElements(map.clusters, map.links, hiddenKinds) : []), [map, hiddenKinds]);
  const selected = selection?.type === "cluster" ? clusters.find((cluster) => cluster.id === selection.id) : undefined;
  const open = useCallback((id: string) => navigate({ kind: "cluster", id }), [navigate]);
  const pick = useCallback((id: string | null) => setSelection(id ? { type: "cluster", id } : null), []);
  const rows = clusters.map((cluster) => ({ ...rowsOf([cluster], colour)[0], size: shownSize(cluster, hiddenKinds) }));
  const canvas =
    summary.status === "loading" ? null : summary.status === "error" ? (
      <CanvasState>
        <ApiErrorState error={summary.error} onRetry={summary.retry} />
      </CanvasState>
    ) : summary.data.graph === null ? (
      <CanvasState>
        <NoGraph />
      </CanvasState>
    ) : clusters.length === 0 ? (
      <CanvasState>
        <EmptyState icon={Network} title={t("noClustersTitle")} description={t("noClusters", { reason: summary.data.reason ?? "" })} />
      </CanvasState>
    ) : (
      <ExplorerCanvas
        scope={OVERVIEW}
        elements={elements}
        selected={selected?.id ?? null}
        highlight={pointed}
        layout={layout}
        root={clusters[0]?.id ?? null}
        label={t("canvasOverview", { count: clusters.length })}
        describedBy={HELP_ID}
        onSelect={pick}
        onActivate={open}
        onBack={() => setSelection(null)}
      />
    );
  return (
    <Workspace
      scope="overview"
      search={search}
      rail={
        <>
          {shared(map ? new Set(clusters.flatMap((cluster) => Object.keys(cluster.kinds))) : null)}
          <ClusterList title={t("rail.clusters")} rows={rows} selected={selected?.id ?? null} onSelect={pick} onOpen={open} onHover={setPointed} testId="kg-clusters" />
        </>
      }
      trail={<Trail crumbs={[{ label: t("overview") }]} />}
      layout={layout}
      onLayout={onLayout}
      canvas={canvas}
      status={summary.status === "loading" ? t("loading") : null}
      legend={<Legend scope="overview" kinds={[]} counts={t("countsOverview", { clusters: clusters.length, links: map?.links.length ?? 0 })} />}
      inspector={
        selected ? (
          <ClusterInspector
            project={project}
            cluster={selected}
            colour={colour(selected.id)}
            inScope={false}
            parts={[]}
            linked={linkedClusters(selected.id, clusters, map?.links ?? []).map(({ cluster, weight }) => ({
              ...rowsOf([cluster], colour)[0],
              weight,
            }))}
            onExplore={open}
            onPickCluster={pick}
            onClose={() => setSelection(null)}
          />
        ) : null
      }
      announcement={selected ? t("announceCluster", { name: selected.label, count: selected.size }) : ""}
    />
  );
}

// -- Cluster -------------------------------------------------------------------------------------------------------

export function ClusterScope({ id, project, hiddenKinds, layout, onLayout, search, shared, navigate, summary }: ScopeProps & { id: string }) {
  const t = useTranslations("kg.explorer");
  const [selection, setSelection] = useState<Selection | undefined>(undefined);
  const [pointed, setPointed] = useState<string | null>(null);
  const { clusters, colour } = useColours(summary);
  const detail = useHubQuery(kgClusterQuery(browserApi, project, id), null);
  const data = detail.status === "success" ? detail.data : null;
  const drawing = useMemo(() => (data ? clusterElements(data, hiddenKinds) : null), [data, hiddenKinds]);
  const self = data?.cluster;
  const parent = self?.parent ? clusters.find((cluster) => cluster.id === self.parent) : undefined;
  const effective: Selection = selection === undefined ? { type: "cluster", id } : selection;
  const part = effective?.type === "cluster" ? data?.subclusters.find((sub) => sub.id === effective.id) : undefined;
  const other = effective?.type === "cluster" && effective.id !== id && !part ? clusters.find((c) => c.id === effective.id) : undefined;
  const partIds = useMemo(() => new Set((data?.subclusters ?? []).map((sub) => sub.id)), [data]);
  const select = useCallback(
    (next: string | null) => setSelection(next === null ? null : partIds.has(next) ? { type: "cluster", id: next } : { type: "node", id: next }),
    [partIds],
  );
  const activate = useCallback(
    (target: string) => navigate(partIds.has(target) ? { kind: "cluster", id: target } : { kind: "node", id: target }),
    [partIds, navigate],
  );
  const up = () => navigate(parent ? { kind: "cluster", id: parent.id } : OVERVIEW);
  const parts = rowsOf(data?.subclusters ?? [], () => colour(self?.parent ?? id));
  const linked = (data?.neighbours ?? []).map((n) => ({ id: n.id, label: n.label, size: n.size, weight: n.weight, colour: colour(n.id) }));
  const nodeActions: NodeActions = {
    onExpand: (node) => navigate({ kind: "node", id: node }),
    onCollapse: () => {},
    onHide: () => {},
    onExplore: (node) => navigate({ kind: "node", id: node }),
    onRelation: (relation: Relation) => navigate({ kind: "node", id: relation.node }),
    onClose: () => setSelection(null),
  };
  const shown = self ?? clusters.find((cluster) => cluster.id === id);
  const canvas =
    detail.status === "loading" ? null : detail.status === "error" ? (
      <CanvasState>
        {detail.error.status === 404 ? <NotFound text={t("clusterNotFound")} navigate={navigate} /> : <ApiErrorState error={detail.error} onRetry={detail.retry} />}
      </CanvasState>
    ) : (
      <ExplorerCanvas
        scope={{ kind: "cluster", id }}
        elements={drawing?.elements ?? []}
        selected={effective && effective.id !== id ? effective.id : null}
        highlight={pointed}
        layout={layout}
        root={null}
        label={t("canvasCluster", { name: shown?.label ?? id, count: drawing?.drawn ?? 0 })}
        describedBy={HELP_ID}
        onSelect={select}
        onActivate={activate}
        onBack={up}
      />
    );
  const inspector = !data ? null : effective?.type === "node" ? (
    <NodeInspector key={effective.id} project={project} id={effective.id} actions={nodeActions} />
  ) : effective?.type === "cluster" && (effective.id === id || part || other) ? (
    <ClusterInspector
      project={project}
      cluster={effective.id === id ? data.cluster : (part ?? other)!}
      colour={colour(effective.id === id || part ? (data.cluster.parent ?? id) : effective.id)}
      parentLabel={effective.id === id ? parent?.label : part ? data.cluster.label : undefined}
      inScope={effective.id === id}
      parts={effective.id === id ? parts : []}
      linked={effective.id === id ? linked : []}
      onExplore={(target) => navigate({ kind: "cluster", id: target })}
      onPickCluster={(target) => setSelection({ type: "cluster", id: target })}
      onClose={() => setSelection(null)}
    />
  ) : null;
  return (
    <Workspace
      scope="cluster"
      search={search}
      rail={
        <>
          {shared(data ? new Set(data.members.map((member) => member.kind)) : null)}
          <ClusterList title={t("rail.parts")} rows={parts} selected={effective?.id ?? null} onSelect={(target) => setSelection({ type: "cluster", id: target })} onOpen={(target) => navigate({ kind: "cluster", id: target })} onHover={setPointed} testId="kg-parts" />
          <ClusterList title={t("rail.linked")} rows={linked} selected={effective?.id ?? null} onSelect={(target) => setSelection({ type: "cluster", id: target })} onOpen={(target) => navigate({ kind: "cluster", id: target })} onHover={() => {}} testId="kg-linked" />
        </>
      }
      trail={
        <Trail
          crumbs={[
            { label: t("overview"), onClick: () => navigate(OVERVIEW) },
            ...(parent ? [{ label: parent.label, onClick: () => navigate({ kind: "cluster", id: parent.id }) }] : []),
            { label: shown?.label ?? id },
          ]}
        />
      }
      layout={layout}
      onLayout={onLayout}
      canvas={canvas}
      status={detail.status === "loading" ? t("loading") : null}
      legend={
        <Legend
          scope="cluster"
          circles={drawing?.mode === "parts"}
          kinds={kindsOfMembers(data?.members ?? []).map((k) => k.kind)}
          counts={
            drawing?.mode === "parts"
              ? t("countsParts", { parts: drawing.drawn, total: drawing.total })
              : t("countsCluster", { drawn: drawing?.drawn ?? 0, total: drawing?.total ?? 0 })
          }
        />
      }
      inspector={inspector}
      announcement={effective?.type === "cluster" && self && effective.id === id ? t("announceCluster", { name: self.label, count: self.size }) : ""}
    />
  );
}
