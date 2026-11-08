"use client";

import { ArrowUpRight, ChevronRight, Info } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { Banner } from "@/components/feedback/banner";

import { type CaseView, fieldRows, ruleRows, shownCase, stepsChanged } from "./case-review-model";
import { stepsOf } from "./content-model";
import { ChangeMark, FieldText } from "./field-diff";
import { Rule, StepsTable, useCodes } from "./item-body";
import { itemHref } from "./links";
import { StepCard } from "./step-card";

/** The Activity Flow and the business rules, folded under one line that counts them and says when they changed. */
function StepsAndRules({ view }: { view: CaseView }) {
  const t = useTranslations("srs.caseReview.content");
  const shown = shownCase(view)!;
  const rules = ruleRows(view.baseItems, view.base, view.items, view.item);
  const codes = useCodes(shown.items, view.links);
  const steps = stepsOf(shown.item).rows.length;
  const changed = stepsChanged(view.base, view.item) || rules.rows.some((row) => row.change !== "same") || rules.removed.length > 0;
  return (
    <details className="group rounded-md border" data-testid="srs-case-more" data-changed={changed || undefined}>
      <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 px-3 text-sm font-medium [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" aria-hidden="true" />
        <span>{t("more", { steps, rules: rules.rows.length })}</span>
        {changed ? <span className="text-xs font-normal text-review">{t("moreChanged")}</span> : null}
      </summary>
      <div className="flex min-w-0 flex-col gap-3 border-t p-3">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          {t("steps")}
          {stepsChanged(view.base, view.item) ? <ChangeMark change="changed" /> : null}
        </h3>
        <StepsTable items={shown.items} item={shown.item} links={view.links} />
        <h3 className="mt-2 text-sm font-semibold">{t("rules")}</h3>
        {rules.rows.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">{t("noRules")}</p>
        ) : (
          <div className="flex flex-col gap-3">
            {rules.rows.map(({ rule, change }) => (
              <Rule key={rule.key} rule={rule} links={view.links} codes={codes} mark={<ChangeMark change={change} />} />
            ))}
          </div>
        )}
      </div>
    </details>
  );
}

/**
 * Step 1, what the use case says: its fields in plain words (who uses it, what they want, the context, when it starts,
 * what is needed first, when it is done), each change of this version marked word by word, then its steps and business
 * rules folded under one line. A use case new in this version, or one it drops, says so once instead.
 */
export function CaseContentCard({ view }: { view: CaseView }) {
  const t = useTranslations("srs.caseReview");
  const shown = shownCase(view)!;
  const rows = fieldRows(view.base, view.item);
  const marked = rows.some((row) => row.change !== "same");
  const ref = shown.item.code ?? shown.item.key;
  const at = view.item ? view.version.number : view.approved;
  return (
    <StepCard id="srs-step-content" title={t("step.content")} note={marked ? t("content.legend") : undefined} testId="srs-case-content">
      {view.base === null && view.item !== null && view.approved !== null && view.compared ? (
        <Banner icon={Info} tone="success" title={t("content.added")} testId="srs-case-new" />
      ) : null}
      {view.item === null ? (
        <Banner icon={Info} tone="attention" title={t("content.removed")} testId="srs-case-dropped" />
      ) : null}
      {rows.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">{t("content.noFields")}</p>
      ) : (
        <dl className="grid grid-cols-1 gap-x-6 gap-y-3 text-sm sm:grid-cols-[minmax(8rem,auto)_1fr]" data-testid="srs-case-fields">
          {rows.map((row) => (
            <div key={row.key} className="contents" data-testid="srs-case-field" data-field={row.key} data-change={row.change}>
              <dt className="flex flex-wrap items-center gap-1.5 text-[13px] font-medium text-muted-foreground max-sm:mt-1">
                {t(`content.field.${row.key}`)}
                <ChangeMark change={row.change} />
              </dt>
              <dd className="min-w-0 leading-relaxed [overflow-wrap:anywhere]">
                <FieldText row={row} items={shown.items} links={view.links} />
              </dd>
            </div>
          ))}
        </dl>
      )}
      <StepsAndRules view={view} />
      <Link href={itemHref(view.project, view.slug, ref, at)} className="inline-flex items-center gap-1.5 self-start text-[13px] text-brand hover:text-brand-hover">
        <ArrowUpRight className="size-4" aria-hidden="true" />
        {t("content.openItem")}
      </Link>
    </StepCard>
  );
}
