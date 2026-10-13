"use client";

import { CornerDownRight } from "lucide-react";
import Link from "next/link";
import { type useFormatter, useTranslations } from "next-intl";

import { type PlanStep, stepLabel } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { stepHref } from "./links";
import { StepStatusIcon } from "./status";

/** What the plan's page and a step's page share about a step: its dependencies as links, and its dates. */

export type StepContext = { project: string; planId: string; byKey: Map<string, PlanStep> };

/** depends_on as links to those steps, each with its status, so a reader sees what a step waits for. */
export function DependsOn({ step, context, compact = false }: { step: PlanStep; context: StepContext; compact?: boolean }) {
  const t = useTranslations("plans.step");
  if (step.dependsOn.length === 0) {
    return compact ? null : <span className="text-sm text-muted-foreground">{t("noDependencies")}</span>;
  }
  return (
    <span className={cn("flex flex-wrap items-center gap-x-2 gap-y-1", compact ? "text-xs" : "text-sm")}>
      {compact ? (
        <span className="inline-flex items-center gap-1 text-muted-foreground">
          <CornerDownRight className="size-3.5" aria-hidden="true" />
          {t("after")}
        </span>
      ) : null}
      {step.dependsOn.map((key) => {
        const target = context.byKey.get(key);
        if (!target) {
          return (
            <span key={key} className="font-mono text-muted-foreground" title={t("unknownStep")}>
              {key}
            </span>
          );
        }
        return (
          <Link
            key={key}
            href={stepHref(context.project, context.planId, key)}
            className="relative z-10 inline-flex items-center gap-1 rounded font-mono text-brand underline-offset-4 hover:underline"
            aria-label={t("dependencyLink", { key, title: stepLabel(target) })}
          >
            <StepStatusIcon group={target.group} raw={target.rawStatus} className="[&_svg]:size-3.5" />
            {key}
          </Link>
        );
      })}
    </span>
  );
}

type Formatter = ReturnType<typeof useFormatter>;

/** A plan date (often `2026-10-02T11:15:00+07:00`, sometimes a bare day or free text) for people to read. */
export function formatDay(format: Formatter, value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return /T\d/.test(value)
    ? format.dateTime(date, { dateStyle: "medium", timeStyle: "short" })
    : format.dateTime(date, { dateStyle: "medium" });
}
