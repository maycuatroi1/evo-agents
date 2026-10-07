"use client";

import { Check, Copy, History, ListChecks, Lock } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useRef } from "react";

import { Identifier, useClipboard } from "@/components/data/identifier";
import { PageHeader } from "@/components/shell/page-header";
import { StatusBadge } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { countSteps, type Plan, parsePlan, planState } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { planHref, revisionsHref } from "./links";

/** The command the read-only banner names and copies. */
export const PLAN_CLI_COMMAND = "evo harness step";

/**
 * A plan's content is read-only on the web (decision of 2026-10-04). Every plan page says so in one sentence, the kit's
 * info banner, with the command that changes a plan and a button that copies it.
 */
export function ReadOnlyNotice({ className }: { className?: string }) {
  const t = useTranslations("plans.readOnly");
  const command = useRef<HTMLElement>(null);
  const { state, copy } = useClipboard(PLAN_CLI_COMMAND, command);
  return (
    <div
      role="note"
      aria-label={t("label")}
      data-testid="plans-read-only"
      className={cn(
        "flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border bg-surface-sunken px-4 py-2.5 text-[13px] leading-[18px] text-muted-foreground",
        className,
      )}
    >
      <Lock className="size-4 shrink-0 self-start sm:self-center" aria-hidden="true" />
      <p className="min-w-0 flex-1 text-pretty">
        <span className="font-semibold text-foreground">{t("title")}</span>{" "}
        {t.rich("text", {
          code: (chunks) => (
            <code ref={command} className="rounded-xs bg-card px-1 py-px font-mono text-xs text-foreground">
              {chunks}
            </code>
          ),
        })}
      </p>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="ml-auto"
        onClick={() => void copy()}
        aria-label={t("copyLabel", { command: PLAN_CLI_COMMAND })}
        data-testid="plans-read-only-copy"
      >
        {state === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
        {state === "copied" ? t("copied") : t("copy")}
      </Button>
    </div>
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
                ? "border-brand text-foreground"
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
 * The head of every page of one plan, on `PageHeader`'s one row. On the plan's own pages (`current` names the tab) the
 * h1 is the plan's title in the interface face, beside the plan's state, its id and its revision as chips, with Run
 * plan on the right (`actions`); the line under it says who changed the plan last, and the plan's tabs follow. On a
 * step's page `title` is the step's own h1 and `status` its state; the line under it links the plan.
 */
export function PlanHeader({
  project,
  plan,
  current,
  title: pageTitle,
  status,
  actions,
  planRunActive = false,
}: {
  project: string;
  plan: Plan;
  current: PlanTab | null;
  /** The page's own h1 (a step's title), in place of the plan's. */
  title?: ReactNode;
  /** The state of what `title` names (a step's status), in place of the plan's state. */
  status?: ReactNode;
  actions?: ReactNode;
  /** A plan run holds the plan: its pill says Plan run active instead of pending or blocked. */
  planRunActive?: boolean;
}) {
  const t = useTranslations("plans");
  const format = useFormatter();
  const planTitle = typeof plan.body.title === "string" && plan.body.title.trim() ? plan.body.title : null;
  const name = planTitle ?? plan.plan_id;
  const state = planState(plan.area, countSteps(parsePlan(plan.body, plan.plan_id).steps), planRunActive);
  const changed = {
    login: plan.updated_by,
    time: format.dateTime(new Date(plan.updated_at), { dateStyle: "medium", timeStyle: "short" }),
    who: (chunks: ReactNode) => <span className="font-mono text-foreground">{chunks}</span>,
  };
  const revision = <Identifier value={t("revision", { revision: plan.revision })} testId="plan-revision" />;
  return (
    <div className="flex flex-col gap-4">
      {pageTitle ? (
        <PageHeader
          title={pageTitle}
          status={status}
          tags={revision}
          actions={actions}
          sub={t.rich("stepOf", {
            ...changed,
            name,
            plan: (chunks) => (
              <Link
                href={planHref(project, plan.plan_id)}
                className="font-medium text-brand underline-offset-4 hover:text-brand-hover hover:underline"
                data-testid="step-plan-link"
              >
                {chunks}
              </Link>
            ),
          })}
        />
      ) : (
        <PageHeader
          title={name}
          status={<StatusBadge kind="plan" status={state} size="lg" />}
          tags={
            <>
              {planTitle ? (
                <Identifier value={plan.plan_id} copy copyLabel={t("copyId", { id: plan.plan_id })} testId="plan-id" />
              ) : null}
              {revision}
            </>
          }
          actions={actions ? <div className="contents" data-testid="plan-actions">{actions}</div> : null}
          sub={t.rich("updated", changed)}
        />
      )}
      {current ? <PlanTabs project={project} planId={plan.plan_id} current={current} /> : null}
    </div>
  );
}
