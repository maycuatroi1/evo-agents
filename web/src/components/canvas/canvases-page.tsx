"use client";

import { Plus, RadioTower, Settings, Table } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { signalsHref } from "@/lib/signal-queries";

import { type CanvasSummary, canvasesQuery } from "./api";
import { NewCanvasDialog } from "./canvas-dialogs";
import { ReaderNote } from "./chrome";
import { useCanvasViewer } from "./hooks";
import { canvasHref, settingsHref } from "./links";
import { canWrite, STATUSES, statusCounts } from "./model";
import { StatusBar } from "./status-bar";

const NARROW_HIDDEN = { statuses: "hidden md:table-cell", created_by: "hidden lg:table-cell", updated_at: "hidden sm:table-cell" };

function CanvasesTable({ project, canvases }: { project: string; canvases: CanvasSummary[] }) {
  const t = useTranslations("canvas.list");
  const tStatus = useTranslations("canvas.status");
  const columns = useMemo(() => {
    const helper = dataTableColumns<CanvasSummary>();
    return helper.columns([
      helper.accessor("name", {
        header: () => t("columns.canvas"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const canvas = info.row.original;
          return (
            <CellMain sub={<span className="font-mono">{canvas.slug}</span>} subTitle={canvas.slug}>
              <Link href={canvasHref(project, canvas.slug)} className={`${NAME_LINK} truncate`} data-canvas-link={canvas.slug}>
                {canvas.name}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor("claims", {
        header: () => t("columns.claims"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <span className="tabular-nums">{info.getValue()}</span>,
      }),
      helper.accessor("statuses", {
        header: () => t("columns.statuses"),
        enableSorting: false,
        cell: (info) => <StatusBar counts={statusCounts(info.getValue())} className="w-40" />,
      }),
      helper.accessor("created_by", { header: () => t("columns.createdBy"), cell: (info) => <span>{info.getValue()}</span> }),
      helper.accessor("updated_at", {
        header: () => t("columns.updatedAt"),
        sortFn: "datetime",
        meta: { numeric: true },
        cell: (info) => <When value={info.getValue()} />,
      }),
    ]);
  }, [t, project]);
  return (
    <DataTable
      data={canvases}
      columns={columns}
      caption={t("caption", { project })}
      getRowId={(row) => row.slug}
      columnClassNames={NARROW_HIDDEN}
      testId="canvases-table"
      mobile={(canvas) => {
        const counts = statusCounts(canvas.statuses);
        return {
          title: canvas.name,
          titleText: canvas.name,
          href: canvasHref(project, canvas.slug),
          meta: [
            canvas.slug,
            t("claimsShort", { count: canvas.claims }),
            ...STATUSES.filter((status) => counts[status] > 0).map((status) => `${counts[status]} ${tStatus(status).toLowerCase()}`),
          ].join(", "),
          data: { "canvas-slug": canvas.slug },
        };
      }}
    />
  );
}

/**
 * The Lean Canvases of a project (/p/{project}/x/canvas/canvases, the core plugin canvas; web/DESIGN.md,
 * components/canvas): each product's canvas with its claims counted by status. A writer starts a canvas here; a reader
 * reads. The Signals page, the measures a test card checks, is one link away.
 */
export function CanvasesPage({ project, title, initialError }: { project: string; title: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("canvas.list");
  const state = useHubQuery(canvasesQuery(browserApi, project), initialError);
  const viewer = useCanvasViewer(project);
  const writer = canWrite(viewer);
  const [creating, setCreating] = useState(false);
  const newButton = (
    <Button onClick={() => setCreating(true)} data-testid="canvas-new">
      <Plus aria-hidden="true" />
      {t("new")}
    </Button>
  );
  return (
    <div className="flex flex-col gap-6" data-testid="canvases">
      <PageHeader
        title={title}
        actions={
          <>
            <Button asChild variant="ghost">
              <Link href={signalsHref(project)} data-testid="canvas-signals">
                <RadioTower aria-hidden="true" />
                {t("signals")}
              </Link>
            </Button>
            <Button asChild variant="ghost">
              <Link href={settingsHref(project)} data-testid="canvas-settings">
                <Settings aria-hidden="true" />
                {t("settings")}
              </Link>
            </Button>
            {writer ? newButton : null}
          </>
        }
      />
      {viewer && !writer ? <ReaderNote project={project}>{t("readerText")}</ReaderNote> : null}
      <QueryView state={state} loading={<TableSkeleton rows={3} />}>
        {(canvases) =>
          canvases.length === 0 ? (
            <EmptyState icon={Table} title={t("emptyTitle")} description={writer ? t("emptyWriter") : t("emptyReader")} testId="canvases-empty">
              {writer ? newButton : null}
            </EmptyState>
          ) : (
            <section aria-labelledby="canvases-title">
              <h2 id="canvases-title" className="sr-only">
                {t("caption", { project })}
              </h2>
              <DataCard testId="canvases-card">
                <CanvasesTable project={project} canvases={canvases} />
              </DataCard>
            </section>
          )
        }
      </QueryView>
      {writer ? <NewCanvasDialog project={project} open={creating} onOpenChange={setCreating} /> : null}
    </div>
  );
}
