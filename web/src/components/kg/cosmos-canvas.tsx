"use client";

import type { Graph } from "@cosmos.gl/graph";
import { Maximize } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { centroids, clusterCentres, clusterColour, rgba, startPositions } from "@/lib/kg/map";

/**
 * Points and links drawn with cosmos.gl on WebGL2, the library loaded only once the canvas mounts (a dynamic import),
 * so no page pays for it before it draws. Each point is coloured by its cluster and pulled towards it, the cluster's
 * label sits at the centre of its points and follows the simulation and the zoom, and the cluster `highlight` names
 * is lit while the others grey out. A canvas says nothing to a screen reader: the page lists the same data in a
 * table beside it. Without WebGL2 the canvas is not drawn and `onUnsupported` is called once.
 */
export type CosmosData = {
  /** One id per point. */
  ids: readonly string[];
  /** The cluster index of each point; undefined outside any cluster. */
  clusterOf: readonly (number | undefined)[];
  /** The number of clusters, and the label drawn at each one's centre (empty for none). */
  labels: readonly string[];
  /** Links as pairs of point indexes, flat. */
  links: readonly number[];
};

const SPACE = 4096;

export function webgl2Supported(): boolean {
  try {
    return document.createElement("canvas").getContext("webgl2") !== null;
  } catch {
    return false;
  }
}

function pointColours(data: CosmosData): Float32Array {
  const out = new Float32Array(data.ids.length * 4);
  data.clusterOf.forEach((cluster, i) => {
    out.set(cluster === undefined ? [0.55, 0.55, 0.58, 0.8] : rgba(clusterColour(cluster), 0.95), i * 4);
  });
  return out;
}

function highlighted(data: CosmosData, cluster: number | null): number[] | undefined {
  if (cluster === null) return undefined;
  const out: number[] = [];
  data.clusterOf.forEach((c, i) => {
    if (c === cluster) out.push(i);
  });
  return out;
}

export function CosmosCanvas({
  data,
  highlight = null,
  onPointClick,
  onUnsupported,
  label,
  testId,
  height = "h-[480px]",
}: {
  data: CosmosData;
  highlight?: number | null;
  onPointClick?: (index: number) => void;
  onUnsupported?: () => void;
  label: string;
  testId: string;
  height?: string;
}) {
  const t = useTranslations("kg.map");
  const container = useRef<HTMLDivElement>(null);
  const overlay = useRef<HTMLDivElement>(null);
  const graph = useRef<Graph | null>(null);
  const click = useRef(onPointClick);
  const [ready, setReady] = useState(false);
  const centres = useMemo(() => clusterCentres(data.labels.length, SPACE), [data.labels.length]);

  useEffect(() => {
    click.current = onPointClick;
  }, [onPointClick]);

  useEffect(() => {
    const div = container.current;
    if (!div) return;
    if (!webgl2Supported()) {
      onUnsupported?.();
      return;
    }
    let cancelled = false;
    let frame = 0;
    const place = () => {
      frame = 0;
      const current = graph.current;
      const labels = overlay.current;
      if (!current || !labels) return;
      const centre = centroids(current.getPointPositions(), data.clusterOf, data.labels.length);
      for (const child of Array.from(labels.children) as HTMLElement[]) {
        const at = centre[Number(child.dataset.cluster)];
        if (!at) {
          child.style.display = "none";
          continue;
        }
        const [x, y] = current.spaceToScreenPosition(at);
        child.style.display = "";
        child.style.transform = `translate(${x}px, ${y}px) translate(-50%, -50%)`;
      }
    };
    const schedule = () => {
      if (!frame) frame = requestAnimationFrame(place);
    };
    void import("@cosmos.gl/graph").then(({ Graph: CosmosGraph }) => {
      if (cancelled) return;
      const instance = new CosmosGraph(div, {
        spaceSize: SPACE,
        backgroundColor: [0, 0, 0, 0],
        pointDefaultSize: 4,
        pointGreyoutOpacity: 0.12,
        linkDefaultWidth: 0.6,
        linkOpacity: 0.35,
        linkGreyoutOpacity: 0.04,
        simulationCluster: 0.4,
        simulationRepulsion: 1.2,
        simulationLinkSpring: 0.6,
        simulationGravity: 0.15,
        fitViewOnInit: true,
        fitViewDelay: 800,
        randomSeed: 7,
        attribution: "",
        onPointClick: (index) => click.current?.(index),
        onSimulationTick: schedule,
        onSimulationEnd: schedule,
        onZoom: schedule,
      });
      graph.current = instance;
      instance.setPointPositions(startPositions(data.ids, data.clusterOf, centres, SPACE / 40));
      instance.setPointColors(pointColours(data));
      instance.setLinks(new Float32Array(data.links));
      instance.setPointClusters([...data.clusterOf]);
      instance.setClusterPositions(centres);
      instance.render();
      setReady(true);
      schedule();
    });
    return () => {
      cancelled = true;
      if (frame) cancelAnimationFrame(frame);
      graph.current?.destroy();
      graph.current = null;
      setReady(false);
    };
  }, [data, centres, onUnsupported]);

  useEffect(() => {
    graph.current?.setConfigPartial({ highlightedPointIndices: highlighted(data, highlight) });
  }, [data, highlight, ready]);

  return (
    <div className={`relative w-full overflow-hidden rounded-md border bg-muted/20 ${height}`} data-testid={testId}>
      <div ref={container} className="absolute inset-0" role="img" aria-label={label} />
      <div ref={overlay} className="pointer-events-none absolute inset-0" aria-hidden="true">
        {data.labels.map((text, index) =>
          text ? (
            <span
              key={index}
              data-cluster={index}
              className="absolute top-0 left-0 max-w-48 truncate rounded border-l-4 bg-background/85 px-1.5 py-0.5 text-xs font-medium text-foreground shadow-sm"
              style={{ display: "none", borderLeftColor: clusterColour(index) }}
            >
              {text}
            </span>
          ) : null,
        )}
      </div>
      {ready ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="absolute top-2 right-2"
          onClick={() => graph.current?.fitView(400)}
        >
          <Maximize aria-hidden="true" />
          {t("fit")}
        </Button>
      ) : (
        <Skeleton className="absolute inset-0" aria-label={t("loading")} />
      )}
    </div>
  );
}
