"use client";

import { Bot, Check, ChevronRight, Circle, CircleAlert, CircleCheck, CircleDot, CircleHelp, CircleX, FileDiff, GitCommitHorizontal, type LucideIcon, ListTree, Radio } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";

import { Identifier } from "@/components/data/identifier";
import { Button } from "@/components/ui/button";
import { type PlanStep, type PlanView, stepLabel } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { commitUrl } from "./forge";
import { useNewTabLabel, useRepoWeb } from "./links";
import type { StepCard } from "./progress-model";
import { type Run, runDiffHref, type RunEvent } from "./queries";
import type { RunTask } from "./run-tasks";
import { criteriaOf, doneWhenOf } from "./step-criteria";
import { StepSoFar } from "./step-so-far-card";
import { useTraceDuration } from "./trace-look";

const ICON: Record<PlanStep["group"], { icon: LucideIcon; tone: string }> = {
  done: { icon: CircleCheck, tone: "text-success" },
  in_progress: { icon: CircleDot, tone: "text-running" },
  blocked: { icon: CircleAlert, tone: "text-attention" },
  pending: { icon: Circle, tone: "text-fg-subtle" },
  other: { icon: CircleHelp, tone: "text-muted-foreground" },
};

/** A 44 px target on a phone, a compact one beside it. */
const ACTION = "max-md:h-11 max-md:min-w-11";

function Fact({ icon: Icon, label, children, testId }: { icon: LucideIcon; label: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="flex min-w-0 items-start gap-2 text-sm leading-5" data-testid={testId}>
      <Icon className="mt-0.5 size-4 shrink-0 text-fg-subtle" aria-hidden="true" />
      <span className="sr-only">{label}: </span>
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1">{children}</div>
    </div>
  );
}

/** The agents of a step: the main agent and each sub-agent that started in its span, a sub-agent opening its profile. */
function Agents({ agents, onOpenAgent }: { agents: readonly RunTask[]; onOpenAgent: (id: string) => void }) {
  const t = useTranslations("runs.detail.progress.card");
  return (
    <Fact icon={Bot} label={t("agents")} testId="progress-agents">
      <span className="rounded-full bg-muted px-2 py-0.5 text-xs leading-5 text-muted-foreground">{t("main")}</span>
      {agents.map((task) => (
        <button
          key={task.id}
          type="button"
          onClick={() => onOpenAgent(task.id)}
          className="min-h-6 max-w-full cursor-pointer truncate rounded-full bg-muted px-2 py-0.5 text-xs leading-5 text-foreground hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring max-md:min-h-11 max-md:px-3"
          aria-label={t("openAgent", { name: task.name })}
          data-testid="progress-agent"
        >
          {task.name}
        </button>
      ))}
    </Fact>
  );
}

function Commits({ run, card }: { run: Run; card: StepCard }) {
  const t = useTranslations("runs.detail.progress.card");
  const named = useNewTabLabel();
  const webOf = useRepoWeb(run.project);
  const { commits } = card.evidence;
  if (commits.length === 0) return null;
  return (
    <Fact icon={GitCommitHorizontal} label={t("commits")} testId="progress-commits">
      {commits.map((commit) => {
        const href = commitUrl(webOf(commit.repo), commit.sha);
        return (
          <Identifier key={commit.sha} value={commit.sha.slice(0, 8)} externalHref={href} linkLabel={href ? named(commit.sha) : undefined} testId="progress-commit" title={commit.repo ?? undefined} />
        );
      })}
    </Fact>
  );
}

function Verify({ card }: { card: StepCard }) {
  const t = useTranslations("runs.detail.progress.card");
  const { verify, passed } = card.evidence;
  if (verify === null) return null;
  const Icon = passed === false ? CircleX : Check;
  return (
    <Fact icon={Icon} label={passed === false ? t("verifyFailed") : passed ? t("verifyPassed") : t("verify")} testId="progress-verify">
      <code className="min-w-0 font-mono text-xs leading-5 text-muted-foreground [overflow-wrap:anywhere]">{verify}</code>
    </Fact>
  );
}

/** What the step had to show (its `done_when` and the criteria it names), and which of them the plan says are reached. */
function Reached({ card, view }: { card: StepCard; view: PlanView | null }) {
  const t = useTranslations("runs.detail.progress.card");
  const doneWhen = useMemo(() => doneWhenOf(card.step), [card.step]);
  const criteria = useMemo(() => criteriaOf(view, card.step), [view, card.step]);
  const done = card.step.group === "done";
  if (doneWhen.length === 0 && criteria.length === 0) return null;
  const met = criteria.filter((criterion) => criterion.met).length;
  return (
    <div className="flex min-w-0 flex-col gap-1" data-testid="progress-reached">
      <h4 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{done ? t("reached") : t("toReach")}</h4>
      <ul className="flex flex-col gap-1">
        {doneWhen.map((sentence) => (
          <li key={sentence} className="flex items-start gap-2 text-sm leading-5" data-testid="progress-done-when" data-reached={done}>
            {done ? <Check className="mt-0.5 size-4 shrink-0 text-success" aria-hidden="true" /> : <Circle className="mt-0.5 size-4 shrink-0 text-fg-subtle" aria-hidden="true" />}
            <span className="min-w-0 [overflow-wrap:anywhere]">{sentence}</span>
          </li>
        ))}
      </ul>
      {criteria.length > 0 ? (
        <p className="text-xs leading-5 text-muted-foreground tabular-nums" data-testid="progress-criteria">
          {t("criteria", { met, total: criteria.length })}
        </p>
      ) : null}
    </div>
  );
}

/**
 * One step of the plan as a card on the Progress view: its state, how long it took, who took part, the commits and the
 * verify its evidence names, what it had to show and whether it did, and the ways on: the step's page, its place in
 * the trace and the run's diff. The step the run works on is the card of Step N so far with a link to Live.
 */
export function ProgressStepCard({
  run,
  card,
  view,
  events,
  tasks,
  onOpenAgent,
  onShowCall,
  onLive,
  stepHref,
}: {
  run: Run;
  card: StepCard;
  view: PlanView | null;
  events: readonly RunEvent[];
  tasks: readonly RunTask[];
  onOpenAgent: (id: string) => void;
  onShowCall: (call: string) => void;
  onLive: () => void;
  stepHref: (key: string) => Route;
}) {
  const t = useTranslations("runs.detail.progress.card");
  const tStatus = useTranslations("status.step");
  const format = useFormatter();
  const short = useTraceDuration();
  const { step, running, span } = card;
  const { icon: Icon, tone } = ICON[running ? "in_progress" : step.group];
  const status = running ? t("running") : step.group === "other" ? (step.rawStatus ?? "") : tStatus(step.group);
  const took = span ? short(Math.max(span.to - span.from, 0)) : null;
  const finished = step.doneAt && !Number.isNaN(Date.parse(step.doneAt)) ? format.dateTime(new Date(step.doneAt), { dateStyle: "medium", timeStyle: "short" }) : null;
  const noEvidence = card.evidence.commits.length === 0 && card.evidence.verify === null;
  return (
    <li className="min-w-0" data-testid="progress-step" data-step={step.key} data-status={running ? "running" : step.group}>
      <article className={cn("flex min-w-0 flex-col rounded-md border bg-card shadow-raised", running && "border-running/60")} aria-labelledby={`progress-step-${step.key}`}>
        <header className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3">
          <Icon className={cn("size-5 shrink-0", tone)} aria-hidden="true" />
          <h3 id={`progress-step-${step.key}`} className="min-w-0 flex-1 text-[15px] leading-[22px] font-semibold [overflow-wrap:anywhere]">
            <span className="font-mono text-xs font-normal text-fg-subtle tabular-nums">{t("step", { step: step.key })}</span>{" "}
            <Link href={stepHref(step.key)} className="underline-offset-4 hover:underline" data-testid="progress-step-link">
              {stepLabel(step) || t("untitled")}
            </Link>
          </h3>
          <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs leading-5 text-muted-foreground tabular-nums">
            <span className={cn("font-medium", running ? "text-running" : "text-foreground")} data-testid="progress-step-status">
              {status}
            </span>
            {took ? (
              <span data-testid="progress-step-time">
                <span className="sr-only">{t("took")}: </span>
                {running ? t("for", { time: took }) : took}
              </span>
            ) : null}
            {finished ? <time dateTime={step.doneAt ?? undefined}>{t("finished", { time: finished })}</time> : null}
          </p>
        </header>

        {running ? (
          <div className="flex flex-col">
            <div className="flex min-w-0 flex-col gap-2 border-t px-4 py-3">
              <Agents agents={card.agents} onOpenAgent={onOpenAgent} />
            </div>
            <StepSoFar run={run} events={events} tasks={tasks} onOpenAgent={onOpenAgent} bare />
            <div className="flex flex-wrap gap-1 border-t px-3 py-2">
              <Button type="button" variant="outline" size="sm" className={ACTION} onClick={onLive} data-testid="progress-live">
                <Radio aria-hidden="true" />
                {t("openLive")}
              </Button>
            </div>
          </div>
        ) : (
          <>
            <div className="flex min-w-0 flex-col gap-2 border-t px-4 py-3">
              {span ? <Agents agents={card.agents} onOpenAgent={onOpenAgent} /> : null}
              <Commits run={run} card={card} />
              <Verify card={card} />
              {noEvidence ? (
                <p className="text-sm leading-5 text-muted-foreground" data-testid="progress-no-evidence">
                  {t("noEvidence")}
                </p>
              ) : null}
            </div>
            {doneWhenOf(step).length > 0 || criteriaOf(view, step).length > 0 ? (
              <div className="border-t px-4 py-3">
                <Reached card={card} view={view} />
              </div>
            ) : null}
            <div className="flex flex-wrap gap-1 border-t px-3 py-2">
              {span?.call ? (
                <Button type="button" variant="ghost" size="sm" className={ACTION} onClick={() => onShowCall(span.call as string)} data-testid="progress-trace">
                  <ListTree aria-hidden="true" />
                  {t("trace")}
                </Button>
              ) : null}
              {run.diff_sha256 ? (
                <Button asChild variant="ghost" size="sm" className={ACTION}>
                  <Link href={runDiffHref(run.project, run.id)} data-testid="progress-diff">
                    <FileDiff aria-hidden="true" />
                    {t("diff")}
                  </Link>
                </Button>
              ) : null}
              <Button asChild variant="ghost" size="sm" className={ACTION}>
                <Link href={stepHref(step.key)}>
                  <ChevronRight aria-hidden="true" />
                  {t("openStep")}
                </Link>
              </Button>
            </div>
          </>
        )}
      </article>
    </li>
  );
}
