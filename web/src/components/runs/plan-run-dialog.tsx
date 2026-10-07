"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleAlert, Flag, GitBranch, GitMerge, ListChecks, Play, TriangleAlert, X } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useId, useState } from "react";

import { InlineError, useWriteFailure } from "@/components/admin/notice";
import { useStepStatusText } from "@/components/plans/status";
import { StatusBadge, useStatusText } from "@/components/status/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Skeleton } from "@/components/ui/skeleton";
import { KNOWN_RUNTIMES } from "@/components/workers/model";
import { workersQuery } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { planQuery } from "@/lib/plan-queries";
import { parsePlan } from "@/lib/plans";
import { projectQuery, whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { Choice, ModelField, Outlook, RuntimeHint, Section, type Target, WorkerPicker } from "./dispatch-fields";
import { useDispatchPlanRun } from "./hooks";
import { dispatchOutlook, dispatchWorkers, modelSuggestions, type PlanRunScope, planRunScope, readModel } from "./model";
import {
  DEFAULT_PLAN_TIMEOUT,
  MODES,
  PLAN_TIMEOUT_CHOICES,
  type PlanTimeout,
  readyStepsQuery,
  type RequestedRuntime,
  type Run,
  runHref,
  type RunMode,
  RUNTIMES,
  type StepReadiness,
} from "./queries";

type Props = {
  project: string;
  planId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the plan run the hub queued, for the page to say so once the dialog closes. */
  onDispatched: (run: Run) => void;
  /** Where focus goes once the dialog has closed, for a dialog opened without a trigger (the command palette). */
  onCloseAutoFocus?: (event: Event) => void;
};

/**
 * Run plan: one run, on one of the visitor's workers, that does every step of the plan not done yet, in depends_on
 * order, in one session (docs/workers.md, A plan run on the machine). The dialog says what the run will do (its steps,
 * repos, checkpoints, and where the agent may push and merge), takes the runtime, model, mode, worker and timeout, and
 * says in its footer which worker could take it now. The content mounts each time the dialog opens.
 */
export function PlanRunDialog({ project, planId, open, onOpenChange, onDispatched, onCloseAutoFocus }: Props) {
  const write = useDispatchPlanRun(project);
  const pending = write.isPending;
  const guard = (event: Event) => {
    if (pending) event.preventDefault();
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <DialogContent
        showCloseButton={false}
        className="flex max-h-[calc(100dvh-2rem)] flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl"
        onEscapeKeyDown={guard}
        onInteractOutside={guard}
        onCloseAutoFocus={onCloseAutoFocus}
        data-testid="plan-run-dialog"
      >
        <PlanRunForm project={project} planId={planId} write={write} onClose={() => onOpenChange(false)} onDispatched={onDispatched} />
      </DialogContent>
    </Dialog>
  );
}

type Write = ReturnType<typeof useDispatchPlanRun>;

/** What keeps the hub from taking a plan run of the plan now, or makes the run fail before its agent starts. */
type Blocker =
  | { kind: "planRun"; id: number; state: Run["state"]; login: string }
  | { kind: "stepRun"; id: number; state: Run["state"]; step: string }
  | { kind: "noPending" }
  | { kind: "unplaced"; steps: string[] }
  | { kind: "noBranch"; repos: string[] };

function blockersOf(scope: PlanRunScope, steps: readonly StepReadiness[], planRun: { id: number; state: Run["state"]; dispatched_by: string } | null): Blocker[] {
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

function PlanRunForm({
  project,
  planId,
  write,
  onClose,
  onDispatched,
}: {
  project: string;
  planId: string;
  write: Write;
  onClose: () => void;
  onDispatched: (run: Run) => void;
}) {
  const t = useTranslations("runs.planRun");
  const tDispatch = useTranslations("runs.dispatch");
  const ids = useId();
  const failure = useWriteFailure();
  const plan = useQuery(planQuery(browserApi, project, planId));
  const ready = useQuery({ ...readyStepsQuery(browserApi, project, planId), refetchInterval: 10_000 });
  const me = useQuery(whoamiQuery(browserApi));
  const workersState = useQuery(workersQuery(browserApi));
  const projectState = useQuery(projectQuery(browserApi, project));

  const [runtime, setRuntime] = useState<RequestedRuntime>("any");
  const [modelText, setModelText] = useState("");
  const [mode, setMode] = useState<RunMode>("headless");
  const [target, setTarget] = useState<Target>("auto");
  const [pinnedChoice, setPinnedChoice] = useState<number | null>(null);
  const [timeout, setTimeoutHours] = useState<PlanTimeout>(DEFAULT_PLAN_TIMEOUT);

  const view = plan.data ? parsePlan(plan.data.body, plan.data.plan_id) : null;
  const defaults = Object.fromEntries((projectState.data?.repos ?? []).map((repo) => [repo.name, repo.default_branch ?? null]));
  const scope = view && ready.data ? planRunScope(ready.data.steps, view, defaults) : null;
  const blockers = scope && ready.data ? blockersOf(scope, ready.data.steps, ready.data.plan_run ?? null) : [];
  const mine = me.data && workersState.data ? dispatchWorkers(workersState.data, me.data.login, project) : [];
  const pinned = target === "pin" ? (pinnedChoice ?? mine[0]?.id ?? null) : null;
  const model = readModel(modelText, runtime);
  const suggestions = modelSuggestions(mine, runtime);
  const runnable = scope !== null && blockers.length === 0;
  const outlook = dispatchOutlook(runnable ? 1 : 0, mine, { project, runtime, repos: scope?.repos.map((repo) => repo.repo) ?? [] }, pinned);
  const pending = write.isPending;
  const loading = plan.isPending || ready.isPending;
  const loadFailed = !loading && (plan.isError || ready.isError) && scope === null;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending || !runnable || model.problem !== null) return;
    let run: Run;
    try {
      run = await write.mutateAsync({ plan_id: planId, worker_id: pinned, runtime, model: model.model, mode, timeout_h: timeout });
    } catch {
      return; // shown above the buttons, from the mutation's state
    }
    onDispatched(run);
    onClose();
  };

  const error = write.isError ? failure(write.error, { 403: t("errors.forbidden"), 404: t("errors.notFound"), 409: t("errors.conflict"), 422: t("errors.invalid") }) : null;
  const apiDetail = write.isError && isApiError(write.error) && write.error.info.status === 409 ? t("errors.hubSays", { message: write.error.info.message }) : null;
  const planTitle = view?.title ?? planId;

  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex min-h-0 flex-1 flex-col" aria-busy={pending || undefined}>
      <div className="flex items-start gap-3 border-b px-4 pt-4 pb-3 sm:px-5">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <DialogTitle className="flex items-center gap-2 text-lg font-semibold">
            <Play className="size-5 text-muted-foreground" aria-hidden="true" />
            {t("title")}
          </DialogTitle>
          <DialogDescription className="text-pretty">
            {t.rich("description", {
              plan: planTitle,
              name: (chunks) => <span className="font-medium text-foreground [overflow-wrap:anywhere]">{chunks}</span>,
            })}
          </DialogDescription>
        </div>
        <DialogClose asChild>
          <Button type="button" variant="ghost" size="icon-lg" className="-mt-1 -mr-1 shrink-0" aria-label={tDispatch("close")} disabled={pending}>
            <X aria-hidden="true" />
          </Button>
        </DialogClose>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-4 py-4 sm:px-5" data-testid="plan-run-body">
        {loading ? (
          <div className="flex flex-col gap-2" role="status" aria-label={t("loading")}>
            <Skeleton className="h-28 w-full" />
            <Skeleton className="h-16 w-full" />
          </div>
        ) : loadFailed ? (
          <p className="text-sm text-danger" role="alert" data-testid="plan-run-load-failed">
            {t("loadFailed")}
          </p>
        ) : scope ? (
          <>
            <Blockers project={project} blockers={blockers} />
            <ScopeSummary scope={scope} />
          </>
        ) : null}

        <Section legend={tDispatch("runtime")} testId="dispatch-runtime">
          <div className="grid gap-2 sm:grid-cols-2">
            {(["any", ...RUNTIMES] as const).map((value) => (
              <Choice
                key={value}
                type="radio"
                name="runtime"
                value={value}
                checked={runtime === value}
                onChange={() => setRuntime(value)}
                title={value === "any" ? tDispatch("runtimeAny") : KNOWN_RUNTIMES[value]}
                hint={<RuntimeHint runtime={value} workers={mine} loaded={workersState.isSuccess} />}
              />
            ))}
          </div>
        </Section>

        <ModelField value={modelText} onChange={setModelText} runtime={runtime} suggestions={suggestions} problem={model.problem} disabled={pending} />

        <Section legend={tDispatch("mode")} testId="dispatch-mode">
          <div className="grid gap-2 sm:grid-cols-2">
            {MODES.map((value) => (
              <Choice
                key={value}
                type="radio"
                name="mode"
                value={value}
                checked={mode === value}
                onChange={() => setMode(value)}
                title={tDispatch(`modes.${value}`)}
                hint={tDispatch(`modes.${value}Hint`)}
              />
            ))}
          </div>
        </Section>

        <WorkerPicker
          project={project}
          workers={mine}
          loading={workersState.isPending || me.isPending}
          target={target}
          pinned={pinned}
          onTarget={setTarget}
          onPinned={setPinnedChoice}
          subject="plan"
        />

        <div className="flex flex-col gap-1.5">
          <label htmlFor={`${ids}-timeout`} className="text-sm font-medium">
            {t("timeout")}
          </label>
          <NativeSelect
            id={`${ids}-timeout`}
            value={String(timeout)}
            onChange={(event) => setTimeoutHours(Number(event.target.value) as PlanTimeout)}
            className="w-full sm:w-56 [&_select]:h-9"
            aria-describedby={`${ids}-timeout-hint`}
            data-testid="plan-run-timeout"
          >
            {PLAN_TIMEOUT_CHOICES.map((hours) => (
              <NativeSelectOption key={hours} value={String(hours)}>
                {t("timeoutValue", { hours })}
              </NativeSelectOption>
            ))}
          </NativeSelect>
          <p id={`${ids}-timeout-hint`} className="text-xs text-pretty text-muted-foreground">
            {t("timeoutHint")}
          </p>
        </div>

        {error ? <InlineError text={error.text} detail={apiDetail ?? error.detail} requestId={error.requestId} /> : null}
      </div>

      <div className="flex flex-col gap-3 border-t bg-muted/50 px-4 py-3 sm:flex-row sm:items-center sm:px-5">
        <Outlook outlook={outlook} project={project} subject="plan" />
        <div className="flex shrink-0 flex-col-reverse gap-2 sm:flex-row">
          <DialogClose asChild>
            <Button
              type="button"
              variant="outline"
              size="lg"
              aria-disabled={pending || undefined}
              className="aria-disabled:opacity-50"
              onClick={(event) => pending && event.preventDefault()}
            >
              {tDispatch("cancel")}
            </Button>
          </DialogClose>
          <Button type="submit" size="lg" disabled={!runnable || model.problem !== null} busy={pending} data-testid="plan-run-submit">
            <Play aria-hidden="true" />
            {pending ? t("submitting") : t("submit")}
          </Button>
        </div>
      </div>
    </form>
  );
}

function Blockers({ project, blockers }: { project: string; blockers: Blocker[] }) {
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

/** The steps the run does, its repos and their branches, its checkpoints, and where the agent may push and merge. */
function ScopeSummary({ scope }: { scope: PlanRunScope }) {
  const t = useTranslations("runs.planRun.summary");
  const format = useFormatter();
  const statusText = useStepStatusText();
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
        <dt className="text-muted-foreground">{t("steps")}</dt>
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
          <span className="text-xs text-pretty text-muted-foreground">{t("order")}</span>
        </dd>

        <dt className="text-muted-foreground">{t("repos")}</dt>
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

        <dt className="text-muted-foreground">{t("checkpoints")}</dt>
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
              <p className="mt-1 text-xs text-pretty text-muted-foreground">{t("checkpointsHint")}</p>
            </>
          )}
        </dd>
      </dl>
      <p
        className={cn(
          "flex items-start gap-2 rounded-sm border px-3 py-2 text-xs text-pretty",
          defaults.length ? "border-attention/30 bg-attention-soft text-attention" : "bg-card text-muted-foreground",
        )}
        data-testid="plan-run-push"
        data-default={defaults.length > 0}
      >
        {defaults.length ? <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden="true" /> : <GitBranch className="mt-px size-3.5 shrink-0" aria-hidden="true" />}
        <span>
          {t("push")}{" "}
          {defaults.length
            ? t("pushDefault", {
                repos: format.list(
                  defaults.map((repo) => t("repoBranch", { repo: repo.repo, branch: repo.branch ?? "" })),
                  { type: "conjunction" },
                ),
                count: defaults.length,
              })
            : t("pushNoDefault")}
        </span>
      </p>
    </section>
  );
}
