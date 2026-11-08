"use client";

import { FileDiff } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { type CaseView, fieldRows, ruleRows, stepsChanged } from "./case-review-model";
import { ChangeMark } from "./field-diff";
import { compareHref } from "./links";
import { StepCard } from "./step-card";

/** One change of the list: what it is about, then the mark of what happened to it. */
function Change({ name, code, change }: { name: string; code?: string | null; change: "added" | "changed" | "removed" }) {
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[13px]" data-testid="srs-case-change" data-change={change}>
      <ChangeMark change={change} />
      {code ? <span className="font-mono text-xs text-muted-foreground">{code}</span> : null}
      <span className="min-w-0 [overflow-wrap:anywhere]">{name}</span>
    </li>
  );
}

/**
 * Step 2, what changed this time: the author's note on the version, then what it changed in this use case (its fields,
 * its steps, the business rules under it, or the whole use case added or removed) against the version approved last,
 * and the full comparison of the two versions.
 */
export function CaseChangesCard({ view }: { view: CaseView }) {
  const t = useTranslations("srs.caseReview");
  const { version, approved } = view;
  const fields = fieldRows(view.base, view.item).filter((row) => row.change !== "same");
  const rules = ruleRows(view.baseItems, view.base, view.items, view.item);
  const changedRules = rules.rows.filter((row) => row.change !== "same");
  const steps = stepsChanged(view.base, view.item);
  const whole = view.item === null ? "removed" : view.base === null && approved !== null ? "added" : null;
  const nothing = whole === null && fields.length === 0 && changedRules.length === 0 && rules.removed.length === 0 && !steps;
  return (
    <StepCard id="srs-step-changes" title={t("step.changes")} testId="srs-case-changes">
      <section className="flex flex-col gap-1.5" aria-labelledby="srs-case-note">
        <h3 id="srs-case-note" className="text-[13px] font-medium text-muted-foreground">
          {t("changes.note", { author: version.author })}
        </h3>
        {version.summary.trim() ? (
          <blockquote className="border-l-2 border-border-strong pl-3 text-sm whitespace-pre-wrap [overflow-wrap:anywhere]" data-testid="srs-case-summary">
            {version.summary.trim()}
          </blockquote>
        ) : (
          <p className="text-[13px] text-muted-foreground">{t("changes.noNote")}</p>
        )}
      </section>
      <section className="flex flex-col gap-1.5" aria-labelledby="srs-case-here">
        <h3 id="srs-case-here" className="text-[13px] font-medium text-muted-foreground">
          {t("changes.inCase")}
        </h3>
        {approved === null ? (
          <p className="text-[13px] text-muted-foreground">{view.latest.approved_version === null ? t("changes.first") : t("changes.noCompare")}</p>
        ) : !view.compared ? (
          <p className="text-[13px] text-muted-foreground" data-testid="srs-case-changes-failed">
            {t("changes.failed")}
          </p>
        ) : whole ? (
          <p className="text-[13px]" data-testid="srs-case-whole" data-change={whole}>
            {t(`changes.${whole}`)}
          </p>
        ) : nothing ? (
          <p className="text-[13px] text-muted-foreground" data-testid="srs-case-changes-elsewhere">
            {t("changes.nothing")}
          </p>
        ) : (
          <ul className="flex flex-col gap-1.5" role="list" data-testid="srs-case-change-list">
            {fields.map((row) => (
              <Change key={row.key} name={t(`content.field.${row.key}`)} change={row.change as "added" | "changed" | "removed"} />
            ))}
            {steps ? <Change name={t("content.steps")} change="changed" /> : null}
            {changedRules.map(({ rule, change }) => (
              <Change key={rule.key} name={rule.title} code={rule.code} change={change as "added" | "changed"} />
            ))}
            {rules.removed.map((rule) => (
              <Change key={rule.key} name={rule.title} code={rule.code} change="removed" />
            ))}
          </ul>
        )}
      </section>
      {approved !== null && approved !== version.number ? (
        <Link
          href={compareHref(view.project, view.slug, Math.min(approved, version.number), Math.max(approved, version.number))}
          className="inline-flex items-center gap-1.5 self-start text-[13px] text-brand hover:text-brand-hover"
          data-testid="srs-case-compare"
        >
          <FileDiff className="size-4" aria-hidden="true" />
          {t("changes.compare")}
        </Link>
      ) : null}
    </StepCard>
  );
}
