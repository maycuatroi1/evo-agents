"use client";

import { Columns2, Rows3 } from "lucide-react";
import { useTranslations } from "next-intl";
import { Fragment, useState } from "react";

import { useIsMobile } from "@/hooks/use-mobile";
import { type PlanDiff, type PlanDiffHunk, type PlanDiffLine, splitRows } from "@/lib/plans";
import { cn } from "@/lib/utils";

/**
 * Two revisions of a plan, line by line, as their git copies read. Every changed line says what happened in words
 * for screen readers ("added", "removed") and with a sign for the eye; the tint is the third cue, never the only
 * one. The unified view works at every width; the side-by-side view is offered from the md breakpoint up.
 */
type Mode = "unified" | "split";

const ROW_TONE: Record<PlanDiffLine["kind"], string> = {
  context: "",
  added: "bg-success",
  removed: "bg-destructive/10",
};

const SIGN: Record<PlanDiffLine["kind"], string> = { context: " ", added: "+", removed: "-" };
const SIGN_TONE: Record<PlanDiffLine["kind"], string> = {
  context: "text-muted-foreground",
  added: "text-success-foreground",
  removed: "text-destructive",
};

const NUMBER = "w-0 px-2 py-0.5 text-right align-top font-mono text-xs text-muted-foreground tabular-nums select-none";
const CODE =
  "px-2 py-0.5 align-top font-mono text-[13px] leading-relaxed whitespace-pre-wrap [font-variant-ligatures:none] [overflow-wrap:anywhere]";

function KindWord({ kind }: { kind: PlanDiffLine["kind"] }) {
  const t = useTranslations("plans.diff");
  if (kind === "context") return null;
  return <span className="sr-only">{kind === "added" ? t("addedLine") : t("removedLine")} </span>;
}

function hunkRange(hunk: PlanDiffHunk) {
  const last = (start: number, count: number) => Math.max(start, start + count - 1);
  return {
    newFrom: hunk.new_start,
    newTo: last(hunk.new_start, hunk.new_lines),
    oldFrom: hunk.old_start,
    oldTo: last(hunk.old_start, hunk.old_lines),
  };
}

function HunkTitle({ hunk, index, diff }: { hunk: PlanDiffHunk; index: number; diff: PlanDiff }) {
  const t = useTranslations("plans.diff");
  const range = hunkRange(hunk);
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 border-b bg-muted/60 px-3 py-1.5">
      <span className="text-xs font-medium">
        {t("hunkTitle", {
          index: index + 1,
          from: range.newFrom,
          to: range.newTo,
          revision: diff.to_revision.revision,
        })}
      </span>
      <span className="font-mono text-xs text-muted-foreground [font-variant-ligatures:none]" aria-hidden="true">
        @@ -{hunk.old_start},{hunk.old_lines} +{hunk.new_start},{hunk.new_lines} @@
      </span>
    </div>
  );
}

function UnifiedHunk({ hunk, index, diff }: { hunk: PlanDiffHunk; index: number; diff: PlanDiff }) {
  const t = useTranslations("plans.diff");
  const range = hunkRange(hunk);
  return (
    <div className="overflow-hidden rounded-lg border bg-card" data-testid="diff-hunk">
      <HunkTitle hunk={hunk} index={index} diff={diff} />
      <table className="w-full border-collapse">
        <caption className="sr-only">
          {t("hunkCaption", { index: index + 1, from: range.oldFrom, to: range.oldTo, newFrom: range.newFrom, newTo: range.newTo })}
        </caption>
        <thead className="sr-only">
          <tr>
            <th scope="col">{t("oldLine", { revision: diff.from_revision.revision })}</th>
            <th scope="col">{t("newLine", { revision: diff.to_revision.revision })}</th>
            <th scope="col">{t("text")}</th>
          </tr>
        </thead>
        <tbody>
          {hunk.lines.map((line, row) => (
            <tr key={row} className={ROW_TONE[line.kind]} data-kind={line.kind} data-testid="diff-line">
              <td className={NUMBER}>{line.old ?? ""}</td>
              <td className={cn(NUMBER, "border-r")}>{line.new ?? ""}</td>
              <td className={CODE}>
                <KindWord kind={line.kind} />
                <span className={cn("mr-2 inline-block w-2 font-bold", SIGN_TONE[line.kind])} aria-hidden="true">
                  {SIGN[line.kind]}
                </span>
                <span data-testid="diff-text">{line.text}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SplitCell({ line, side }: { line: PlanDiffLine | null; side: "old" | "new" }) {
  if (!line) {
    return (
      <>
        <td className={cn(NUMBER, "bg-muted/50")} />
        <td className={cn(CODE, "bg-muted/50", side === "old" ? "border-r" : null)} />
      </>
    );
  }
  const number = side === "old" ? line.old : line.new;
  return (
    <>
      <td className={cn(NUMBER, ROW_TONE[line.kind])}>{number ?? ""}</td>
      <td className={cn(CODE, ROW_TONE[line.kind], side === "old" ? "border-r" : null)} data-kind={line.kind}>
        <KindWord kind={line.kind} />
        <span className={cn("mr-2 inline-block w-2 font-bold", SIGN_TONE[line.kind])} aria-hidden="true">
          {SIGN[line.kind]}
        </span>
        {line.text}
      </td>
    </>
  );
}

function SplitHunk({ hunk, index, diff }: { hunk: PlanDiffHunk; index: number; diff: PlanDiff }) {
  const t = useTranslations("plans.diff");
  const range = hunkRange(hunk);
  return (
    <div className="overflow-hidden rounded-lg border bg-card" data-testid="diff-hunk">
      <HunkTitle hunk={hunk} index={index} diff={diff} />
      <table className="w-full table-fixed border-collapse">
        <caption className="sr-only">
          {t("hunkCaption", { index: index + 1, from: range.oldFrom, to: range.oldTo, newFrom: range.newFrom, newTo: range.newTo })}
        </caption>
        <colgroup>
          <col className="w-12" />
          <col />
          <col className="w-12" />
          <col />
        </colgroup>
        <thead className="sr-only">
          <tr>
            <th scope="col">{t("oldLine", { revision: diff.from_revision.revision })}</th>
            <th scope="col">{t("oldText", { revision: diff.from_revision.revision })}</th>
            <th scope="col">{t("newLine", { revision: diff.to_revision.revision })}</th>
            <th scope="col">{t("newText", { revision: diff.to_revision.revision })}</th>
          </tr>
        </thead>
        <tbody>
          {splitRows(hunk).map((row, position) => (
            <tr key={position}>
              <SplitCell line={row.left} side="old" />
              <SplitCell line={row.right} side="new" />
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function DiffModeToggle({ mode, onChange }: { mode: Mode; onChange: (mode: Mode) => void }) {
  const t = useTranslations("plans.diff");
  return (
    <div role="group" aria-label={t("mode")} className="hidden w-fit rounded-lg border bg-muted/40 p-0.5 md:inline-flex">
      {(
        [
          ["unified", Rows3],
          ["split", Columns2],
        ] as const
      ).map(([id, Icon]) => (
        <button
          key={id}
          type="button"
          aria-pressed={mode === id}
          onClick={() => onChange(id)}
          className={cn(
            "inline-flex min-h-9 items-center gap-1.5 rounded-md px-3 text-sm font-medium transition-colors",
            mode === id ? "bg-card text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
          )}
          data-testid={`diff-mode-${id}`}
        >
          <Icon className="size-4" aria-hidden="true" />
          {t(id)}
        </button>
      ))}
    </div>
  );
}

/** The hunks of a diff, unified or side by side, and the counts of lines added and removed. */
export function DiffView({ diff }: { diff: PlanDiff }) {
  const t = useTranslations("plans.diff");
  const [mode, setMode] = useState<Mode>("unified");
  const mobile = useIsMobile();
  const shown: Mode = mobile ? "unified" : mode;
  return (
    <div className="flex flex-col gap-3" data-testid="plan-diff">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm" data-testid="diff-stats">
          <span className="inline-flex items-center gap-1 rounded-md bg-success px-1.5 py-0.5 font-mono text-xs font-medium text-success-foreground">
            {t("added", { count: diff.added })}
          </span>
          <span className="inline-flex items-center gap-1 rounded-md bg-destructive/10 px-1.5 py-0.5 font-mono text-xs font-medium text-destructive">
            {t("removed", { count: diff.removed })}
          </span>
          <span className="text-xs text-muted-foreground">{t("context", { count: diff.context })}</span>
        </p>
        {diff.hunks.length ? <DiffModeToggle mode={mode} onChange={setMode} /> : null}
      </div>
      {diff.hunks.length === 0 ? (
        <p className="rounded-lg border border-dashed bg-card px-4 py-6 text-center text-sm text-muted-foreground" data-testid="diff-empty">
          {t("noChange")}
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          {diff.hunks.map((hunk, index) => (
            <Fragment key={`${hunk.old_start}-${hunk.new_start}`}>
              {shown === "split" ? (
                <SplitHunk hunk={hunk} index={index} diff={diff} />
              ) : (
                <UnifiedHunk hunk={hunk} index={index} diff={diff} />
              )}
            </Fragment>
          ))}
        </div>
      )}
    </div>
  );
}
