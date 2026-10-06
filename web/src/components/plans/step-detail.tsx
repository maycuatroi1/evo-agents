"use client";

import { ArrowLeft, ArrowRight, FileCheck2 } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useMemo } from "react";

import { StepRuns } from "@/components/runs/step-runs";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { planQuery } from "@/lib/plan-queries";
import { dependents, type Plan, type PlanStep, parsePlan, stepLabel } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { stepHref } from "./links";
import { PlanHeader, ReadOnlyNotice } from "./plan-header";
import { PlanNotFound } from "./plan-not-found";
import { Prose, ValueView, Verbatim } from "./prose";
import { BlockingBadge, StepStatusBadge, StepStatusIcon } from "./status";
import { DependsOn, formatDay } from "./step-board";

type Context = { project: string; planId: string; byKey: Map<string, PlanStep> };

function Block({ title, children, testId }: { title: string; children: ReactNode; testId?: string }) {
  return (
    <section className="flex flex-col gap-2" data-testid={testId}>
      <h2 className="text-sm font-medium text-muted-foreground">{title}</h2>
      {children}
    </section>
  );
}

function Missing({ children }: { children: ReactNode }) {
  return <p className="text-sm text-muted-foreground italic">{children}</p>;
}

function Facts({ step, context }: { step: PlanStep; context: Context }) {
  const t = useTranslations("plans.step");
  const format = useFormatter();
  const waiting = dependents([...context.byKey.values()], step.key);
  const rows: [string, ReactNode][] = [
    [t("status"), <StepStatusBadge key="status" group={step.group} raw={step.rawStatus} />],
    [t("blockingLabel"), <BlockingBadge key="blocking" blocking={step.blocking} showFalse />],
    [t("repo"), step.repo ? <span className="font-mono text-sm">{step.repo}</span> : <span className="text-muted-foreground">-</span>],
    [
      t("doneAt"),
      step.doneAt ? (
        <time className="text-sm tabular-nums" dateTime={step.doneAt} data-testid="step-done-at">
          {formatDay(format, step.doneAt)}
        </time>
      ) : (
        <span className="text-sm text-muted-foreground">-</span>
      ),
    ],
    [t("dependsOn"), <DependsOn key="depends" step={step} context={context} />],
    [
      t("dependents"),
      waiting.length ? (
        <span className="flex flex-wrap gap-x-2 gap-y-1 text-sm">
          {waiting.map((other) => (
            <Link
              key={other.key}
              href={stepHref(context.project, context.planId, other.key)}
              className="inline-flex items-center gap-1 rounded font-mono text-brand underline-offset-4 hover:underline"
              aria-label={t("dependencyLink", { key: other.key, title: stepLabel(other) })}
            >
              <StepStatusIcon group={other.group} raw={other.rawStatus} className="[&_svg]:size-3.5" />
              {other.key}
            </Link>
          ))}
        </span>
      ) : (
        <span className="text-sm text-muted-foreground">{t("noDependents")}</span>
      ),
    ],
  ];
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-3 rounded-md border bg-card shadow-raised p-4 sm:grid-cols-[auto_1fr]" data-testid="step-facts">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-sm text-muted-foreground sm:pt-0.5">{label}</dt>
          <dd className="min-w-0">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function StepNavigator({ steps, current, context }: { steps: PlanStep[]; current: PlanStep; context: Context }) {
  const t = useTranslations("plans.step");
  return (
    <nav aria-label={t("navigator")} className="hidden lg:block">
      <ol className="sticky top-20 flex max-h-[calc(100svh-6rem)] flex-col gap-0.5 overflow-y-auto rounded-md border bg-card shadow-raised p-1.5">
        {steps.map((step) => {
          const active = step.index === current.index;
          return (
            <li key={`${step.index}-${step.key}`}>
              <Link
                href={stepHref(context.project, context.planId, step.key)}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex min-h-9 items-start gap-2 rounded-sm px-2 py-1.5 text-sm transition-colors",
                  active ? "bg-accent font-medium text-accent-foreground" : "hover:bg-muted",
                )}
              >
                <StepStatusIcon group={step.group} raw={step.rawStatus} className="mt-0.5" />
                <span className="w-6 shrink-0 font-mono text-xs leading-5 tabular-nums">{step.key}</span>
                <span className="line-clamp-2 min-w-0 text-pretty">{stepLabel(step) || step.key}</span>
              </Link>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function PrevNext({ steps, current, context }: { steps: PlanStep[]; current: PlanStep; context: Context }) {
  const t = useTranslations("plans.step");
  const previous = current.index > 0 ? steps[current.index - 1] : null;
  const next = current.index < steps.length - 1 ? steps[current.index + 1] : null;
  if (!previous && !next) return null;
  const card = "flex min-h-11 flex-1 flex-col gap-0.5 rounded-md border bg-card px-3 py-2 text-sm transition-colors hover:border-brand/40 hover:bg-accent/40";
  return (
    <nav aria-label={t("prevNext")} className="flex flex-col gap-2 border-t pt-4 sm:flex-row">
      {previous ? (
        <Link href={stepHref(context.project, context.planId, previous.key)} className={card} rel="prev">
          <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
            <ArrowLeft className="size-3.5" aria-hidden="true" />
            {t("previous")}
          </span>
          <span className="line-clamp-1 font-medium">
            <span className="font-mono text-xs">{previous.key}</span> {stepLabel(previous)}
          </span>
        </Link>
      ) : (
        <span className="hidden flex-1 sm:block" />
      )}
      {next ? (
        <Link href={stepHref(context.project, context.planId, next.key)} className={cn(card, "sm:items-end sm:text-right")} rel="next">
          <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
            {t("next")}
            <ArrowRight className="size-3.5" aria-hidden="true" />
          </span>
          <span className="line-clamp-1 font-medium">
            <span className="font-mono text-xs">{next.key}</span> {stepLabel(next)}
          </span>
        </Link>
      ) : null}
    </nav>
  );
}

function Detail({ project, plan, stepKey }: { project: string; plan: Plan; stepKey: string }) {
  const t = useTranslations("plans.step");
  const view = useMemo(() => parsePlan(plan.body, plan.plan_id), [plan]);
  const context = useMemo<Context>(
    () => ({ project, planId: plan.plan_id, byKey: new Map(view.steps.map((step) => [step.key, step])) }),
    [project, plan.plan_id, view.steps],
  );
  const step = view.steps.find((candidate) => candidate.key === stepKey);
  if (!step) return <PlanNotFound project={project} planId={plan.plan_id} step={stepKey} />;
  const label = stepLabel(step);
  return (
    <>
      <PlanHeader
        project={project}
        plan={plan}
        current={null}
        title={
          <>
            <span className="text-muted-foreground tabular-nums">{step.key}</span> {label || t("untitled")}
          </>
        }
        status={<StepStatusBadge group={step.group} raw={step.rawStatus} size="lg" testId="step-header-status" />}
      />
      <ReadOnlyNotice />
      <div className="grid gap-6 lg:grid-cols-[18rem_minmax(0,1fr)]">
        <StepNavigator steps={view.steps} current={step} context={context} />
        <article className="flex min-w-0 flex-col gap-6" aria-label={t("detailLabel", { key: step.key })}>
          <Facts step={step} context={context} />
          <StepRuns project={project} planId={plan.plan_id} stepKey={step.key} />
          <Block title={t("what")} testId="step-what">
            {step.what ? <Prose>{step.what}</Prose> : <Missing>{t("noWhat")}</Missing>}
          </Block>
          <Block title={t("verify")} testId="step-verify">
            {step.verify ? <Verbatim>{step.verify}</Verbatim> : <Missing>{t("noVerify")}</Missing>}
          </Block>
          {step.note ? (
            <Block title={t("note")} testId="step-note">
              <Prose>{step.note}</Prose>
            </Block>
          ) : null}
          <Block title={t("evidence")}>
            {step.evidence ? (
              <div className="flex flex-col gap-1.5">
                <Verbatim testId="step-evidence" className="border-success/30 bg-success-soft/40">
                  {step.evidence}
                </Verbatim>
                <p className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                  <FileCheck2 className="size-3.5" aria-hidden="true" />
                  {t("evidenceVerbatim")}
                </p>
              </div>
            ) : (
              <Missing>{step.group === "done" ? t("noEvidenceDone") : t("noEvidence")}</Missing>
            )}
          </Block>
          {step.extra.map(([key, value]) => (
            <Block key={key} title={key}>
              <ValueView value={value} />
            </Block>
          ))}
          <PrevNext steps={view.steps} current={step} context={context} />
        </article>
      </div>
    </>
  );
}

export function StepDetail({
  project,
  planId,
  stepKey,
  initialError,
  invalid = false,
}: {
  project: string;
  planId: string;
  stepKey: string;
  initialError: ApiErrorInfo | null;
  invalid?: boolean;
}) {
  const state = useHubQuery({ ...planQuery(browserApi, project, planId), enabled: !invalid }, initialError);
  if (invalid || (state.status === "error" && state.error.status === 404)) {
    return <PlanNotFound project={project} planId={planId} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(plan) => <Detail project={project} plan={plan} stepKey={stepKey} />}
    </QueryView>
  );
}
