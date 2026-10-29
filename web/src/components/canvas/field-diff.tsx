"use client";

import { ArrowRight } from "lucide-react";
import { useTranslations } from "next-intl";

import { valueText } from "./model";

type Change = { field: string; before?: unknown; after?: unknown };

/** The name of a claim's field as people read it: a test card's field (`test_card.deadline`) under the card's name. */
export function useFieldName(): (field: string) => string {
  const t = useTranslations("canvas.field");
  const tCard = useTranslations("canvas.card");
  return (field) => {
    if (field.startsWith("test_card.")) {
      const name = field.slice("test_card.".length);
      return t.has(`card.${name}` as never) ? t(`card.${name}` as never) : name;
    }
    return t.has(field as never) ? t(field as never) : field === "test_card" ? tCard("heading") : field;
  };
}

/** A field's value in a diff, words for the values the pages translate (a status, a cell, a confidence). */
export function useValueWords(): (field: string, value: unknown) => string {
  const tStatus = useTranslations("canvas.status");
  const tCell = useTranslations("canvas.cell");
  const tConfidence = useTranslations("canvas.confidence");
  const tField = useTranslations("canvas.field");
  return (field, value) => {
    const text = valueText(value);
    if (field === "status" && tStatus.has(text as never)) return tStatus(text as never);
    if (field === "cell" && tCell.has(text as never)) return tCell(text as never);
    if (field === "confidence" && tConfidence.has(text as never)) return tConfidence(text as never);
    if (field === "archived") return value ? tField("yes") : tField("no");
    return text;
  };
}

/**
 * What a rev, a proposal or a conflict changes, field by field: the field's name, then what it was struck through and
 * what it is now, each said in words for screen readers ("was", "now"); an empty side reads as None.
 */
export function FieldChanges({ changes, testId = "field-changes" }: { changes: readonly Change[]; testId?: string }) {
  const t = useTranslations("canvas.field");
  const name = useFieldName();
  const words = useValueWords();
  if (changes.length === 0) return <p className="text-xs text-fg-subtle">{t("nothing")}</p>;
  return (
    <dl className="grid min-w-0 gap-x-3 gap-y-1.5 text-[13px] sm:grid-cols-[minmax(7rem,auto)_minmax(0,1fr)]" data-testid={testId}>
      {changes.map((change) => {
        const before = words(change.field, change.before);
        const after = words(change.field, change.after);
        return (
          <div key={change.field} className="contents" data-field={change.field}>
            <dt className="text-xs leading-5 text-muted-foreground">{name(change.field)}</dt>
            <dd className="flex min-w-0 flex-wrap items-baseline gap-x-1.5 gap-y-0.5 [overflow-wrap:anywhere]">
              <span className="sr-only">{t("was")}</span>
              {before ? <del className="text-fg-subtle decoration-danger/60">{before}</del> : <span className="text-fg-subtle italic">{t("none")}</span>}
              <ArrowRight className="size-3.5 shrink-0 self-center text-fg-subtle" aria-hidden="true" />
              <span className="sr-only">{t("now")}</span>
              {after ? <ins className="text-foreground no-underline">{after}</ins> : <span className="text-fg-subtle italic">{t("none")}</span>}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}
