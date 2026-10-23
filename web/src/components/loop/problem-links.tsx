"use client";

import { CircleMinus, IterationCw, Rocket } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Identifier, NAME_LINK, Tag } from "@/components/data/identifier";
import { Meter } from "@/components/data/meter";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { AnyClaimStatusBadge } from "@/components/status/claim-status";
import { StatusBadge, StatusPill } from "@/components/status/status-badge";
import { browserApi } from "@/lib/api/browser";
import { canvasPageHref, claimPageHref } from "@/lib/core-plugin-links";
import { goalsHref, LOOP_REFRESH_MS, loopHref, loopItemsQuery } from "@/lib/loop-queries";
import { percent } from "@/lib/plans";
import type { ProblemClaim, ProblemGoal, ProblemPlan, ProjectProblemShown } from "@/lib/problem-queries";
import { cn } from "@/lib/utils";

import { GoalStatusBadge } from "./badges";
import { ChainList } from "./chain-list";
import { Panel, PanelSkeleton } from "./problem-parts";

const ROW = "flex min-h-11 min-w-0 flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2";

function planHref(project: string, id: string): Route {
  return `/p/${encodeURIComponent(project)}/plans/${encodeURIComponent(id)}` as Route;
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="px-4 py-4 text-[13px] text-muted-foreground">{children}</p>;
}

function Count({ count }: { count: number }) {
  return <span className="tabular-nums">{count}</span>;
}

/** A goal's status as the problem lists it: active or paused, or closed, since the problem's read does not say how. */
function ProblemGoalStatus({ goal }: { goal: ProblemGoal }) {
  const t = useTranslations("problems.page");
  if (goal.status === "closed") return <StatusPill look={{ tone: "outline", icon: CircleMinus }} label={t("goalClosed")} data-status="closed" data-testid="goal-status" />;
  return <GoalStatusBadge goal={{ status: goal.status, close_reason: null }} />;
}

/** The goals whose newest revision answers the problem, each opening its page. */
export function GoalsSection({ project, goals }: { project: string; goals: ProblemGoal[] }) {
  const t = useTranslations("problems.page");
  return (
    <Panel id="problem-goals-title" title={t("goalsTitle")} aside={<Count count={goals.length} />} testId="problem-goals">
      {goals.length === 0 ? (
        <Empty>{t("noGoals")}</Empty>
      ) : (
        <ul className="divide-y" aria-labelledby="problem-goals-title">
          {goals.map((goal) => (
            <li key={goal.code} className={ROW} data-testid="problem-goal" data-goal={goal.code}>
              <Identifier value={goal.code} />
              <Link href={goalsHref(project, goal.code)} className={cn(NAME_LINK, "min-w-0 flex-1 truncate text-sm")} title={goal.statement}>
                {goal.statement}
              </Link>
              <ProblemGoalStatus goal={goal} />
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

type CanvasGroup = { slug: string; name: string; claims: ProblemClaim[] };

/** The claims by canvas, in the order the hub lists them (by canvas, then by code). */
function byCanvas(claims: readonly ProblemClaim[]): CanvasGroup[] {
  const groups = new Map<string, CanvasGroup>();
  for (const claim of claims) {
    const group = groups.get(claim.canvas) ?? { slug: claim.canvas, name: claim.canvas_name, claims: [] };
    group.claims.push(claim);
    groups.set(claim.canvas, group);
  }
  return [...groups.values()];
}

/** The claims of the project's canvases that link the problem, by canvas, each with its cell and status. */
export function ClaimsSection({ project, claims }: { project: string; claims: ProblemClaim[] }) {
  const t = useTranslations("problems.page");
  const cells = useTranslations("canvas.cell");
  const cellWord = (cell: string) => (cells.has(cell as never) ? cells(cell as never) : cell);
  return (
    <Panel id="problem-claims-title" title={t("claimsTitle")} aside={<Count count={claims.length} />} testId="problem-claims">
      {claims.length === 0 ? (
        <Empty>{t("noClaims")}</Empty>
      ) : (
        <div className="flex flex-col divide-y">
          {byCanvas(claims).map((group) => (
            <section key={group.slug} aria-label={t("canvasLabel", { canvas: group.name })} data-testid="problem-canvas" data-canvas={group.slug}>
              <div className="flex min-h-10 flex-wrap items-center gap-2 bg-surface-sunken px-4 py-2">
                <h3 className="min-w-0 truncate text-[13px] leading-[18px] font-semibold">
                  <Link href={canvasPageHref(project, group.slug)} className={NAME_LINK}>
                    {group.name}
                  </Link>
                </h3>
                <span className="font-mono text-xs text-fg-subtle">{group.slug}</span>
              </div>
              <ul className="divide-y">
                {group.claims.map((claim) => (
                  <li key={claim.code} className={ROW} data-testid="problem-claim" data-claim={claim.code}>
                    <Identifier value={claim.code} />
                    <Link
                      href={claimPageHref(project, group.slug, claim.code)}
                      className={cn(NAME_LINK, "min-w-0 flex-1 basis-48 line-clamp-2 text-sm")}
                      title={claim.text}
                    >
                      {claim.text}
                    </Link>
                    <Tag data-cell={claim.cell}>{cellWord(claim.cell)}</Tag>
                    <AnyClaimStatusBadge status={claim.status} />
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}
    </Panel>
  );
}

/**
 * One plan that names the problem: its title opening it, its id, its area as its state (active, or completed as the
 * plans list groups them) and its steps done as words and a meter.
 */
function PlanRow({ project, plan }: { project: string; plan: ProblemPlan }) {
  const t = useTranslations("problems.page");
  const progress = useTranslations("plans.progress");
  const done = progress("doneOfTotal", { done: plan.steps_done, total: plan.steps_total });
  return (
    <li className={cn(ROW, "py-2.5")} data-testid="problem-plan" data-plan-id={plan.plan_id}>
      <div className="flex min-w-0 flex-1 basis-56 flex-col gap-0.5">
        <Link href={planHref(project, plan.plan_id)} className={cn(NAME_LINK, "truncate text-sm")} title={plan.title ?? plan.plan_id}>
          {plan.title ?? plan.plan_id}
        </Link>
        {plan.title ? <span className="truncate font-mono text-xs text-fg-subtle">{plan.plan_id}</span> : null}
      </div>
      {plan.area === "completed" ? (
        <StatusBadge kind="plan" status="completed" />
      ) : (
        <StatusPill look={{ tone: "neutral", icon: Rocket }} label={t("planActive")} data-status="active" data-testid="plan-state" />
      )}
      <div className="flex w-40 max-w-full flex-col gap-1">
        <span className="text-xs text-muted-foreground tabular-nums" data-testid="problem-plan-progress">
          {done}
        </span>
        <Meter value={percent(plan.steps_done, plan.steps_total)} label={t("planMeter", { plan: plan.title ?? plan.plan_id })} valueText={done} />
      </div>
    </li>
  );
}

/** The plans whose body names the problem, each with its state and progress. */
export function PlansSection({ project, plans }: { project: string; plans: ProblemPlan[] }) {
  const t = useTranslations("problems.page");
  return (
    <Panel id="problem-plans-title" title={t("plansTitle")} aside={<Count count={plans.length} />} testId="problem-plans">
      {plans.length === 0 ? (
        <Empty>{t("noPlans")}</Empty>
      ) : (
        <ul className="divide-y" aria-labelledby="problem-plans-title">
          {plans.map((plan) => (
            <PlanRow key={plan.plan_id} project={project} plan={plan} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

/** The Loop's work items that go from the problem, by chain: what the Loop did and does for it. */
export function LoopSection({ project, problem }: { project: string; problem: ProjectProblemShown }) {
  const t = useTranslations("problems.page");
  const found = useHubQuery({
    ...loopItemsQuery(browserApi, project, { state: null, kind: null, goal: null, open: false, problem: problem.code }),
    refetchInterval: LOOP_REFRESH_MS,
  });
  return (
    <Panel
      id="problem-loop-title"
      title={t("loopTitle")}
      testId="problem-loop"
      aside={
        <Link href={loopHref(project, "?view=chain")} className="inline-flex min-h-6 items-center gap-1.5 text-brand transition-colors hover:text-brand-hover">
          <IterationCw className="size-4" aria-hidden="true" />
          {t("openLoop")}
        </Link>
      }
    >
      <QueryView state={found} loading={<PanelSkeleton />}>
        {(data) => (data.items.length === 0 ? <Empty>{t("noItems")}</Empty> : <ChainList project={project} items={data.items} />)}
      </QueryView>
    </Panel>
  );
}
