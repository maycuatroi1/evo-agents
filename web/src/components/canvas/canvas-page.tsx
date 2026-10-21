"use client";

import { useQuery } from "@tanstack/react-query";
import { Download, Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Identifier } from "@/components/data/identifier";
import { MetricStrip } from "@/components/data/metric-strip";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { signalKeysQuery } from "@/lib/signal-queries";

import { baselinesQuery, type CanvasDetail, type Cell, canvasQuery, exportCanvas, proposalsQuery } from "./api";
import { STATUS_LOOKS } from "./badges";
import { BaselinesCard, WaitingBaselines } from "./baselines-card";
import { CanvasGrid } from "./canvas-grid";
import { ReaderNote } from "./chrome";
import { NewClaimDialog } from "./claim-dialog";
import { signInAgainOn401, useCanvasViewer } from "./hooks";
import { canvasHref } from "./links";
import { canWrite, STATUSES, statusCounts, utcToday } from "./model";
import { ProposalsCard } from "./proposals";

/** Export the canvas as evo-canvas/1 JSON, which the browser saves; `evo-agents hub canvas import` reads it back. */
function ExportButton({ project, slug }: { project: string; slug: string }) {
  const t = useTranslations("canvas.page");
  const failure = useWriteFailure();
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      const exported = await exportCanvas(browserApi(), project, slug);
      const url = URL.createObjectURL(new Blob([JSON.stringify(exported, null, 2)], { type: "application/json;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = `${slug}.evo-canvas.json`;
      link.click();
      URL.revokeObjectURL(url);
      notify({ tone: "success", text: t("exported"), description: link.download });
    } catch (error) {
      signInAgainOn401(error);
      notifyFailure(t("exportFailed"), failure(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Button type="button" variant="ghost" busy={busy} onClick={() => void save()} data-testid="canvas-export">
      <Download aria-hidden="true" />
      {t("export")}
    </Button>
  );
}

/** Whether archived claims show on the canvas: a switch that keeps its state in the address (`?archived=1`). */
function ArchivedSwitch({ project, slug, archived }: { project: string; slug: string; archived: boolean }) {
  const t = useTranslations("canvas.page");
  const router = useRouter();
  const id = useId();
  return (
    <div className="flex items-center gap-2">
      <Switch id={id} checked={archived} onCheckedChange={(on) => router.replace(canvasHref(project, slug, on), { scroll: false })} data-testid="canvas-archived" />
      <Label htmlFor={id} className="font-normal text-muted-foreground">
        {t("archived")}
      </Label>
    </div>
  );
}

function CanvasView({ project, canvas, archived }: { project: string; canvas: CanvasDetail; archived: boolean }) {
  const t = useTranslations("canvas.page");
  const tStatus = useTranslations("canvas.status");
  const viewer = useCanvasViewer(project);
  const writer = canWrite(viewer);
  const [adding, setAdding] = useState<Cell | null>(null);
  const proposals = useQuery(proposalsQuery(browserApi, project, canvas.slug));
  const baselines = useQuery(baselinesQuery(browserApi, project, canvas.slug));
  const keys = useQuery({ ...signalKeysQuery(browserApi, project), enabled: writer && adding !== null });
  const counts = statusCounts(canvas.statuses);
  const metrics = STATUSES.map((status) => ({ id: status, label: tStatus(status), icon: STATUS_LOOKS[status].icon, value: counts[status] }));
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="canvas-page" data-canvas={canvas.slug}>
      <div className="flex flex-col gap-3">
        <PageHeader
          title={canvas.name}
          tags={<Identifier value={canvas.slug} testId="canvas-slug" />}
          sub={t("sub", { login: canvas.created_by, claims: canvas.claims })}
          actions={
            <>
              <ExportButton project={project} slug={canvas.slug} />
              {writer ? (
                <Button onClick={() => setAdding("problem")} data-testid="claim-new">
                  <Plus aria-hidden="true" />
                  {t("addClaim")}
                </Button>
              ) : null}
            </>
          }
          testId="canvas-head"
        />
        {canvas.description ? <p className="max-w-prose text-[13px] text-pretty whitespace-pre-line text-muted-foreground [overflow-wrap:anywhere]">{canvas.description}</p> : null}
      </div>
      {viewer && !writer ? <ReaderNote project={project}>{t("readerText")}</ReaderNote> : null}
      <WaitingBaselines project={project} slug={canvas.slug} baselines={baselines.data ?? []} />
      <MetricStrip label={t("statuses")} metrics={metrics} testId="canvas-statuses" />
      <ProposalsCard project={project} slug={canvas.slug} proposals={proposals.data ?? []} writer={writer} />
      <section aria-labelledby="canvas-grid-title" className="flex min-w-0 flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="canvas-grid-title" className="text-[15px] leading-[22px] font-semibold">
            {t("grid")}
          </h2>
          <ArchivedSwitch project={project} slug={canvas.slug} archived={archived} />
        </div>
        <CanvasGrid project={project} canvas={canvas} today={utcToday()} onAdd={writer ? setAdding : null} />
      </section>
      <BaselinesCard project={project} slug={canvas.slug} baselines={baselines.data ?? []} />
      {writer ? (
        <NewClaimDialog
          project={project}
          slug={canvas.slug}
          cell={adding}
          keys={keys.data ?? []}
          onOpenChange={(open) => {
            if (!open) setAdding(null);
          }}
        />
      ) : null}
    </div>
  );
}

/**
 * A canvas whole (/p/{project}/x/canvas/canvases/{slug}): its nine cells with their claims, each with its status,
 * confidence, evidence and deadline; how many claims hold each status; the proposals that wait for a writer; the
 * baseline that waits for review; and the baselines before. A writer adds a claim to any cell and decides proposals;
 * a reader reads.
 */
export function CanvasPage({ project, slug, archived, initialError }: { project: string; slug: string; archived: boolean; initialError: ApiErrorInfo | null }) {
  const state = useHubQuery(canvasQuery(browserApi, project, slug, archived), initialError);
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(canvas) => <CanvasView project={project} canvas={canvas} archived={archived} />}
    </QueryView>
  );
}
