"use client";

import { ArrowLeft, History, ListChecks, PencilOff } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import type { Plan } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { planHref, plansHref, revisionsHref } from "./links";
import { AreaBadge } from "./status";

/** Plans are read-only on the web (decision of 2026-10-04): every plan page says how a plan is changed. */
export function ReadOnlyNotice({ className }: { className?: string }) {
  const t = useTranslations("plans");
  return (
    <aside
      aria-label={t("readOnly.label")}
      data-testid="plans-read-only"
      className={cn(
        "flex items-start gap-2.5 rounded-lg border border-dashed bg-card px-3 py-2.5 text-sm text-muted-foreground",
        className,
      )}
    >
      <PencilOff className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <p className="text-pretty">
        {t.rich("readOnly.text", {
          code: (chunks) => <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs text-foreground">{chunks}</code>,
        })}
      </p>
    </aside>
  );
}

export function BackLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link
      href={href as "/"}
      className="inline-flex w-fit items-center gap-1.5 rounded-md text-sm text-primary underline-offset-4 hover:underline"
    >
      <ArrowLeft className="size-4" aria-hidden="true" />
      {children}
    </Link>
  );
}

export type PlanTab = "steps" | "revisions";

function PlanTabs({ project, planId, current }: { project: string; planId: string; current: PlanTab }) {
  const t = useTranslations("plans.tabs");
  const tabs = [
    { id: "steps" as const, href: planHref(project, planId), icon: ListChecks },
    { id: "revisions" as const, href: revisionsHref(project, planId), icon: History },
  ];
  return (
    <nav aria-label={t("label")} className="-mb-px flex gap-1 overflow-x-auto border-b">
      {tabs.map((tab) => {
        const active = tab.id === current;
        return (
          <Link
            key={tab.id}
            href={tab.href}
            aria-current={active ? "page" : undefined}
            data-testid={`plan-tab-${tab.id}`}
            className={cn(
              "inline-flex min-h-11 items-center gap-2 border-b-2 px-3 text-sm font-medium whitespace-nowrap transition-colors",
              active
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:border-input hover:text-foreground",
            )}
          >
            <tab.icon className="size-4" aria-hidden="true" />
            {t(tab.id)}
          </Link>
        );
      })}
    </nav>
  );
}

/**
 * The header of every page of one plan: its title, id, area, revision and last change, then the plan's tabs. It
 * follows `PageHeader` (one h1, the same type scale); the tabs' rule takes the place of the header's own.
 */
export function PlanHeader({
  project,
  plan,
  current,
  title: pageTitle,
}: {
  project: string;
  plan: Plan;
  current: PlanTab | null;
  /** The page's own h1 (a step's title); the plan's title becomes the line above it. */
  title?: ReactNode;
}) {
  const t = useTranslations("plans");
  const format = useFormatter();
  const planTitle = typeof plan.body.title === "string" && plan.body.title.trim() ? plan.body.title : null;
  const name = planTitle ?? plan.plan_id;
  return (
    <header className={cn("flex flex-col gap-4", current ? null : "border-b pb-5")}>
      <BackLink href={pageTitle ? planHref(project, plan.plan_id) : plansHref(project)}>
        {pageTitle ? t("backToPlan", { name }) : t("backToPlans")}
      </BackLink>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="flex min-w-0 flex-col gap-1.5">
          <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
            {pageTitle ? t("stepEyebrow") : t("planEyebrow")}
          </p>
          <h1 className="text-2xl font-semibold tracking-tight break-words text-balance">
            {pageTitle ?? (planTitle ? planTitle : <span className="font-mono">{plan.plan_id}</span>)}
          </h1>
          <p className="flex flex-col gap-0.5 text-xs text-muted-foreground">
            {planTitle || pageTitle ? (
              <span className="font-mono [overflow-wrap:anywhere]" data-testid="plan-id">
                {pageTitle && planTitle ? `${plan.plan_id}: ${planTitle}` : plan.plan_id}
              </span>
            ) : null}
            <span>
              {t.rich("updated", {
                login: plan.updated_by,
                time: format.dateTime(new Date(plan.updated_at), { dateStyle: "medium", timeStyle: "short" }),
                who: (chunks) => <span className="font-mono text-foreground">{chunks}</span>,
              })}
            </span>
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <AreaBadge area={plan.area} />
          <Badge variant="outline" className="font-mono" data-testid="plan-revision">
            {t("revision", { revision: plan.revision })}
          </Badge>
        </div>
      </div>
      {current ? <PlanTabs project={project} planId={plan.plan_id} current={current} /> : null}
    </header>
  );
}
