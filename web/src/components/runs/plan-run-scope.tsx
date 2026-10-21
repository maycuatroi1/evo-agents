"use client";

import { CircleAlert, Flag, GitBranch, GitMerge, ListChecks, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { InfoTip } from "@/components/feedback/info-tip";
import { StatusBadge, useStatusText } from "@/components/status/status-badge";
import { Badge } from "@/components/ui/badge";
import type { StepGroup } from "@/lib/plans";

import type { PlanRunScope } from "./model";
import { type Run, runHref, type StepReadiness } from "./queries";

/**
 * What the Run plan dialog says a plan run will do, and what keeps the hub from taking it now: the blockers above the
 * form, and the summary of its steps, repos, checkpoints and pushes. The dialog hands the summary the words of a
 * step's status (`useStepStatusText` of the plans feature), so this file stays within its own feature.
 */

/** What keeps the hub from taking a plan run of the plan now, or makes the run fail before its agent starts. */
export type Blocker =
  | { kind: "planRun"; id: number; state: Run["state"]; login: string }
  | { kind: "stepRun"; id: number; state: Run["state"]; step: string }
  | { kind: "noPending" }
  | { kind: "unplaced"; steps: string[] }
  | { kind: "noBranch"; repos: string[] };

export function blockersOf(scope: PlanRunScope, steps: readonly StepReadiness[], planRun: { id: number; state: Run["state"]; dispatched_by: string } | null): Blocker[] {
  const found: Blocker[] = [];
  if (planRun) found.push({ kind: "planRun", id: planRun.id, state: planRun.state, login: planRun.dispatched_by });
  const stepRun = steps.filter((step) => step.active_run).sort((a, b) => a.active_run!.id - b.active_run!.id)[0];
  if (!planRun && stepRun?.active_run) found.push({ kind: "stepRun", id: stepRun.active_run.id, state: stepRun.active_run.state, step: stepRun.key });
  if (scope.pending === 0) found.push({ kind: "noPending" });
  if (scope.unplaced.length) found.push({ kind: "unplaced", steps: scope.unplaced });
  const unnamed = scope.repos.filter((repo) => repo.branch === null).map((repo) => repo.repo);
  if (unnamed.length) found.push({ kind: "noBranch", repos: unnamed });
  return found;
}

export function Blockers({ project, blockers }: { project: string; blockers: Blocker[] }) {
  const t = useTranslations("runs.planRun.blocker");
  const tState = useStatusText("run");
  const format = useFormatter();
  if (blockers.length === 0) return null;
  const runLink = (id: number) =>
    function RunLink(chunks: ReactNode) {
      return (
        <Link href={runHref(project, id)} className="font-medium underline underline-offset-4">
          {chunks}
        </Link>
      );
    };
  const text = (blocker: Blocker): ReactNode => {
    switch (blocker.kind) {
      case "planRun":
        return t.rich("planRun", { id: blocker.id, state: tState(blocker.state), login: blocker.login, link: runLink(blocker.id) });
      case "stepRun":
        return t.rich("stepRun", { id: blocker.id, state: tState(blocker.state), step: blocker.step, link: runLink(blocker.id) });
      case "noPending":
        return t("noPending");
      case "unplaced":
        return t("unplaced", { steps: format.list(blocker.steps, { type: "conjunction" }) });
      case "noBranch":
        return t("noBranch", { repos: format.list(blocker.repos, { type: "conjunction" }) });
    }
  };
  return (
    <div role="alert" className="flex flex-col gap-1.5 rounded-md border border-attention/30 bg-attention-soft px-3 py-2.5 text-sm text-attention" data-testid="plan-run-blockers">
      {blockers.map((blocker) => (
        <p key={blocker.kind} className="flex items-start gap-2 text-pretty" data-testid={`plan-run-blocker-${blocker.kind}`}>
          <CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>{text(blocker)}</span>
        </p>
      ))}
    </div>
  );
}

/**
 * The steps the run does, its repos and their branches, and its checkpoints; how the agent takes them and where it
 * pushes show from the info icons, and a warning only when the plan names a default branch, where the agent merges.
 */
export function ScopeSummary({ scope, statusText }: { scope: PlanRunScope; statusText: (group: StepGroup) => string }) {
  const t = useTranslations("runs.planRun.summary");
  const format = useFormatter();
  const counts = (
    [
      ["pending", scope.pending],
      ["in_progress", scope.inProgress],
      ["blocked", scope.blocked],
    ] as const
  ).filter(([, count]) => count > 0);
  const defaults = scope.repos.filter((repo) => repo.defaultBranch);
  const titled = new Map(scope.checkpoints.map((step) => [step.key, step.title]));

  return (
    <section aria-labelledby="plan-run-summary-title" className="flex flex-col gap-3 rounded-md border bg-muted/30 p-3 sm:p-4" data-testid="plan-run-summary">
      <h3 id="plan-run-summary-title" className="flex items-center gap-2 text-sm font-medium">
        <ListChecks className="size-4 text-muted-foreground" aria-hidden="true" />
        {t("title")}
      </h3>
      <dl className="grid gap-x-4 gap-y-3 text-sm sm:grid-cols-[max-content_minmax(0,1fr)]">
        <Term label={t("steps")} info={t("order")} testId="plan-run-steps-info" />
        <dd className="flex min-w-0 flex-col gap-1.5" data-testid="plan-run-steps" data-count={scope.steps.length}>
          <span className="font-medium">{t("stepsCount", { count: scope.steps.length })}</span>
          {counts.length > 0 ? (
            <span className="flex flex-wrap gap-1.5">
              {counts.map(([group, count]) => (
                <StatusBadge
                  key={group}
                  kind="step"
                  status={group}
                  label={t("statusCount", { count, status: statusText(group) })}
                  className="tabular-nums"
                  data-testid={`plan-run-count-${group}`}
                />
              ))}
            </span>
          ) : null}
        </dd>

        <Term label={t("repos")} info={`${t("push")} ${defaults.length ? "" : t("pushNoDefault")}`.trim()} testId="plan-run-repos-info" />
        <dd className="min-w-0">
          <ul className="flex flex-col gap-1.5" data-testid="plan-run-repos">
            {scope.repos.map((repo) => (
              <li key={repo.repo} className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1" data-testid={`plan-run-repo-${repo.repo}`}>
                <span className="font-mono text-xs font-medium [overflow-wrap:anywhere]">{repo.repo}</span>
                {repo.branch ? (
                  <span className="inline-flex min-w-0 items-center gap-1 font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">
                    <GitBranch className="size-3.5 shrink-0" aria-hidden="true" />
                    {repo.branch}
                  </span>
                ) : (
                  <span className="text-xs text-danger">{t("noBranch")}</span>
                )}
                {repo.defaultBranch ? (
                  <Badge variant="warning" data-testid="plan-run-default-branch">
                    <GitMerge aria-hidden="true" />
                    {t("defaultBranch")}
                  </Badge>
                ) : null}
              </li>
            ))}
            {scope.repos.length === 0 ? <li className="text-xs text-muted-foreground">{t("noRepos")}</li> : null}
          </ul>
        </dd>

        <Term label={t("checkpoints")} info={scope.checkpoints.length ? t("checkpointsHint") : undefined} testId="plan-run-checkpoints-info" />
        <dd className="min-w-0" data-testid="plan-run-checkpoints" data-count={scope.checkpoints.length}>
          {scope.checkpoints.length === 0 ? (
            <span className="text-muted-foreground">{t("noCheckpoints")}</span>
          ) : (
            <>
              <ul className="flex flex-col gap-1">
                {scope.checkpoints.map((step) => (
                  <li key={step.key} className="flex min-w-0 items-start gap-1.5">
                    <Flag className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                    <span className="min-w-0 [overflow-wrap:anywhere]">
                      <span className="mr-1.5 font-mono text-xs text-muted-foreground tabular-nums">{step.key}</span>
                      {titled.get(step.key) ?? t("untitled")}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </dd>
      </dl>
      {defaults.length ? (
        <p
          className="flex items-start gap-2 rounded-sm border border-attention/30 bg-attention-soft px-3 py-2 text-xs text-pretty text-attention"
          data-testid="plan-run-push"
          data-default="true"
        >
          <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          <span>
            {t("pushDefault", {
              repos: format.list(
                defaults.map((repo) => t("repoBranch", { repo: repo.repo, branch: repo.branch ?? "" })),
                { type: "conjunction" },
              ),
              count: defaults.length,
            })}
          </span>
        </p>
      ) : null}
    </section>
  );
}

/** A term of the summary, with an info icon beside it for what it means. */
function Term({ label, info, testId }: { label: string; info?: string; testId: string }) {
  return (
    <dt className="flex items-center gap-1.5 self-start text-muted-foreground">
      {label}
      {info ? (
        <InfoTip label={label} testId={testId}>
          {info}
        </InfoTip>
      ) : null}
    </dt>
  );
}
