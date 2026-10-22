"use client";

import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import type { DiffKind, DiffLine } from "@/lib/line-diff";
import { cn } from "@/lib/utils";

/**
 * A git-style unified diff of a document: old and new line numbers, a sign, then the line. Added rows are green,
 * removed rows red. Unchanged runs keep CONTEXT lines beside each change; a hidden run longer than MIN_HIDDEN lines
 * folds into a button that shows it.
 */

const CONTEXT = 3;
const MIN_HIDDEN = 8;

const ROW: Record<DiffKind, string> = {
  same: "",
  add: "bg-green-500/15 text-green-900 dark:text-green-200",
  del: "bg-red-500/15 text-red-900 dark:text-red-200",
};
const SIGN: Record<DiffKind, string> = { same: " ", add: "+", del: "−" };

type Block = { kind: "lines"; start: number; end: number } | { kind: "fold"; start: number; end: number };

/** The rows as shown lines and folds: [start, end) indexes into `lines`. */
function blocks(lines: DiffLine[]): Block[] {
  const near = new Array<boolean>(lines.length).fill(false);
  lines.forEach((line, index) => {
    if (line.kind === "same") return;
    for (let k = Math.max(0, index - CONTEXT); k <= Math.min(lines.length - 1, index + CONTEXT); k++) near[k] = true;
  });
  const out: Block[] = [];
  let index = 0;
  while (index < lines.length) {
    const start = index;
    if (near[index]) {
      while (index < lines.length && near[index]) index++;
    } else {
      while (index < lines.length && !near[index]) index++;
      if (index - start > MIN_HIDDEN) {
        out.push({ kind: "fold", start, end: index });
        continue;
      }
    }
    const last = out[out.length - 1];
    if (last?.kind === "lines" && last.end === start) last.end = index;
    else out.push({ kind: "lines", start, end: index });
  }
  return out;
}

const GRID = "grid grid-cols-[3.5rem_3.5rem_1.5rem_minmax(0,1fr)]";
const NUMBER = "select-none px-2 text-right text-muted-foreground tabular-nums";

function Row({ line }: { line: DiffLine }) {
  return (
    <div className={cn(GRID, ROW[line.kind])} data-kind={line.kind}>
      <span className={NUMBER}>{line.oldNo ?? ""}</span>
      <span className={NUMBER}>{line.newNo ?? ""}</span>
      <span className="select-none text-center" aria-hidden={line.kind === "same"}>
        {SIGN[line.kind]}
      </span>
      <span className="pr-3 whitespace-pre-wrap [overflow-wrap:anywhere]">{line.text}</span>
    </div>
  );
}

export function DocDiff({ lines, testId = "doc-diff" }: { lines: DiffLine[]; testId?: string }) {
  const t = useTranslations("chats.doc");
  const shown = useMemo(() => blocks(lines), [lines]);
  const [opened, setOpened] = useState<ReadonlySet<number>>(() => new Set());

  return (
    <div
      role="region"
      aria-label={t("diffLabel")}
      tabIndex={0}
      data-testid={testId}
      className="overflow-auto bg-card py-1 font-mono text-[13px] leading-5 text-foreground [font-variant-ligatures:none]"
    >
      {shown.map((block) => {
        if (block.kind === "fold" && !opened.has(block.start)) {
          return (
            <button
              key={`fold-${block.start}`}
              type="button"
              data-testid="doc-diff-fold"
              className="block w-full bg-muted/60 px-3 py-0.5 text-left text-muted-foreground hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring"
              onClick={() => setOpened((now) => new Set(now).add(block.start))}
            >
              ⋯ {t("hiddenLines", { count: block.end - block.start })}
            </button>
          );
        }
        return lines.slice(block.start, block.end).map((line, offset) => <Row key={block.start + offset} line={line} />);
      })}
    </div>
  );
}

export function DocDiffLegend() {
  const t = useTranslations("chats.doc");
  const items: { kind: DiffKind; label: string }[] = [
    { kind: "add", label: t("legendAdded") },
    { kind: "del", label: t("legendRemoved") },
    { kind: "same", label: t("legendSame") },
  ];
  return (
    <ul className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground" data-testid="doc-diff-legend">
      {items.map((item) => (
        <li key={item.kind} className="flex items-center gap-1.5" data-kind={item.kind}>
          <span className={cn("inline-flex h-4 w-4 items-center justify-center rounded-sm border font-mono", ROW[item.kind])} aria-hidden="true">
            {item.kind === "same" ? "" : SIGN[item.kind]}
          </span>
          {item.label}
        </li>
      ))}
    </ul>
  );
}
