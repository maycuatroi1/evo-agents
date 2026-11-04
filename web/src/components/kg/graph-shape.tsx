"use client";

import { useFormatter, useTranslations } from "next-intl";
import { useId, useMemo, useState } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import { kgShapeQuery } from "@/lib/kg/queries";
import type { ShapeRow } from "@/lib/kg/types";

import { KindBadge } from "./badges";
import { NoGraph } from "./no-graph";

/** Rows the section shows before Show all: the combinations with the most edges. */
export const SHAPE_SHOWN = 10;

function ShapeTable({ rows, caption, id }: { rows: ShapeRow[]; caption: string; id?: string }) {
  const t = useTranslations("kg.shape");
  const format = useFormatter();
  const most = Math.max(1, ...rows.map((row) => row.count));
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
        meta: { numeric: true },
        // The bar is for the eye, against the largest count; the figure says the number.
        cell: (info) => (
          <span className="inline-flex items-center justify-end gap-2.5">
            <span className="hidden h-1.5 w-24 overflow-hidden rounded-full bg-muted sm:block" aria-hidden="true">
              <span
                className="block h-full min-w-0.5 rounded-full bg-chart-1"
                style={{ width: `${(info.getValue() / most) * 100}%` }}
              />
            </span>
            <span className="min-w-[4.5rem] tabular-nums">{format.number(info.getValue())}</span>
          </span>
        ),
      }),
    ]);
  }, [t, format, most]);
  return (
    <div id={id}>
      <DataTable
        data={rows}
        columns={columns}
        caption={caption}
        getRowId={(row) => `${row.src_kind}\u0000${row.rel}\u0000${row.dst_kind}`}
        initialSorting={[{ id: "count", desc: true }]}
        density="compact"
        testId="kg-shape-table"
      />
    </div>
  );
}

/** The shape's rows, the combinations with the most edges first, then Show all for the rest. */
function ShapeRows({ rows }: { rows: ShapeRow[] }) {
  const t = useTranslations("kg.shape");
  const [all, setAll] = useState(false);
  const id = useId();
  const sorted = useMemo(() => [...rows].sort((a, b) => b.count - a.count), [rows]);
  const shown = all ? sorted : sorted.slice(0, SHAPE_SHOWN);
  return (
    <div className="flex flex-col gap-3">
      <ShapeTable rows={shown} caption={t("caption", { count: rows.length })} id={id} />
      {rows.length > SHAPE_SHOWN ? (
        <Button
          variant="outline"
          className="self-start"
          aria-expanded={all}
          aria-controls={id}
          onClick={() => setAll((value) => !value)}
          data-testid="kg-shape-all"
        >
          {all ? t("showFewer") : t("showAll", { count: rows.length })}
        </Button>
      ) : null}
    </div>
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
                <ShapeRows rows={data.shape} />
              )
            }
          </QueryView>
        </CardContent>
      </Card>
    </section>
  );
}
