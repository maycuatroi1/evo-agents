"use client";

import { useTranslations } from "next-intl";

import type { WordPart } from "@/lib/word-diff";

import type { SrsItem } from "./api";
import { type Change, fieldParts, type FieldRow } from "./case-review-model";
import { CodeText, type ItemLinks } from "./item-body";
import { plainLine } from "./review-queue-model";

const ADDED = "rounded-xs bg-success-solid/25 px-0.5 text-foreground no-underline";
const REMOVED = "rounded-xs bg-danger-solid/15 px-0.5 text-danger line-through decoration-danger";

/** A changed run as its white space around and its words, so the tint covers words only. */
function edges(text: string): [string, string, string] {
  const [, before = "", words = "", after = ""] = /^(\s*)([\s\S]*?)(\s*)$/.exec(text) ?? [];
  return [before, words, after];
}

/** Added and removed words: a tint and a strike for the eye, and the words "added" and "removed" for a screen reader. */
function Parts({ parts }: { parts: WordPart[] }) {
  const t = useTranslations("srs.caseReview.content");
  return (
    <span className="[overflow-wrap:anywhere]" data-testid="srs-case-field-diff">
      {parts.map((part, index) => {
        if (part.kind === "same") return <span key={index}>{part.text}</span>;
        const [before, words, after] = edges(part.text);
        return (
          <span key={index}>
            {before}
            {part.kind === "add" ? (
              <ins className={ADDED} data-kind="add">
                <span className="sr-only">{t("addedText")} </span>
                {words}
              </ins>
            ) : (
              <del className={REMOVED} data-kind="del">
                <span className="sr-only">{t("removedText")} </span>
                {words}
              </del>
            )}
            {after}
          </span>
        );
      })}
    </span>
  );
}

/**
 * A field of the use case as the review reads it: as written when this version left it alone, else word by word
 * against the version approved last, the words it added on a green tint and those it removed struck through.
 */
export function FieldText({ row, items, links }: { row: FieldRow; items: SrsItem[]; links: ItemLinks }) {
  if (row.change === "same") return <CodeText text={row.after ?? row.before ?? ""} items={items} links={links} />;
  const before = plainLine(row.before ?? "");
  const after = plainLine(row.after ?? "");
  const parts: WordPart[] = row.change === "changed" ? fieldParts(before, after) : row.change === "added" ? [{ kind: "add", text: after }] : [{ kind: "del", text: before }];
  return <Parts parts={parts} />;
}

/** The mark beside a field or a rule this version changed: New, Changed or Removed. */
export function ChangeMark({ change }: { change: Change }) {
  const t = useTranslations("srs.caseReview.content.mark");
  if (change === "same") return null;
  const tone = change === "added" ? "bg-success-soft text-success" : change === "removed" ? "bg-danger-soft text-danger" : "bg-review-soft text-review";
  return (
    <span className={`inline-flex h-[18px] items-center rounded-xs px-1.5 text-[11px] leading-none font-medium whitespace-nowrap ${tone}`} data-testid="srs-case-mark" data-change={change}>
      {t(change)}
    </span>
  );
}
