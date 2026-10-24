"use client";

import { Boxes, Network } from "lucide-react";
import dynamic from "next/dynamic";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import { QueryView, useHubQuery } from "@/components/states/query-view";
import { TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { clusterIndex } from "@/lib/kg/map";
import { kgClustersQuery, kgWholeQuery } from "@/lib/kg/queries";
import { clusterHref, KG_MAP_ID } from "@/lib/kg/routes";
import type { ClusterMap, WholeGraph } from "@/lib/kg/types";

import { ClusterMatrix, ClusterTable } from "./cluster-table";
import type { CosmosData } from "./cosmos-canvas";
import { NoGraph } from "./node-search";

// cosmos.gl and luma.gl load only when a map is drawn: imported statically they would sit in the page's first load.
const CosmosCanvas = dynamic(() => import("./cosmos-canvas").then((module) => module.CosmosCanvas), {
  ssr: false,
  loading: () => <Skeleton className="h-[480px] w-full rounded-md" />,
});

type Mode = "clusters" | "whole";

function mapData(map: ClusterMap): CosmosData {
  const index = clusterIndex(map.clusters);
  return {
    ids: map.nodes.map((node) => node.id),
    clusterOf: map.nodes.map((node) => index.get(node.cluster)),
    labels: map.clusters.map((cluster) => cluster.label),
    links: map.edges.flatMap(([a, b]) => [a ?? 0, b ?? 0]),
  };
}

function wholeData(map: ClusterMap, whole: WholeGraph): CosmosData {
  const index = clusterIndex(map.clusters);
  return {
    ids: whole.nodes.map((node) => node.id),
    clusterOf: whole.nodes.map((node) => (node.cluster ? index.get(node.cluster) : undefined)),
    labels: map.clusters.map((cluster) => cluster.label),
    links: whole.edges.flatMap(([a, b]) => [a ?? 0, b ?? 0]),
  };
}

/** The focus of the URL (`?focus=<node id>`), set by a search result's "Show on map", and a way to clear it. */
function useFocus(): [string, () => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const focus = params.get("focus") ?? "";
  const clear = () => {
    const search = new URLSearchParams(params.toString());
    search.delete("focus");
    const text = search.toString();
    window.history.replaceState(null, "", `${pathname}${text ? `?${text}` : ""}`);
  };
  return [focus, clear];
}

function WholeCanvas({ project, map, onUnsupported }: { project: string; map: ClusterMap; onUnsupported: () => void }) {
  const t = useTranslations("kg.map");
  const whole = useHubQuery(kgWholeQuery(browserApi, project), null);
  return (
    <QueryView state={whole} loading={<Skeleton className="h-[480px] w-full rounded-md" />}>
      {(data) => (
        <div className="flex flex-col gap-2">
          <p className="text-sm text-muted-foreground" data-testid="kg-whole-summary">
            {t("wholeSummary", { nodes: data.nodes.length, edges: data.edges.length })}
          </p>
          {data.truncated ? <p className="text-sm text-muted-foreground">{t("truncated", { count: data.nodes.length })}</p> : null}
          <CosmosCanvas data={wholeData(map, data)} label={t("canvasLabel")} testId="kg-whole-canvas" onUnsupported={onUnsupported} />
        </div>
      )}
    </QueryView>
  );
}

function MapBody({ project, map }: { project: string; map: ClusterMap }) {
  const t = useTranslations("kg.map");
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("clusters");
  const [webgl, setWebgl] = useState(true);
  const [focusId, clearFocus] = useFocus();
  const [focusCleared, setFocusCleared] = useState(false);
  const data = useMemo(() => mapData(map), [map]);
  const focusNode = focusCleared ? "" : focusId;
  const focus = useMemo(() => {
    if (!focusNode) return null;
    const node = map.nodes.find((n) => n.id === focusNode);
    return node ? (clusterIndex(map.clusters).get(node.cluster) ?? null) : null;
  }, [map, focusNode]);
  const unsupported = useCallback(() => setWebgl(false), []);
  const open = useCallback(
    (point: number) => {
      const cluster = data.clusterOf[point];
      const id = cluster === undefined ? undefined : map.clusters[cluster]?.id;
      if (id) router.push(clusterHref(project, id));
    },
    [data, map, project, router],
  );
  const nodes = map.nodes.length;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground" data-testid="kg-map-summary">
          {t("summary", { clusters: map.clusters.length, nodes, links: map.links.length })}
        </p>
        {webgl ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setMode(mode === "whole" ? "clusters" : "whole")}
            data-testid="kg-map-mode"
          >
            {mode === "whole" ? <Boxes aria-hidden="true" /> : <Network aria-hidden="true" />}
            {mode === "whole" ? t("clusters") : t("whole")}
          </Button>
        ) : null}
      </div>
      {focusNode ? (
        <p className="flex flex-wrap items-center gap-2 text-sm" role="status" data-testid="kg-map-focus">
          {focus === null ? t("focusMissing", { node: focusNode }) : t("focus", { node: focusNode })}
          <Button
            type="button"
            variant="link"
            size="sm"
            className="h-auto p-0"
            onClick={() => {
              setFocusCleared(true);
              clearFocus();
            }}
          >
            {t("clearFocus")}
          </Button>
        </p>
      ) : null}
      {!webgl ? (
        <p className="text-sm text-muted-foreground" data-testid="kg-map-no-webgl">
          {t("noWebgl")}
        </p>
      ) : mode === "whole" ? (
        <WholeCanvas project={project} map={map} onUnsupported={unsupported} />
      ) : (
        <CosmosCanvas
          data={data}
          highlight={focus}
          onPointClick={open}
          onUnsupported={unsupported}
          label={t("canvasLabel")}
          testId="kg-map-canvas"
        />
      )}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] lg:items-start">
        <ClusterTable project={project} clusters={map.clusters} focus={focus} />
        <ClusterMatrix clusters={map.clusters} links={map.links} focus={focus} />
      </div>
    </div>
  );
}

/**
 * The cluster map of /p/{project}/kg (GET /v1/kg/{project}/clusters): the clusters drawn with cosmos.gl, coloured by
 * cluster with each label at its cluster's centre, a click opening the cluster's page; a button for every node and
 * edge; the clusters as a table and the links between them as a matrix. Without WebGL2 the table and the matrix stay,
 * with a line saying why.
 */
export function ClusterMapSection({ project }: { project: string }) {
  const t = useTranslations("kg.map");
  const clusters = useHubQuery(kgClustersQuery(browserApi, project), null);
  return (
    <section aria-labelledby="kg-map-title" id={KG_MAP_ID} className="scroll-mt-20">
      <Card data-testid="kg-map">
        <CardHeader>
          <CardTitle>
            <h2 id="kg-map-title">{t("title")}</h2>
          </CardTitle>
          <CardDescription>{t("description")}</CardDescription>
        </CardHeader>
        <CardContent>
          <QueryView state={clusters} loading={<TableSkeleton rows={4} />}>
            {(data) =>
              data.graph === null ? (
                <NoGraph />
              ) : data.clusters.length === 0 ? (
                <p className="text-sm text-muted-foreground" data-testid="kg-map-empty">
                  {t("noClusters", { reason: data.reason ?? "" })}
                </p>
              ) : (
                <MapBody project={project} map={data} />
              )
            }
          </QueryView>
        </CardContent>
      </Card>
    </section>
  );
}

