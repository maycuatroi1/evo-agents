"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { clusterColour, clusterMatrix, type Matrix } from "@/lib/kg/map";
import { clusterHref } from "@/lib/kg/routes";
import type { ClusterLink, ClusterSummary } from "@/lib/kg/types";
import { cn } from "@/lib/utils";

export function kindsText(kinds: Record<string, number>): string {
  return Object.entries(kinds)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([kind, count]) => `${kind} ${count}`)
    .join(", ");
}

export function Swatch({ index }: { index: number }) {
  return (
    <span
      aria-hidden="true"
      className="inline-block size-2.5 shrink-0 rounded-full"
      style={{ backgroundColor: clusterColour(index) }}
    />
  );
}

type Row = ClusterSummary & { index: number };

/**
 * The clusters as a table, the same data the map draws, for screen readers and for a browser without WebGL2: each
 * label opens the cluster's page; the cluster lit up on the map is marked.
 */
export function ClusterTable({
  project,
  clusters,
  focus,
  testId = "kg-cluster-table",
}: {
  project: string;
  clusters: ClusterSummary[];
  /** The index of the cluster lit up, or null. */
  focus?: number | null;
  testId?: string;
}) {
  const t = useTranslations("kg.map.table");
  const rows = useMemo<Row[]>(() => clusters.map((cluster, index) => ({ ...cluster, index })), [clusters]);
  const columns = useMemo(() => {
    const helper = dataTableColumns<Row>();
    return helper.columns([
      helper.accessor("label", {
        header: () => t("label"),
        sortFn: "text",
        cell: (info) => (
          <div className="flex min-w-0 items-center gap-2">
            <Swatch index={info.row.original.index} />
            <Link
              href={clusterHref(project, info.row.original.id)}
              className={cn(NAME_LINK, "break-words whitespace-normal", focus === info.row.original.index && "font-semibold")}
              aria-current={focus === info.row.original.index ? "true" : undefined}
              data-testid="kg-cluster-link"
              data-cluster-id={info.row.original.id}
            >
              {info.getValue() || info.row.original.id}
            </Link>
          </div>
        ),
      }),
      helper.accessor("size", {
        header: () => t("size"),
        sortFn: "basic",
        cell: (info) => <span className="tabular-nums">{info.getValue()}</span>,
      }),
      helper.accessor((row) => kindsText(row.kinds), {
        id: "kinds",
        header: () => t("kinds"),
        sortFn: "text",
        cell: (info) => <span className="text-xs text-muted-foreground">{info.getValue()}</span>,
      }),
      helper.accessor("subclusters", {
        header: () => t("subclusters"),
        sortFn: "basic",
        cell: (info) => <span className="tabular-nums">{info.getValue()}</span>,
      }),
    ]);
  }, [t, project, focus]);
  return (
    <DataTable
      data={rows}
      columns={columns}
      caption={t("caption", { count: rows.length })}
      getRowId={(row) => row.id}
      density="compact"
      columnClassNames={{ kinds: "hidden md:table-cell", subclusters: "hidden sm:table-cell" }}
      testId={testId}
    />
  );
}

const CELL = 18;
const GUTTER = 6;

function MatrixSvg({ matrix, focus }: { matrix: Matrix; focus: number | null }) {
  const t = useTranslations("kg.map.matrix");
  const n = matrix.clusters.length;
  const side = GUTTER + n * CELL;
  const title = (row: number, col: number, weight: number) =>
    t("cell", { a: matrix.clusters[row]?.label ?? "", b: matrix.clusters[col]?.label ?? "", count: weight });
  return (
    <svg
      viewBox={`0 0 ${side} ${side}`}
      width={side}
      height={side}
      className="max-w-full"
      role="img"
      aria-label={t("title")}
      data-testid="kg-cluster-matrix"
    >
      {matrix.clusters.map((cluster, i) => (
        <g key={cluster.id}>
          <rect x={0} y={GUTTER + i * CELL} width={GUTTER - 2} height={CELL - 2} fill={clusterColour(i)} />
          <rect x={GUTTER + i * CELL} y={0} width={CELL - 2} height={GUTTER - 2} fill={clusterColour(i)} />
          <rect
            x={GUTTER + i * CELL}
            y={GUTTER + i * CELL}
            width={CELL - 2}
            height={CELL - 2}
            className="fill-muted"
            stroke={focus === i ? "currentColor" : undefined}
          >
            <title>{cluster.label}</title>
          </rect>
        </g>
      ))}
      {matrix.cells.map((cell) => (
        <rect
          key={`${cell.row}-${cell.col}`}
          x={GUTTER + cell.col * CELL}
          y={GUTTER + cell.row * CELL}
          width={CELL - 2}
          height={CELL - 2}
          className="fill-brand"
          fillOpacity={0.15 + 0.85 * (matrix.max ? cell.weight / matrix.max : 0)}
          stroke={focus !== null && (cell.row === focus || cell.col === focus) ? "currentColor" : undefined}
          data-testid="kg-matrix-cell"
        >
          <title>{title(cell.row, cell.col, cell.weight)}</title>
        </rect>
      ))}
    </svg>
  );
}

/** The cluster-by-cluster matrix in SVG: a cell for each pair of clusters edges join, darker for more edges. */
export function ClusterMatrix({
  clusters,
  links,
  focus = null,
}: {
  clusters: ClusterSummary[];
  links: ClusterLink[];
  focus?: number | null;
}) {
  const t = useTranslations("kg.map.matrix");
  const matrix = useMemo(() => clusterMatrix(clusters, links), [clusters, links]);
  return (
    <section aria-labelledby="kg-matrix-title" className="flex flex-col gap-2">
      <h3 id="kg-matrix-title" className="text-sm font-semibold">
        {t("title")}
      </h3>
      <p className="text-xs text-muted-foreground">{t("description")}</p>
      {matrix.cells.length === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="kg-matrix-empty">
          {t("empty")}
        </p>
      ) : (
        <div className="overflow-x-auto text-foreground">
          <MatrixSvg matrix={matrix} focus={focus !== null && focus < matrix.clusters.length ? focus : null} />
        </div>
      )}
      {matrix.hidden > 0 ? <p className="text-xs text-muted-foreground">{t("hidden", { count: matrix.hidden })}</p> : null}
    </section>
  );
}

