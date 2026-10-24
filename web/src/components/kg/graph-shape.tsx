"use client";

import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { TableSkeleton } from "@/components/states/states";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import { kgShapeQuery } from "@/lib/kg/queries";
import type { ShapeRow } from "@/lib/kg/types";

import { KindBadge } from "./badges";
import { NoGraph } from "./node-search";

function ShapeTable({ rows, caption }: { rows: ShapeRow[]; caption: string }) {
  const t = useTranslations("kg.shape");
  const columns = useMemo(() => {
    const helper = dataTableColumns<ShapeRow>();
    return helper.columns([
      helper.accessor("src_kind", {
        header: () => t("columns.source"),
        sortFn: "text",
        cell: (info) => <KindBadge kind={info.getValue()} />,
      }),
      helper.accessor("rel", {
        header: () => t("columns.rel"),
        sortFn: "text",
        cell: (info) => <span className="font-mono text-xs">{info.getValue()}</span>,
      }),
      helper.accessor("dst_kind", {
        header: () => t("columns.target"),
        sortFn: "text",
        cell: (info) => <KindBadge kind={info.getValue()} />,
      }),
      helper.accessor("count", {
        header: () => t("columns.count"),
        sortFn: "basic",
        cell: (info) => <span className="tabular-nums">{info.getValue()}</span>,
      }),
    ]);
  }, [t]);
  return (
    <DataTable
      data={rows}
      columns={columns}
      caption={caption}
      getRowId={(row) => `${row.src_kind}\u0000${row.rel}\u0000${row.dst_kind}`}
      initialSorting={[{ id: "count", desc: true }]}
      density="compact"
      testId="kg-shape-table"
    />
  );
}

/**
 * The graph's shape: how many edges of each type join each kind of node to each other, counted over the edges and
 * nodes the reader may read (GET /v1/kg/{project}/shape), so it never shows what the graph hides from them.
 */
export function GraphShapeSection({ project }: { project: string }) {
  const t = useTranslations("kg.shape");
  const shape = useHubQuery(kgShapeQuery(browserApi, project), null);
  return (
    <section aria-labelledby="kg-shape-title">
      <Card data-testid="kg-shape">
        <CardHeader>
          <CardTitle>
            <h2 id="kg-shape-title">{t("title")}</h2>
          </CardTitle>
          <CardDescription>{t("description")}</CardDescription>
        </CardHeader>
        <CardContent>
          <QueryView state={shape} loading={<TableSkeleton rows={4} />}>
            {(data) =>
              data.graph === null ? (
                <NoGraph />
              ) : data.shape.length === 0 ? (
                <p className="text-sm text-muted-foreground" data-testid="kg-shape-empty">
                  {t("empty")}
                </p>
              ) : (
                <ShapeTable rows={data.shape} caption={t("caption", { count: data.shape.length })} />
              )
            }
          </QueryView>
        </CardContent>
      </Card>
    </section>
  );
}
