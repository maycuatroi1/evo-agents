"use client";

import { Bandage, CircleCheck, CircleDashed, GitMerge, Goal, Kanban, ListChecks, Lock, type LucideIcon, PackageCheck, Route as RouteIcon, Table } from "lucide-react";
import type { Route } from "next";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Identifier, RunRef } from "@/components/data/identifier";
import { srsItemHref, taskHref } from "@/lib/core-plugin-links";
import { goalsHref, type LoopChainStages } from "@/lib/loop-queries";
import { mergeRequestHref } from "@/lib/merge-queue";
import { problemsHref } from "@/lib/problem-links";
import { cn } from "@/lib/utils";

export type StageId = "problem" | "goal" | "claim" | "use_case" | "task" | "plan" | "merges" | "release";
const STAGE_ICONS: Record<StageId, LucideIcon> = {
  problem: Bandage,
  goal: Goal,
  claim: Table,
  use_case: RouteIcon,
  task: Kanban,
  plan: ListChecks,
  merges: GitMerge,
  release: PackageCheck,
};
const STAGES: readonly StageId[] = ["problem", "goal", "claim", "use_case", "task", "plan", "merges", "release"];

function base(project: string): string {
  return `/p/${encodeURIComponent(project)}`;
}

/** Where a stage stands: reached (it has its object), hidden (the reader may not read it), or not yet. */
type Shown = { reached: true; value: ReactNode; detail?: ReactNode } | { reached: false; hidden: boolean };

function useStages(project: string, stages: LoopChainStages): Record<StageId, Shown> {
  const t = useTranslations("loop.chain");
  const claimStatus = useTranslations("canvas.status");
  const versionState = useTranslations("srs.versionState");
  const mergeState = useTranslations("mergeQueue.state");
  const problemStatus = useTranslations("problems.status");
  const format = useFormatter();
  const hidden = new Set<string>(stages.hidden);
  const none = (stage: string): Shown => ({ reached: false, hidden: hidden.has(stage) });
  // A state the web has no words for yet reads as the hub wrote it.
  const claimWord = (key: string) => (claimStatus.has(key as never) ? claimStatus(key as never) : key);
  const versionWord = (key: string | null) => (key === null ? "" : versionState.has(key as never) ? versionState(key as never) : key);
  const mergeWord = (key: string) => (mergeState.has(key as never) ? mergeState(key as never) : key);
  const { problem, goal, claim, use_case: useCase, task, plan, merges } = stages;
  return {
    problem: problem
      ? {
          reached: true,
          value: <Identifier value={problem.code} href={problemsHref(project, problem.code)} testId="chain-problem" />,
          detail: problem.status === "open" ? problem.statement : t("problemDetail", { statement: problem.statement, status: problemStatus(problem.status) }),
        }
      : none("problem"),
    goal: goal ? { reached: true, value: <Identifier value={goal.code} href={goalsHref(project, goal.code)} />, detail: goal.statement } : none("goal"),
    claim: claim
      ? {
          reached: true,
          value: <Identifier value={claim.code} href={`${base(project)}/x/canvas/canvases/${encodeURIComponent(claim.canvas)}/claims/${claim.code}` as Route} />,
          detail: t("claimDetail", { canvas: claim.canvas, status: claimWord(claim.status) }),
        }
      : stages.proposals !== null
        ? { reached: true, value: <span className="text-sm text-foreground">{t("proposals", { count: stages.proposals })}</span> }
        : none("claim"),
    use_case: useCase
      ? {
          reached: true,
          value: useCase.code ? (
            <Identifier value={useCase.code} href={srsItemHref(project, useCase.document, useCase.code)} />
          ) : (
            <Identifier value={useCase.document} />
          ),
          detail: t("useCaseDetail", { document: useCase.document, version: useCase.version ?? "?", state: versionWord(useCase.state) }),
        }
      : none("use_case"),
    task: task ? { reached: true, value: <Identifier value={`#${task.number}`} href={taskHref(project, String(task.number))} />, detail: task.title } : none("task"),
    plan: plan ? { reached: true, value: <Identifier value={plan.id} href={`${base(project)}/plans/${encodeURIComponent(plan.id)}` as Route} />, detail: plan.goal } : none("plan"),
    merges:
      merges.length > 0
        ? {
            reached: true,
            value: (
              <span className="flex flex-wrap gap-1">
                {merges.map((merge) => (
                  <Identifier key={merge.id} value={`${merge.repo} !${merge.iid ?? "?"}`} href={mergeRequestHref(project, merge.id)} data-merge-state={merge.state} />
                ))}
              </span>
            ),
            detail: merges.map((merge) => `!${merge.iid ?? "?"} ${mergeWord(merge.state)}`).join(", "),
          }
        : none("merges"),
    release: stages.released_at
      ? {
          reached: true,
          value: <span className="text-sm text-foreground tabular-nums">{format.dateTime(new Date(stages.released_at), { dateStyle: "medium", timeStyle: "short" })}</span>,
          detail: stages.after_sha.length > 0 ? stages.after_sha.map((sha) => sha.slice(0, 8)).join(", ") : undefined,
        }
      : none("release"),
  };
}

/**
 * The stages of a chain from the problem its goal answers to its release, in one container: the problem, the goal, the
 * claim (or the claims a brainstorm proposed), the use case, the frame task, the plan, the merge requests and the
 * release, each with its object and how it stands, "Not yet" while the chain has not come that far (or its goal answers
 * no problem), and "Not visible to you" for what the reader may not read. The cells share each row out: all eight in
 * one on a wide screen, two to a row on a phone.
 */
export function ChainRail({ project, stages }: { project: string; stages: LoopChainStages }) {
  const t = useTranslations("loop.chain");
  const shown = useStages(project, stages);
  return (
    <ol className="flex flex-wrap gap-px overflow-hidden rounded-md border bg-border shadow-raised" aria-label={t("railLabel")} data-testid="chain-rail">
      {STAGES.map((stage, index) => {
        const Icon = STAGE_ICONS[stage];
        const at = shown[stage];
        const Mark = at.reached ? CircleCheck : at.hidden ? Lock : CircleDashed;
        return (
          <li
            key={stage}
            className={cn("flex min-w-0 flex-[1_1_10rem] flex-col gap-1 px-4 py-3", at.reached ? "bg-card" : "bg-surface-sunken")}
            data-testid="chain-stage"
            data-stage={stage}
            data-reached={at.reached ? "true" : at.hidden ? "hidden" : "false"}
          >
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Icon className="size-3.5 shrink-0" aria-hidden="true" />
              <span>
                <span className="sr-only">{t("step", { index: index + 1, total: STAGES.length })} </span>
                {t(`stage.${stage}`)}
              </span>
              <Mark className={cn("ml-auto size-3.5 shrink-0", at.reached ? "text-success" : "text-fg-subtle")} aria-hidden="true" />
            </div>
            {at.reached ? (
              <>
                <div className="flex min-w-0 flex-wrap items-center gap-1">{at.value}</div>
                {at.detail ? (
                  <p className="line-clamp-2 text-xs text-fg-subtle [overflow-wrap:anywhere]" title={typeof at.detail === "string" ? at.detail : undefined}>
                    {at.detail}
                  </p>
                ) : null}
              </>
            ) : (
              <p className="text-[13px] text-fg-subtle">{at.hidden ? t("hidden") : t("notYet")}</p>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/** The runs handed out along the chain, each as the newest run that goes on from it, with its kind and state. */
export function ChainRuns({ project, stages }: { project: string; stages: LoopChainStages }) {
  const t = useTranslations("loop.chain");
  const runState = useTranslations("status.run");
  if (stages.runs.length === 0 && !stages.hidden.includes("runs")) return null;
  return (
    <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-muted-foreground" data-testid="chain-runs">
      <span className="font-medium text-foreground">{t("runs")}</span>
      {stages.runs.map((run) => (
        <span key={run.id} className="inline-flex items-center gap-1.5" data-run-id={run.id}>
          <RunRef id={run.id} href={`${base(project)}/runs/${run.id}` as Route} label={t("runLabel", { id: run.id })} />
          <span>{t("runWords", { kind: run.kind, state: runState.has(run.state as never) ? runState(run.state as never) : run.state })}</span>
        </span>
      ))}
      {stages.hidden.includes("runs") ? <span className="text-fg-subtle">{t("runsHidden")}</span> : null}
    </p>
  );
}
