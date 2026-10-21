"use client";

import { useTranslations } from "next-intl";
import { Fragment } from "react";

import { cn } from "@/lib/utils";

import { type DiffHunk, type DiffLine, parseHunks } from "./content-model";

/**
 * An item's text in two versions, line by line (evo_agents.core_plugins.srs.diff): unified, or side by side from the
 * md breakpoint up. Every changed line says what happened in words for screen readers and with a sign for the eye;
 * the tint is the third cue, never the only one (as a plan's diff, plans/plan-diff.tsx).
 */
export type DiffMode = "unified" | "split";

const ROW_TONE: Record<DiffLine["kind"], string> = { context: "", added: "bg-success-soft", removed: "bg-danger-soft" };
const SIGN: Record<DiffLine["kind"], string> = { context: " ", added: "+", removed: "-" };
const SIGN_TONE: Record<DiffLine["kind"], string> = { context: "text-muted-foreground", added: "text-success", removed: "text-danger" };
const NUMBER = "w-0 px-2 py-0.5 text-right align-top font-mono text-xs text-muted-foreground tabular-nums select-none";
const CODE = "px-2 py-0.5 align-top font-mono text-[13px] leading-relaxed whitespace-pre-wrap [font-variant-ligatures:none] [overflow-wrap:anywhere]";

function Text({ line }: { line: DiffLine }) {
  const t = useTranslations("srs.compare");
  return (
    <>
      {line.kind === "context" ? null : <span className="sr-only">{line.kind === "added" ? t("addedLine") : t("removedLine")} </span>}
      <span className={cn("mr-2 inline-block w-2 font-semibold", SIGN_TONE[line.kind])} aria-hidden="true">
        {SIGN[line.kind]}
      </span>
      {line.text}
    </>
  );
}

/** A hunk's lines side by side: removed lines beside the added lines that replaced them, context on both sides. */
function splitRows(hunk: DiffHunk): { left: DiffLine | null; right: DiffLine | null }[] {
  const rows: { left: DiffLine | null; right: DiffLine | null }[] = [];
  let removed: DiffLine[] = [];
  let added: DiffLine[] = [];
  const flush = () => {
    for (let at = 0; at < Math.max(removed.length, added.length); at += 1) rows.push({ left: removed[at] ?? null, right: added[at] ?? null });
    removed = [];
    added = [];
  };
  for (const line of hunk.lines) {
    if (line.kind === "removed") removed.push(line);
    else if (line.kind === "added") added.push(line);
    else {
      flush();
      rows.push({ left: line, right: line });
    }
  }
  flush();
  return rows;
}

function Side({ line, old }: { line: DiffLine | null; old: boolean }) {
  if (!line) {
    return (
      <>
        <td className={cn(NUMBER, "bg-muted/50")} />
        <td className={cn(CODE, "bg-muted/50", old && "border-r")} />
      </>
    );
  }
  return (
    <>
      <td className={cn(NUMBER, ROW_TONE[line.kind])}>{(old ? line.old : line.new) ?? ""}</td>
      <td className={cn(CODE, ROW_TONE[line.kind], old && "border-r")} data-kind={line.kind}>
        <Text line={line} />
      </td>
    </>
  );
}

export function DiffLines({ lines, mode, from, to, label }: { lines: string[]; mode: DiffMode; from: number; to: number; label: string }) {
  const t = useTranslations("srs.compare");
  const hunks = parseHunks(lines);
  if (hunks.length === 0) return <p className="px-4 pb-4 text-[13px] text-muted-foreground">{t("noLines")}</p>;
  return (
    <div className="overflow-x-auto border-t" role="region" aria-label={label} tabIndex={0}>
      <table className={cn("w-full border-collapse", mode === "split" && "table-fixed")} data-testid="srs-diff-lines">
        <caption className="sr-only">{label}</caption>
        {mode === "split" ? (
          <colgroup>
            <col className="w-12" />
            <col />
            <col className="w-12" />
            <col />
          </colgroup>
        ) : null}
        <thead className="sr-only">
          <tr>
            {mode === "split" ? (
              <>
                <th scope="col">{t("oldLine", { version: from })}</th>
                <th scope="col">{t("oldText", { version: from })}</th>
                <th scope="col">{t("newLine", { version: to })}</th>
                <th scope="col">{t("newText", { version: to })}</th>
              </>
            ) : (
              <>
                <th scope="col">{t("oldLine", { version: from })}</th>
                <th scope="col">{t("newLine", { version: to })}</th>
                <th scope="col">{t("text")}</th>
              </>
            )}
          </tr>
        </thead>
        <tbody>
          {hunks.map((hunk, index) => (
            <Fragment key={index}>
              {index > 0 ? (
                <tr aria-hidden="true">
                  <td colSpan={mode === "split" ? 4 : 3} className="h-2 border-y bg-muted/60" />
                </tr>
              ) : null}
              {mode === "split"
                ? splitRows(hunk).map((row, at) => (
                    <tr key={at}>
                      <Side line={row.left} old />
                      <Side line={row.right} old={false} />
                    </tr>
                  ))
                : hunk.lines.map((line, at) => (
                    <tr key={at} className={ROW_TONE[line.kind]} data-kind={line.kind} data-testid="srs-diff-line">
                      <td className={NUMBER}>{line.old ?? ""}</td>
                      <td className={cn(NUMBER, "border-r")}>{line.new ?? ""}</td>
                      <td className={CODE}>
                        <Text line={line} />
                      </td>
                    </tr>
                  ))}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}
