"use client";

import { Check, Circle } from "lucide-react";
import { useTranslations } from "next-intl";

import type { PlanStep, PlanView } from "@/lib/plans";

import { criteriaOf, doneWhenOf } from "./step-criteria";

/**
 * The step in progress, open in the Plan panel under its row: its `done_when`, and the criteria it names with how far
 * each is, "1 of 3 criteria met". Nothing is drawn for a step that states neither.
 */
export function StepFocus({ view, step }: { view: PlanView; step: PlanStep }) {
  const t = useTranslations("runs.detail.planSteps");
  const doneWhen = doneWhenOf(step);
  const criteria = criteriaOf(view, step);
  if (doneWhen.length === 0 && criteria.length === 0) return null;
  const met = criteria.filter((criterion) => criterion.met).length;
  return (
    <li className="flex min-w-0 flex-col gap-2 px-2 pb-2 pl-12 max-md:pl-10" data-testid="run-step-focus">
      {doneWhen.length > 0 ? (
        <div className="flex min-w-0 flex-col gap-0.5" data-testid="run-step-done-when">
          <span className="text-xs font-medium text-muted-foreground">{t("doneWhen")}</span>
          {doneWhen.map((sentence) => (
            <p key={sentence} className="max-w-[80ch] text-[15px] leading-6 text-pretty [overflow-wrap:anywhere]">
              {sentence}
            </p>
          ))}
        </div>
      ) : null}
      {criteria.length > 0 ? (
        <div className="flex min-w-0 flex-col gap-1" data-testid="run-step-criteria">
          <span className="text-xs font-medium text-muted-foreground tabular-nums" data-testid="run-step-criteria-count">
            {t("criteria", { met, total: criteria.length })}
          </span>
          <ul className="flex flex-col gap-1">
            {criteria.map((criterion) => (
              <li key={criterion.id ?? criterion.text} className="flex min-w-0 items-start gap-2" data-testid="run-step-criterion" data-met={criterion.met}>
                {criterion.met ? <Check className="mt-1 size-4 shrink-0 text-success" aria-hidden="true" /> : <Circle className="mt-1 size-4 shrink-0 text-fg-subtle" aria-hidden="true" />}
                <span className="sr-only">{criterion.met ? t("criterionMet") : t("criterionOpen")}: </span>
                {criterion.id ? <span className="mt-0.5 shrink-0 font-mono text-xs leading-5 text-fg-subtle">{criterion.id}</span> : null}
                <span className="min-w-0 text-[15px] leading-6 text-pretty [overflow-wrap:anywhere]">{criterion.text}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </li>
  );
}
