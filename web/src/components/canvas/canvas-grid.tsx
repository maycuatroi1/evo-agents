"use client";

import { CalendarClock, Plus, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import type { CanvasClaim, CanvasDetail, Cell } from "./api";
import { ClaimStatusBadge, ConfidenceTag } from "./badges";
import { claimHref } from "./links";
import { CELL_PLACE, CELLS, isOverdue } from "./model";

/** A test card's deadline as a short day, in amber with "past its deadline" once a baseline would ask about it. */
function Deadline({ claim, today }: { claim: CanvasClaim; today: string }) {
  const t = useTranslations("canvas.claimCard");
  const format = useFormatter();
  const deadline = claim.test_card?.deadline;
  if (!deadline) return null;
  const overdue = isOverdue(claim, today);
  const day = format.dateTime(new Date(`${deadline}T00:00:00Z`), { month: "short", day: "numeric", timeZone: "UTC" });
  const words = overdue ? t("overdue", { day }) : t("due", { day });
  return (
    <span
      className={cn("inline-flex shrink-0 items-center gap-1 text-xs whitespace-nowrap tabular-nums", overdue ? "font-medium text-attention" : "text-muted-foreground")}
      title={words}
      data-testid="claim-deadline"
      data-overdue={overdue || undefined}
    >
      <CalendarClock className="size-3.5" aria-hidden="true" />
      <span className="sr-only">{words}</span>
      <span aria-hidden="true">{day}</span>
    </span>
  );
}

/** How much evidence a claim has, and how much of it is real and supports it: `1/3` with a shield, said in words. */
function EvidenceCount({ claim }: { claim: CanvasClaim }) {
  const t = useTranslations("canvas.claimCard");
  if (claim.evidence_count === 0) return null;
  const words = t("evidence", { count: claim.evidence_count, real: claim.real_support_count });
  return (
    <span
      className={cn("inline-flex items-center gap-1 text-xs tabular-nums", claim.real_support_count > 0 ? "text-success" : "text-muted-foreground")}
      title={words}
      data-testid="claim-evidence-count"
    >
      <ShieldCheck className="size-3.5" aria-hidden="true" />
      <span aria-hidden="true">
        {claim.real_support_count}/{claim.evidence_count}
      </span>
      <span className="sr-only">{words}</span>
    </span>
  );
}

/** A claim on the canvas: its code and status, its text linking to its page, its confidence, evidence and deadline. */
function ClaimCard({ project, slug, claim, today }: { project: string; slug: string; claim: CanvasClaim; today: string }) {
  return (
    <li
      className={cn("flex min-w-0 flex-col gap-2 rounded-md border bg-card p-3 shadow-raised", claim.archived && "border-dashed opacity-75")}
      data-testid="claim-card"
      data-claim={claim.code}
      data-status={claim.status}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        <span className="font-mono text-xs font-medium text-muted-foreground tabular-nums">{claim.code}</span>
        <ClaimStatusBadge status={claim.status} className="ml-auto" />
      </div>
      <Link
        href={claimHref(project, slug, claim.code)}
        className="text-[13px] leading-[18px] font-medium text-pretty text-foreground transition-colors [overflow-wrap:anywhere] hover:text-brand"
        data-testid="claim-card-text"
      >
        <span className="line-clamp-4">{claim.text}</span>
      </Link>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1">
        <ConfidenceTag confidence={claim.confidence} />
        <EvidenceCount claim={claim} />
        <Deadline claim={claim} today={today} />
      </div>
    </li>
  );
}

/** One cell of the canvas: its number and name, how many claims it holds, its claims by place, and Add for a writer. */
function CellBox({
  project,
  slug,
  cell,
  claims,
  today,
  onAdd,
}: {
  project: string;
  slug: string;
  cell: Cell;
  claims: readonly CanvasClaim[];
  today: string;
  onAdd: ((cell: Cell) => void) | null;
}) {
  const t = useTranslations("canvas.cell");
  const tGrid = useTranslations("canvas.grid");
  const number = CELLS.indexOf(cell) + 1;
  const headingId = `cell-${cell}`;
  return (
    <section
      aria-labelledby={headingId}
      className={cn("flex min-h-40 min-w-0 flex-col gap-3 rounded-md border bg-surface-sunken/60 p-3", CELL_PLACE[cell])}
      data-testid="canvas-cell"
      data-cell={cell}
    >
      <div className="flex min-w-0 items-start gap-2">
        <span className="mt-px grid size-5 shrink-0 place-items-center rounded-full bg-card text-[11px] font-semibold text-muted-foreground tabular-nums ring-1 ring-border" aria-hidden="true">
          {number}
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <h3 id={headingId} className="text-[13px] leading-5 font-semibold text-foreground">
            {t(cell)}
            <span className="sr-only">{tGrid("count", { count: claims.length })}</span>
          </h3>
          <p className="text-xs text-fg-subtle">{t(`${cell}Hint`)}</p>
        </div>
        {onAdd ? (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            onClick={() => onAdd(cell)}
            aria-label={tGrid("add", { cell: t(cell) })}
            title={tGrid("add", { cell: t(cell) })}
            data-testid="cell-add"
          >
            <Plus aria-hidden="true" />
          </Button>
        ) : null}
      </div>
      {claims.length === 0 ? (
        <p className="text-xs text-fg-subtle" data-testid="cell-empty">
          {tGrid("empty")}
        </p>
      ) : (
        <ul className="flex min-w-0 flex-col gap-2">
          {claims.map((claim) => (
            <ClaimCard key={claim.code} project={project} slug={slug} claim={claim} today={today} />
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * The nine cells as Lean Canvas prints them from xl (Problem, Solution over Key metrics, the value proposition, Unfair
 * advantage over Channels, Customer segments, then Cost structure beside Revenue streams), two columns from md and one
 * on a phone. The cells come in the order Lean Canvas numbers them, which is how a screen reader and the keyboard
 * walk them; each cell's number says it on the printed layout too.
 */
export function CanvasGrid({
  project,
  canvas,
  today,
  onAdd,
}: {
  project: string;
  canvas: CanvasDetail;
  today: string;
  onAdd: ((cell: Cell) => void) | null;
}) {
  const byCell = new Map(canvas.cells.map((cell) => [cell.cell, cell.claims]));
  return (
    <div className="grid min-w-0 gap-3 md:grid-cols-2 xl:grid-cols-10" data-testid="canvas-grid">
      {CELLS.map((cell) => (
        <CellBox key={cell} project={project} slug={canvas.slug} cell={cell} claims={byCell.get(cell) ?? []} today={today} onAdd={onAdd} />
      ))}
    </div>
  );
}
