"use client";

import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import type { DiffKind, DiffLine } from "@/lib/line-diff";
import { cn } from "@/lib/utils";
import { lineWords, type WordPart } from "@/lib/word-diff";

import { TRACKED_TONE } from "./tracked-marks";

/**
 * A git-style unified diff of a document: old and new line numbers, a sign, then the line. Every changed line says what
 * happened in words for screen readers and with a sign for the eye; the tint (`success-soft`, `danger-soft`) is the
 * third cue, never the only one (as a plan's diff, plans/plan-diff.tsx). A removed line's text is `danger` and struck
 * through. When a removed line pairs with the added line in its place (lib/word-diff.ts), the words that differ are
 * on a stronger tint of the same colour, so an edit inside a long line shows where it is. Unchanged runs keep CONTEXT
 * lines beside each change; a hidden run longer than MIN_HIDDEN lines folds into a button that shows it.
 */

const CONTEXT = 3;
const MIN_HIDDEN = 8;

const ROW: Record<DiffKind, string> = { same: "", add: "bg-success-soft", del: "bg-danger-soft" };
const SIGN: Record<DiffKind, string> = { same: " ", add: "+", del: "−" };
const SIGN_TONE: Record<DiffKind, string> = { same: "text-muted-foreground", add: "text-success", del: "text-danger" };
const TEXT: Record<DiffKind, string> = { same: "", add: "", del: "text-danger line-through decoration-danger" };
// The words of a paired line that differ: a stronger tint than the row's.
const WORD: Record<DiffKind, string> = { same: "", add: "rounded-xs bg-success-solid/25", del: "rounded-xs bg-danger-solid/20" };

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

function Row({ line, words }: { line: DiffLine; words: WordPart[] | null }) {
  const t = useTranslations("chats.doc");
  return (
    <div className={cn(GRID, ROW[line.kind])} data-kind={line.kind}>
      <span className={NUMBER}>{line.oldNo ?? ""}</span>
      <span className={NUMBER}>{line.newNo ?? ""}</span>
      <span className={cn("text-center font-semibold select-none", SIGN_TONE[line.kind])} aria-hidden="true">
        {SIGN[line.kind]}
      </span>
      <span className={cn("pr-3 whitespace-pre-wrap [overflow-wrap:anywhere]", TEXT[line.kind])}>
        {line.kind === "same" ? null : <span className="sr-only">{line.kind === "add" ? t("addedLine") : t("removedLine")} </span>}
        {words
          ? words.map((part, index) =>
              part.kind === "same" ? (
                part.text
              ) : (
                <span key={index} className={WORD[part.kind]} data-word={part.kind}>
                  {part.text}
                </span>
              ),
            )
          : line.text}
      </span>
    </div>
  );
}

export function DocDiff({ lines, testId = "doc-diff" }: { lines: DiffLine[]; testId?: string }) {
  const t = useTranslations("chats.doc");
  const shown = useMemo(() => blocks(lines), [lines]);
  const words = useMemo(() => lineWords(lines), [lines]);
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
        return lines
          .slice(block.start, block.end)
          .map((line, offset) => <Row key={block.start + offset} line={line} words={words[block.start + offset]} />);
      })}
    </div>
  );
}

/**
 * What the colours of the changes mean: added, removed (struck through), unchanged. The line view shows its row tints
 * with the + and − signs; the marked view (`tracked`) shows a word as `tracked-marks.tsx` marks it in the document.
 */
export function DocDiffLegend({ tracked = false }: { tracked?: boolean }) {
  const t = useTranslations("chats.doc");
  const items: { kind: DiffKind; label: string }[] = [
    { kind: "add", label: t("legendAdded") },
    { kind: "del", label: t("legendRemoved") },
    { kind: "same", label: t("legendSame") },
  ];
  return (
    <ul className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground" data-testid="doc-diff-legend" data-view={tracked ? "tracked" : "lines"}>
      {items.map((item) => (
        <li key={item.kind} className="flex items-center gap-1.5" data-kind={item.kind}>
          {tracked ? (
            <span className={cn("font-medium", item.kind === "same" ? "rounded-xs border px-0.5 text-foreground" : TRACKED_TONE[item.kind])} aria-hidden="true">
              Aa
            </span>
          ) : (
            <span className={cn("inline-flex h-4 w-4 items-center justify-center rounded-sm border font-mono", ROW[item.kind], SIGN_TONE[item.kind])} aria-hidden="true">
              {item.kind === "same" ? "" : SIGN[item.kind]}
            </span>
          )}
          {item.label}
        </li>
      ))}
    </ul>
  );
}
